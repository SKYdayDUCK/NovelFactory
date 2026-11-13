"""Linux multi-process SDK status ablation, with per-request traces and a ledger oracle."""
import argparse
import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from contextvars import ContextVar
import gzip
import json
import multiprocessing
import os
from pathlib import Path
import resource
import subprocess
from tempfile import TemporaryDirectory
from time import monotonic_ns, process_time
from unittest.mock import patch

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import event, text

from benchmarks.hera_mixed import population, task_spec
from benchmarks.http_load import loopback_server, percentiles
from benchmarks.p2_behavioral_load import isolated_kernel
from novel_factory.application.audit_log import LogService
from novel_factory.application.kernel import Kernel
from novel_factory.application.mcp_runtime import McpRuntime
from novel_factory.application.task_operations import TaskOperationsBinding
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.objects import ObjectStore
from novel_factory.interfaces.admission import AdmissionController
from novel_factory.interfaces.http import HttpSettings, create_app

request_tag = ContextVar("request_tag", default="")


def serve(pipe, url, objects, bindings, enabled, cpus):
    os.sched_setaffinity(0, cpus)
    database = Database(url, pool_size=4, max_overflow=0)
    kernel = Kernel(database, ObjectStore(Path(objects)))
    data = []
    statements = {}
    engine = database.engine
    settings = HttpSettings(fuse_status_auth=enabled, fenced_task_status=True, rate_burst=500)
    app = create_app(kernel, settings=settings, mcp_bindings=bindings)

    def log(component, start, end, detail=None):
        if request_tag.get():
            data.append((request_tag.get(), component, start, end, detail))

    def before_sql(conn, cursor, sql, params, ctx, many):
        ctx.hera_start = monotonic_ns()

    def after_sql(conn, cursor, sql, params, ctx, many):
        end = monotonic_ns()
        if request_tag.get():
            idx = statements.setdefault(sql, len(statements))
            log("sql", ctx.hera_start, end, idx)

    event.listen(engine, "before_cursor_execute", before_sql)
    event.listen(engine, "after_cursor_execute", after_sql)
    event.listen(engine, "begin", lambda conn: conn.info.update(hera_tx=monotonic_ns()))
    for operation in ("commit", "rollback"):
        event.listen(engine, operation, lambda conn, op=operation: log(
            "transaction_to_" + op, conn.info["hera_tx"], monotonic_ns()))
    event.listen(engine, "checkout", lambda *_: log("pool_checkout", monotonic_ns(), monotonic_ns(),
                                                    engine.pool.checkedout()))

    def measure(name, method):
        def wrapper(*args, **kwargs):
            start = monotonic_ns()
            try:
                return method(*args, **kwargs)
            finally:
                log(name, start, monotonic_ns())
        return wrapper

    original_slot = AdmissionController.slot

    @asynccontextmanager
    async def slot(self, principal, kind="default"):
        start = monotonic_ns()
        async with original_slot(self, principal, kind):
            queue = "auth" if self is app.state.authentication_admission else "ingress" if self is app.state.ingress else "business"
            log("queue_" + queue,
                start, monotonic_ns(), kind)
            yield

    async def instrumented(scope, receive, send):
        raw = dict(scope.get("headers", [])).get(b"x-hera-probe", b"")
        tag = request_tag.set(raw.decode("ascii"))
        start = monotonic_ns()
        try:
            await app(scope, receive, send)
        finally:
            log("asgi", start, monotonic_ns())
            request_tag.reset(tag)

    # Trace the same SDK and SQLAlchemy paths in both arms, without logging bearer
    # tokens, bind parameters, response bodies, or JSON-RPC message contents.
    with patch.object(AdmissionController, "slot", slot), \
            patch.object(LogService, "begin_tool", staticmethod(measure("begin_tool", LogService.begin_tool))), \
            patch.object(LogService, "finish_tool", staticmethod(measure("finish_tool", LogService.finish_tool))), \
            patch.object(McpRuntime, "available_tools", measure("available_tools", McpRuntime.available_tools)), \
            patch.object(McpRuntime, "task_operation", measure("task_operation", McpRuntime.task_operation)), \
            patch.object(Kernel, "authenticate", measure("authenticate", Kernel.authenticate)), \
            loopback_server(instrumented, lifespan="on") as base:
        cpu = process_time()
        pipe.send(base)
        pipe.recv()
        pipe.send(dict(events=data, sql_texts=list(statements), server_cpu_s=process_time()-cpu,
                       server_peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                       server_affinity=sorted(os.sched_getaffinity(0))))
    database.close()


async def call_phase(base, fixtures, warm_calls):
    records = []
    async with AsyncExitStack() as stack:
        sessions = []
        for identity, task in fixtures:
            client = await stack.enter_async_context(httpx.AsyncClient(
                headers={"authorization": "Bearer " + identity["token"]},
                trust_env=False, timeout=30))
            read, write, _ = await stack.enter_async_context(streamable_http_client(base + "/mcp", http_client=client))
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            sessions.append((session, task, client))

        async def lane(i, session, task, client, phase, calls):
            # SDK's writer task outlives this coroutine. Each lane owns a dedicated
            # HTTP client and has only one call in flight, so tag that client.
            for j in range(calls):
                tag = f"{phase}.{i}.{j}"
                client.headers["x-hera-probe"] = tag
                start = monotonic_ns()
                result = await session.call_tool("nf_task_status", {"task_id": task})
                end = monotonic_ns()
                body = result.structuredContent or {}
                ok = not result.isError and body.get("task_id") == task and body.get("state") == "READY"
                records.append((tag, start, end, ok))
        for phase, calls in (("first", 1), ("warm", warm_calls)):
            await asyncio.gather(*(lane(i, *s, phase, calls) for i, s in enumerate(sessions)))
    return records


def run(args, enabled, index):
    with TemporaryDirectory() as tmp, isolated_kernel(os.environ["NF_TEST_DATABASE_URL"], Path(tmp)) as (setup, url):
        actors = population(setup, args.clients)
        fixtures, bindings = [], {}
        for identity, wr in actors:
            p = identity["principal"]
            spec = task_spec(p, wr)
            setup.create_task(p, spec, "seed-" + spec["task_id"])
            fixtures.append((identity, spec["task_id"]))
            bindings[p] = TaskOperationsBinding(wr["work_id"], wr["branch_id"])
        setup.db.close()
        ctx = multiprocessing.get_context("spawn")
        parent, child = ctx.Pipe()
        server = ctx.Process(target=serve, args=(child, url, tmp, bindings, enabled, args.server_cpus))
        server.start()
        try:
            if not parent.poll(30):
                raise TimeoutError("Server did not report its loopback address")
            base = parent.recv()
            cpu = process_time()
            calls = asyncio.run(call_phase(base, fixtures, args.warm_calls))
            client_cpu = process_time() - cpu
            parent.send("stop")
            if not parent.poll(30):
                raise TimeoutError("Server trace timed out")
            telemetry = parent.recv()
        finally:
            if server.is_alive() and server.exitcode is None:
                server.join(5)
            if server.is_alive():
                server.terminate()
            server.join()
            parent.close()
            child.close()
        assert not server.exitcode
        with setup.db.uow(write=False) as repo:
            oracle = repo.conn.execute(text("""
                SELECT count(*) as records,count(o.outbox_id) as published
                FROM nf_audit_events a LEFT JOIN nf_outbox o
                    ON o.dedupe_key='audit:' || a.event_id::text
                WHERE a.tool_name='nf_task_status' AND (
                    a.event_type='mcp.started' OR (a.event_type='mcp.finished' AND a.outcome='OK'))
            """)).mappings().one()
            pg = repo.conn.scalar(text("SHOW server_version"))
        result = dict(sha=subprocess.check_output(["git","rev-parse","HEAD"], text=True).strip(),
                      run=index, fused=enabled, postgres=pg, pool=4, clients=args.clients,
                      client_affinity=sorted(os.sched_getaffinity(0)), client_cpu_s=client_cpu,
                      client_peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                      call_fields=["tag", "start_ns", "end_ns", "verified"],
                      event_fields=["tag", "component", "start_ns", "end_ns", "detail"],
                      calls=calls, oracle=dict(oracle), **telemetry)
        rows = [r for r in calls if r[0].startswith("warm.")]
        result["warm_latency"] = percentiles([(r[2]-r[1])/1e9 for r in rows])
        result["warm_goodput"] = sum(r[3] for r in rows)*1e9/(max(r[2] for r in rows)-min(r[1] for r in rows))
        path = args.output / f"{index}-{'on' if enabled else 'off'}.json.gz"
        with gzip.open(path, "wt") as f:
            json.dump(result, f, separators=(",",":"))
        print(path, result["warm_goodput"], result["warm_latency"], flush=True)
        assert all(r[3] for r in calls) and oracle["records"] == oracle["published"] == 2*len(calls)
        assert {r[0] for r in telemetry["events"] if r[1] == "begin_tool"} == {r[0] for r in calls}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-cpus", type=int, nargs="+", required=True)
    parser.add_argument("--clients", type=int, default=8)
    parser.add_argument("--warm-calls", type=int, default=24)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--order", default="ABBA")
    args = parser.parse_args()
    if set(args.order) - {"A","B"} or not args.order or min(args.clients, args.warm_calls) < 1:
        parser.error("Use positive counts and an A/B order")
    args.output.mkdir(parents=True, exist_ok=True)
    for index, arm in enumerate(args.order):
        run(args, arm == "B", index)
