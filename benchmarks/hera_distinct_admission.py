"""Bound the live client population while admitting independently authorized writers to two works."""

import argparse
import asyncio
import gzip
import json
import logging
import os
import resource
import subprocess
from collections import Counter
from contextlib import AsyncExitStack, ExitStack
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter, process_time
from urllib.parse import urlsplit

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import text

from benchmarks.hera_mixed import population, task_spec
from benchmarks.http_load import SqlMeasurements, loopback_server, percentiles
from benchmarks.p2_behavioral_load import hardware, isolated_kernel
from novel_factory.application.kernel import Kernel
from novel_factory.application.task_operations import TaskOperationsBinding
from novel_factory.infrastructure.database import Database
from novel_factory.interfaces.http import HttpSettings, create_app


def prepare(kernel, count):
    identities = population(kernel, count)
    active = []
    bindings = {}
    for i, (grant, world) in enumerate(identities):
        actor = grant["principal"]
        spec = task_spec(actor, world)
        bindings[actor] = TaskOperationsBinding(world["work_id"], world["branch_id"])
        active.append(
            (
                grant,
                {"spec": spec, "idempotency_key": "hera:" + spec["task_id"]},
                "mcp" if i % 4 in (0, 3) else "rest",
            )
        )
    assert (
        len(bindings)
        == len({a.grant_id for a in bindings})
        == len({a.principal_id for a in bindings})
        == count
    )
    assert (
        len({row[0]["token"] for row in active})
        == len({row[1]["spec"]["task_id"] for row in active})
        == count
    )
    return active, bindings


def pg_usage(pids):
    ticks = os.sysconf("SC_CLK_TCK")
    cpu, rss = 0, 0
    for pid in pids:
        parts = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        cpu += (int(parts[11]) + int(parts[12])) / ticks
        rss += int(parts[21]) * os.sysconf("SC_PAGE_SIZE") // 1024
    return cpu, rss


def established(port):
    # Count only server-side established IPv4 sockets, so a loopback connection is counted once.
    return sum(
        line.split()[3] == "01" and int(line.split()[1].split(":")[1], 16) == port
        for line in Path("/proc/net/tcp").read_text().splitlines()[1:]
    )


async def open_peer(stack, base, token, protocol):
    client = await stack.enter_async_context(
        httpx.AsyncClient(
            base_url=base,
            trust_env=False,
            timeout=60,
            headers={"Authorization": "Bearer " + token},
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=1),
        )
    )
    if protocol == "mcp":
        wires = await stack.enter_async_context(
            streamable_http_client(base + "/mcp", http_client=client)
        )
        sdk = await stack.enter_async_context(ClientSession(*wires[:2]))
        assert (await sdk.initialize()).protocolVersion == "2025-11-25"

        async def call(command, *, read=None):
            reply = await sdk.call_tool(
                "nf_task_status" if read else "nf_task_submit",
                {"task_id": read} if read else command,
            )
            return "TOOL_ERROR" if reply.isError else "OK", reply.structuredContent or {}
    else:

        async def call(command, *, read=None):
            reply = (
                await client.get("/api/v1/tasks/" + read)
                if read
                else await client.post("/api/v1/hera/tasks", json=command)
            )
            return str(reply.status_code), reply.json()

    return call


async def negatives(base, active):
    results = []
    for index in (0, 1):
        grant, command, proto = active[index]
        async with AsyncExitStack() as stack:
            call = await open_peer(stack, base, grant["token"], proto)
            peer_tid = active[1 - index][1]["spec"]["task_id"]
            spec = task_spec(grant["principal"], command["spec"]["world_ref"])
            denied_read = await call({}, read=peer_tid)
            denied_submit = await call(
                {"spec": spec, "idempotency_key": "negative:" + spec["task_id"]}
            )
            for probe, (code, body) in (("cross_work", denied_read), ("full", denied_submit)):
                error = body.get("error", body)
                results.append(
                    dict(
                        transport=proto,
                        probe=probe,
                        wire_code=code,
                        error=error.get("code"),
                        retryable=error.get("retryable"),
                        retry_after_ms=error.get("retry_after_ms"),
                    )
                )
    return results


async def exercise(base, app, kernel, active, pids):
    index = iter(enumerate(active))
    rows = []
    inflight, sessions, business = Counter(), Counter(), Counter()
    peak = Counter()
    telemetry = []
    port = urlsplit(base).port

    def enter(kind, label):
        kind[label] += 1
        peak[label] = max(peak[label], kind[label])

    begin = perf_counter()
    cpu = process_time()
    pg_cpu, _ = pg_usage(pids)

    async def sample():
        while True:
            _, pg_rss = pg_usage(pids)
            tcp = established(port)
            telemetry.append([round(perf_counter() - begin, 4), tcp, pg_rss, len(rows)])
            await asyncio.sleep(0.5)

    async def lane():
        for i, (grant, command, proto) in index:
            actor = grant["principal"]
            spec = command["spec"]
            t0 = perf_counter() - begin
            enter(inflight, "lifecycles")
            installed, issued, response, ok = False, None, None, False
            code, cleanup = "NO_RESPONSE", None
            try:
                async with AsyncExitStack() as stack:
                    call = await open_peer(stack, base, grant["token"], proto)
                    installed = True
                    if proto == "mcp":
                        enter(sessions, "sdk_sessions")
                    issued = perf_counter() - begin
                    enter(business, "operations")
                    try:
                        wire, body = await call(command)
                        code = body.get("error", body).get("code", wire)
                        ok = (
                            wire == ("OK" if proto == "mcp" else "202")
                            and body.get("task_id") == spec["task_id"]
                            and body.get("state") == "READY"
                            and body.get("generation") == 0
                            and body.get("status_tool") == "nf_task_status"
                            and body.get("status_url") == "/api/v1/tasks/" + spec["task_id"]
                        )
                        response = perf_counter() - begin
                    finally:
                        business["operations"] -= 1
            except Exception as exc:
                cleanup = type(exc).__name__
            finally:
                if installed and proto == "mcp":
                    sessions["sdk_sessions"] -= 1
                inflight["lifecycles"] -= 1
                rows.append(
                    [
                        i,
                        actor.principal_id,
                        actor.grant_id,
                        spec["world_ref"]["work_id"],
                        spec["task_id"],
                        proto,
                        installed,
                        code,
                        ok,
                        cleanup,
                        t0,
                        issued,
                        response,
                        perf_counter() - begin,
                    ]
                )
                if len(rows) % 500 == 0:
                    print(
                        "completed",
                        len(rows),
                        "elapsed",
                        round(perf_counter() - begin, 2),
                        flush=True,
                    )

    monitor = asyncio.create_task(sample())
    with SqlMeasurements(kernel.db.engine) as sql:
        try:
            await asyncio.gather(*(lane() for _ in range(20)))
        finally:
            monitor.cancel()
            try:
                await monitor
            except asyncio.CancelledError:
                pass
        elapsed = perf_counter() - begin
        metrics = sql.result(elapsed)
    pg_final, _ = pg_usage(pids)
    result = dict(
        raw_fields=[
            "index",
            "principal",
            "grant",
            "work",
            "task",
            "wire",
            "initialized",
            "response_code",
            "ok",
            "exception",
            "lane_start_s",
            "business_start_s",
            "response_s",
            "client_closed_s",
        ],
        rows=sorted(rows),
        peak=dict(peak),
        telemetry_fields=[
            "from_release_s",
            "established_api_tcp",
            "pg_backend_rss_sum_kib",
            "complete",
        ],
        telemetry=telemetry,
        peak_api_established_sampled=max(row[1] for row in telemetry),
        peak_pg_backends_rss_sum_kib_sampled=max(row[2] for row in telemetry),
        pg_cpu_s=pg_final - pg_cpu,
        client_api_cpu_s=process_time() - cpu,
        client_api_peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        last_reply_s=max((r[12] for r in rows if r[12] is not None), default=0),
        drain_s=elapsed,
        sql=metrics,
        call_latency=percentiles([r[12] - r[11] for r in rows if r[12] is not None]),
        release_latency=percentiles([r[12] for r in rows if r[12] is not None]),
        admissions={
            name: getattr(app.state, name).snapshot()
            for name in ("ingress", "admission", "authentication_admission")
        },
    )
    result["negative_controls"] = await negatives(base, active)
    return result


def observe(kernel, active):
    with kernel.db.uow(write=False) as repo:
        rows = repo.conn.execute(
            text("""
            SELECT t.task_id::text,t.principal_id,t.grant_id::text,t.work_id::text,t.branch_id::text,t.state,
                a.resource_class,g.active,g.work_id=t.work_id,g.token_hash,
                c.receipt->>'task_id'=t.task_id::text AND c.receipt->>'state'='READY',
                (SELECT count(*) FROM nf_outbox o WHERE o.aggregate_id=t.task_id::text AND o.topic='task.changed')
            FROM nf_tasks t JOIN nf_durable_admissions a USING(task_id) JOIN nf_grants g USING(grant_id)
            LEFT JOIN nf_task_commands c ON c.principal_id=t.principal_id AND c.idempotency_key='hera:'||t.task_id::text
        """)
        ).all()
        matching = {
            r[:5]
            for r in rows
            if tuple(r[5:9]) == ("READY", "MODEL", True, True) and r[10] and r[11] == 1
        }
        expected = {
            (
                s["spec"]["task_id"],
                g["principal"].principal_id,
                g["principal"].grant_id,
                s["spec"]["world_ref"]["work_id"],
                s["spec"]["world_ref"]["branch_id"],
            )
            for g, s, _ in active
        }
        counts = repo.conn.execute(
            text("""SELECT (SELECT count(*) FROM nf_tasks),(SELECT count(*) FROM nf_durable_admissions),
            (SELECT count(*) FROM nf_task_commands),(SELECT count(*) FROM nf_call_attempts), current_setting('server_version')""")
        ).one()
        return dict(
            matched=len(matching & expected),
            unmatched_expected=len(expected - matching),
            unexpected=len(matching - expected),
            pg_task_rows=counts[0],
            pg_admissions=counts[1],
            pg_command_rows=counts[2],
            provider_calls=counts[3],
            pg_version=counts[4],
            distinct_persisted_tokens=len({r[9] for r in rows}),
            by_work=dict(Counter(r[3] for r in rows)),
            unchanged_heads=all(
                kernel.head(repo, w["work_id"], w["branch_id"]) == w
                for w in [x[1]["spec"]["world_ref"] for x in active[:2]]
            ),
        )


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--count", required=True, type=int, choices=(20, 5000))
    p.add_argument("--output", required=True, type=Path)
    args = p.parse_args()
    logging.getLogger("mcp").setLevel(logging.WARNING)
    with (
        TemporaryDirectory() as folder,
        isolated_kernel(os.environ["NF_TEST_DATABASE_URL"], Path(folder)) as (setup, url),
    ):
        setup.db.close()
        kernel = Kernel(Database(url, pool_size=4, max_overflow=0), setup.objects)
        try:
            with kernel.db.uow() as repo:
                repo.conn.execute(
                    text("""UPDATE nf_durable_admission_policy SET enabled=true,total_limit=:n,resource_limit=:n,
                    principal_limit=1,work_limit=:w"""),
                    {"n": args.count, "w": args.count // 2},
                )
            started = perf_counter()
            active, bindings = prepare(kernel, args.count)
            provisioning = perf_counter() - started
            with ExitStack() as stack:
                conns = [stack.enter_context(kernel.db.engine.connect()) for _ in range(4)]
                pids = [conn.scalar(text("SELECT pg_backend_pid()")) for conn in conns]
            app = create_app(
                kernel,
                settings=HttpSettings(
                    max_inflight_requests=32,
                    per_principal_concurrency=1,
                    max_queued_ingress=64,
                    max_queued_operations=64,
                    queue_timeout_seconds=60,
                ),
                mcp_bindings=bindings,
            )
            with loopback_server(app, lifespan="on") as base:
                result = asyncio.run(exercise(base, app, kernel, active, pids))
            # Fresh physical connections verify what survived the submitter and service shutdown.
            kernel.db.engine.dispose()
            result.update(
                database=observe(kernel, active),
                sha=subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
                offered=args.count,
                setup_s=provisioning,
                environment=hardware(),
                logical_ready_at_release=args.count,
            )
            result["environment"]["benchmark_source_sha256"] = sha256(
                Path(__file__).read_bytes()
            ).hexdigest()
            result["versions"] = {
                pkg: version(pkg) for pkg in ("mcp", "uvicorn", "httpx", "psycopg")
            }
            rows = result["rows"]
            controls = result["negative_controls"]
            d = result["database"]
            result["acceptance"] = (
                len(rows) == args.count
                and all(r[8] and not r[9] for r in rows)
                and all(
                    d[k] == args.count
                    for k in (
                        "matched",
                        "pg_task_rows",
                        "pg_admissions",
                        "pg_command_rows",
                        "distinct_persisted_tokens",
                    )
                )
                and d["unmatched_expected"] == d["unexpected"] == d["provider_calls"] == 0
                and d["unchanged_heads"]
                and sorted(d["by_work"].values()) == [args.count // 2] * 2
                and Counter(r[5] for r in rows) == {"mcp": args.count // 2, "rest": args.count // 2}
                and result["peak"]["lifecycles"] <= 20
                and result["peak"]["operations"] <= 20
                and result["sql"]["peak_checked_out_connections"] <= 4
                and result["sql"]["checked_out_at_end"] == 0
                and all(a["active"] == a["queued"] == 0 for a in result["admissions"].values())
                and len(controls) == 4
                and all(
                    c["error"] == ("VIEW_DENIED" if c["probe"] == "cross_work" else "BACKPRESSURE")
                    and (c["probe"] != "full" or c["retryable"] is True)
                    for c in controls
                )
            )
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with gzip.open(args.output, "wt") as output:
                json.dump(result, output, separators=(",", ":"))
            print(
                {k: v for k, v in result.items() if k not in ("rows", "telemetry", "environment")},
                flush=True,
            )
            if not result["acceptance"]:
                raise SystemExit(1)
        finally:
            kernel.db.close()


if __name__ == "__main__":
    main()
