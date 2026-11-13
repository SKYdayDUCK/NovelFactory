"""Real remote MCP role reads with separate client/server processes and a two-connection pool.

Run the same driver by absolute path with PYTHONPATH set to the product under test.
It migrates a disposable PostgreSQL database. Optional --context alternates both role tools.
"""
import argparse
import asyncio
from collections import Counter
from contextlib import AsyncExitStack
import gzip
from hashlib import sha256
import json
import logging
import multiprocessing
import os
from pathlib import Path
import resource
import inspect
import subprocess
import tempfile
from time import monotonic_ns, process_time

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import text

from benchmarks.bound_stdio_throughput import provision
from benchmarks.http_load import loopback_server, percentiles, SqlMeasurements
from benchmarks.p2_behavioral_load import isolated_kernel
from novel_factory.application.kernel import Kernel
from novel_factory.fixtures import load_fixture
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.objects import ObjectStore
from novel_factory.interfaces.bindings import parse_binding, read_private_config
from novel_factory.interfaces.http import HttpSettings, create_app


def product_source():
    repo = Path(inspect.getfile(Kernel)).resolve().parents[3]
    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()
    if git("status", "--porcelain", "--", "src", "migrations"):
        raise ValueError("Measure a clean product worktree")
    return git("rev-parse", "HEAD"), git("rev-parse", "HEAD:src")


def server(pipe, url, objects, roster, cpu, source):
    assert product_source() == source
    os.sched_setaffinity(0, {cpu})
    db = Database(url, pool_size=2, max_overflow=0)
    settings = HttpSettings(max_inflight_requests=64, max_queued_ingress=1500,
                            max_queued_operations=1024, queue_timeout_seconds=180,
                            compact_remote_role_reads=True, share_auth_slots=True)
    app = create_app(Kernel(db, ObjectStore(Path(objects))), settings=settings, mcp_bindings=roster)
    try:
        with loopback_server(app, lifespan="on", keepalive=300) as base:
            pipe.send(base)
            assert pipe.recv() == "measure"
            with SqlMeasurements(db.engine) as sql:
                start = process_time()
                pipe.send("ready")
                elapsed = pipe.recv()
                result = sql.result(elapsed)
                result.update(cpu_s=process_time() - start,
                              rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
            pipe.send(result)
    finally:
        db.close()


async def read_roles(pipe, base, roles, config, repeats, context):
    gate, ready = asyncio.Event(), asyncio.Queue()
    init = asyncio.Semaphore(4)
    rows = []
    tools = {"nf_task_get", "nf_context_get", "nf_query_batch", "nf_artifact_put"}
    async def lane(i):
        initialized = False
        try:
            async with AsyncExitStack() as stack:
                async with init:
                    client = await stack.enter_async_context(httpx.AsyncClient(
                        headers={"Authorization": "Bearer " + config[i]["token"]},
                        limits=httpx.Limits(max_connections=2, max_keepalive_connections=1, keepalive_expiry=300),
                        trust_env=False, timeout=240))
                    channels = await stack.enter_async_context(streamable_http_client(base + "/mcp", http_client=client))
                    session = await stack.enter_async_context(ClientSession(*channels[:2]))
                    await session.initialize()
                    assert {tool.name for tool in (await session.list_tools()).tools} == tools
                initialized = True
                ready.put_nowait(i)
                await gate.wait()
                for j in range(repeats):
                    name = "nf_context_get" if context and j % 2 else "nf_task_get"
                    sent = monotonic_ns()
                    result = await session.call_tool(name, {})
                    status = "OK" if not result.isError and result.structuredContent == roles[i].expected[name] else "WRONG_RESULT"
                    rows.append((i, j, sent, monotonic_ns(), status))
        except Exception as error:
            if not initialized:
                ready.put_nowait(i)
            rows.append((i, -1, 0, monotonic_ns(), type(error).__name__))
    async with asyncio.TaskGroup() as group:
        for i in range(len(roles)):
            group.create_task(lane(i))
        for _ in roles:
            await asyncio.wait_for(ready.get(), 300)
        pipe.send("measure")
        assert pipe.recv() == "ready"
        cpu = process_time()
        start = monotonic_ns()
        gate.set()
    end = max(row[3] for row in rows)
    # Include origin-to-last-response in drain; clients can close after their last response.
    duration = (end - start) / 1e9
    pipe.send(duration)
    return dict(rows=rows, start_ns=start, drain_s=duration, outcomes=dict(Counter(row[4] for row in rows)),
                latency=percentiles([(row[3] - row[2]) / 1e9 for row in rows if row[2]]),
                client_cpu_s=process_time()-cpu, client_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--clients", type=int, required=True)
    p.add_argument("--repeats", type=int, default=1)
    p.add_argument("--context", action="store_true")
    p.add_argument("--server-cpu", type=int, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    if not 1 <= args.clients <= 1000 or args.repeats < 1:
        p.error("1 to 1000 clients and a positive repeat count are required")
    logging.getLogger("mcp").setLevel(logging.WARNING)
    source = product_source()
    with tempfile.TemporaryDirectory() as root, isolated_kernel(os.environ["NF_TEST_DATABASE_URL"], Path(root)) as (kernel, url):
        worlds = [load_fixture(kernel) for _ in range(2)]
        roles = [provision(kernel, Path(root), i, world=worlds[i % 2], lease_seconds=3600) for i in range(args.clients)]
        configs = [read_private_config(role.path) for role in roles]
        roster = {kernel.authenticate(row["token"]): parse_binding(row["binding"]) for row in configs}
        assert len(roster) == len(roles)
        kernel.db.close()
        ctx = multiprocessing.get_context("spawn")
        parent, child = ctx.Pipe()
        worker = ctx.Process(target=server, args=(child, url, root, roster, args.server_cpu, source))
        worker.start()
        try:
            assert parent.poll(60), "API startup timeout"
            result = asyncio.run(read_roles(parent, parent.recv(), roles, configs, args.repeats, args.context))
            assert parent.poll(30), "API statistics timeout"
            result["server"] = parent.recv()
        finally:
            worker.join(5)
            if worker.is_alive():
                worker.terminate()
                worker.join()
            parent.close()
            child.close()
        assert worker.exitcode == 0
        with kernel.db.uow(write=False) as repo:
            result["verified_audits"] = dict(repo.conn.execute(text("""
                SELECT a.event_type, count(*) FROM nf_audit_events a
                JOIN nf_outbox o ON o.dedupe_key='audit:' || a.event_id::text AND o.topic='audit.recorded'
                JOIN nf_tasks t ON a.task_id=t.task_id AND a.principal_id::text=t.principal_id::text
                WHERE a.tool_name IN ('nf_task_get','nf_context_get')
                  AND (a.event_type='mcp.started' OR (a.event_type='mcp.finished' AND a.outcome='OK'
                       AND a.evidence_id=t.context_bundle_id))
                GROUP BY a.event_type
            """)).all())
            result["unchanged_heads"] = all(kernel.head(repo, b["world_ref"]["work_id"], b["world_ref"]["branch_id"]) == b["world_ref"] for b in worlds)
            result["attempts"] = repo.conn.scalar(text("SELECT count(*) FROM nf_call_attempts"))
            result["postgres"] = repo.conn.scalar(text("SHOW server_version"))
        assert product_source() == source
        result.update(source=source[0], product_tree=source[1],
                      driver_sha256=sha256(Path(__file__).read_bytes()).hexdigest(),
                      clients=len(roles), repeats=args.repeats, alternate_context=args.context,
                      client_affinity=sorted(os.sched_getaffinity(0)), server_cpu=args.server_cpu)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(args.output, "wt") as f:
            json.dump(result, f, separators=(",", ":"))
        print({k: v for k, v in result.items() if k != "rows"}, flush=True)
        total = len(roles) * args.repeats
        assert result["outcomes"] == {"OK": total}
        assert result["verified_audits"] == {"mcp.started": total, "mcp.finished": total}
        assert result["unchanged_heads"] and not result["attempts"]


if __name__ == "__main__":
    main()
