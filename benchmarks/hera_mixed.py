"""Measure shared REST / Streamable HTTP operations over TCP with per-grant clients."""
import argparse
import asyncio
from collections import Counter
from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta
import gzip
import hashlib
import json
import logging
import os
import random
import resource
import importlib.metadata
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter, process_time
from uuid import uuid4

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import text

from benchmarks.http_load import SqlMeasurements, loopback_server, percentiles
from benchmarks.p2_behavioral_load import hardware, isolated_kernel
from novel_factory.application.kernel import Kernel
from novel_factory.application.task_operations import TaskOperationsBinding
from novel_factory.fixtures import load_fixture
from novel_factory.infrastructure.database import Database
from novel_factory.interfaces.http import HttpSettings, create_app


def task_spec(principal, wr):
    return dict(schema_version="2.0", task_id=str(uuid4()), generation=0, role_id="writer",
                task_kind="hera_load", state="READY", world_ref=wr, story_time={"tick": 0, "phase": 0},
                context_bundle_id=None, policy_version="nf-policy-1", dependency_fingerprint=None,
                runtime_contract_version="a" * 64, capability_grant_id=principal.grant_id,
                input_artifacts=[], budget={"currency": "USD", "max_amount": "1", "token_budget": 10},
                wall_deadline=(datetime.now(UTC) + timedelta(hours=2)).isoformat())


def population(kernel, count):
    books = [load_fixture(kernel), load_fixture(kernel)]
    rows = []
    for i in range(count):
        book = books[i % 2]
        wr = book["world_ref"]
        identity = kernel.issue_grant(book["principal"], wr["work_id"], role_id="writer",
                                      branch_ids=[wr["branch_id"]])
        rows.append((identity, wr))
    return rows


async def trial(kernel, actors, count, percent, fast, operation, *, on_start=None):
    bindings = {}
    lanes = []
    for i, (identity, wr) in enumerate(actors[:count]):
        bindings[identity["principal"]] = TaskOperationsBinding(wr["work_id"], wr["branch_id"])
        specs = [task_spec(identity["principal"], wr) for _ in range(i, count, len(actors))]
        if operation == "read":
            kernel.create_task(identity["principal"], specs[0], "seed:" + specs[0]["task_id"])
        lanes.append((identity, specs))
    settings = HttpSettings(max_inflight_requests=64, max_queued_ingress=1500, max_queued_operations=1024,
                            max_queued_per_principal=16, per_principal_concurrency=2, queue_timeout_seconds=120,
                            rate_burst=1000, rate_per_second=1000, fenced_task_status=fast)
    app = create_app(kernel, settings=settings, mcp_bindings=bindings)
    # Cross-protocol split is by identity. Interleave the two protocols across works.
    mcp_ids = {i for i in range(len(lanes)) if (i + 1) * percent // 100 > i * percent // 100}
    # Avoid aliasing a 50/50 transport split to a 50/50 work split.
    permutation = list(range(len(lanes)))
    random.Random(807).shuffle(permutation)
    mcp_ids = {permutation[i] for i in mcp_ids}
    ready = asyncio.Queue()
    start = asyncio.Event()
    init_slots = asyncio.Semaphore(4)
    baseline = 0.0
    active = peak = 0
    raw = []

    async def lane(base, index, identity, specs):
        nonlocal active, peak
        is_mcp = index in mcp_ids
        async with AsyncExitStack() as stack:
            async with init_slots:
                client = await stack.enter_async_context(httpx.AsyncClient(
                    base_url=base, headers={"Authorization": "Bearer " + identity["token"]},
                    timeout=180, trust_env=False, limits=httpx.Limits(max_connections=2, max_keepalive_connections=1, keepalive_expiry=300)))
                if is_mcp:
                    reader, writer, _ = await stack.enter_async_context(streamable_http_client(base+"/mcp", http_client=client))
                    mcp = await stack.enter_async_context(ClientSession(reader, writer))
                    assert (await mcp.initialize()).protocolVersion == "2025-11-25"
                    assert "nf_task_status" in {tool.name for tool in (await mcp.list_tools()).tools}
                else:
                    assert (await client.get("/health")).status_code == 200
            await ready.put(index)
            await start.wait()
            for j, spec in enumerate(specs):
                target = specs[0] if operation == "read" else spec
                tid = target["task_id"]
                payload = {"spec": spec, "idempotency_key": "hera:" + tid}
                seq = j * len(lanes) + index
                t0 = perf_counter()
                active += 1
                peak = max(peak, active)
                try:
                    if is_mcp:
                        result = (await mcp.call_tool("nf_task_submit", payload) if operation == "submit"
                                  else await mcp.call_tool("nf_task_status", {"task_id": tid}))
                        body = result.structuredContent or {}
                        status = body.get("error", {}).get("code", "MCP_ERROR") if result.isError else "OK"
                    else:
                        resp = (await client.post("/api/v1/hera/tasks", json=payload) if operation == "submit"
                                else await client.get("/api/v1/tasks/" + tid))
                        body = resp.json()
                        status = "OK" if resp.status_code == (202 if operation == "submit" else 200) else str(resp.status_code)
                    ok = (status == "OK" and body.get("task_id") == tid and body.get("state") == "READY"
                          and body.get("generation") == 0 and (body.get("world_ref") == target["world_ref"]
                          if operation == "read" else body.get("status_tool") == "nf_task_status"))
                except Exception as exc:
                    ok, status = False, type(exc).__name__
                finally:
                    active -= 1
                raw.append([seq, index, "mcp" if is_mcp else "http", tid, status, ok,
                            round(t0 - baseline, 6), round(perf_counter() - baseline, 6)])

    cleanup_errors = {}
    async def measured_lane(base, i, identity, specs):
        # A transport task can raise on SDK context exit. Preserve the other
        # independently authorized clients and the original offered denominator.
        try:
            await lane(base, i, identity, specs)
        except Exception as exc:
            if not start.is_set():
                await ready.put(i)
                await start.wait()
            cleanup_errors[i] = type(exc).__name__
            seen = {r[0] for r in raw if r[1] == i}
            for j, spec in enumerate(specs):
                seq = j * len(lanes) + i
                if seq not in seen:
                    when = perf_counter() - baseline
                    tid = (specs[0] if operation == "read" else spec)["task_id"]
                    raw.append([seq, i, "mcp" if i in mcp_ids else "http", tid,
                                type(exc).__name__, False, round(when, 6), round(when, 6)])

    with loopback_server(app, lifespan="on", keepalive=300) as base:
        async with asyncio.TaskGroup() as group:
            for i, (identity, specs) in enumerate(lanes):
                group.create_task(measured_lane(base, i, identity, specs))
            for _ in lanes:
                await asyncio.wait_for(ready.get(), timeout=120)
            with SqlMeasurements(kernel.db.engine) as sql:
                cpu, baseline = process_time(), perf_counter()
                if on_start is not None:
                    on_start(baseline)
                start.set()
                # Lane teardown includes SDK notifications. Time response completion separately.
                while len(raw) < count:
                    await asyncio.sleep(.01)
                elapsed = max(row[7] for row in raw)
                cpu = process_time() - cpu
                metrics = sql.result(elapsed)
        admissions = {key: getattr(app.state, key).snapshot() for key in ("admission","ingress","authentication_admission")}
    verified = sum(r[5] for r in raw)
    ids = [r[3] for r in raw if r[5]]
    with kernel.db.uow(write=False) as repo:
        pg = repo.conn.execute(text("""
            SELECT count(*) AS n, count(*) FILTER (WHERE state='READY') AS ready
            FROM nf_tasks WHERE task_id = ANY(CAST(:ids AS uuid[]))
        """), {"ids": list(set(ids))}).one()
    return {"workload": operation, "offered": count, "principals": len(lanes), "mcp_sessions": len(mcp_ids),
            "http_clients": len(lanes) - len(mcp_ids), "api_processes": 1, "worker_processes": 0,
            "client_and_server_share_process": True, "wire": "loopback TCP", "mcp_percent_requested": percent,
            "offered_by_transport": dict(Counter(r[2] for r in raw)), "fenced": fast, "lane_errors": cleanup_errors, "outcomes": dict(Counter(r[4] for r in raw)),
            "verified_responses": verified, "unique_tasks_verified_in_pg": pg.n, "verified_ready": pg.ready,
            "durability_check": pg.n == pg.ready == len(set(ids)), "peak_client_operations": peak,
            "combined_process_peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            "versions": {pkg: importlib.metadata.version(pkg) for pkg in ("mcp", "fastapi", "httpx", "uvicorn")},
            "seconds": elapsed, "goodput_s": verified / elapsed, "cpu_s_client_and_server": cpu, "sql": metrics,
            "latency": percentiles([r[7] - r[6] for r in raw]), "from_release": percentiles([r[7] for r in raw]),
            "admission": admissions, "raw_fields": ["request", "lane", "transport", "task", "status", "ok",
                "start_s", "response_s"], "raw": sorted(raw)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--populations", nargs="+", type=int, default=[50])
    parser.add_argument("--clients", type=int, default=10)
    parser.add_argument("--mcp-pct", nargs="+", type=int, default=[0, 20, 50, 80, 100])
    parser.add_argument("--order", default="ABBA", choices=["ABBA","B"])
    parser.add_argument("--operation", choices=("read","submit"), default="read")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not (1 <= args.clients <= 1000 and all(1 <= n <= 5000 for n in args.populations)
            and all(0 <= p <= 100 for p in args.mcp_pct)):
        parser.error("Population 1..5000, live lanes 1..1000, percent 0..100")
    logging.getLogger("mcp").setLevel(logging.WARNING)
    args.output.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory() as tmp, isolated_kernel(os.environ["NF_TEST_DATABASE_URL"], Path(tmp)) as (setup, url):
        setup.db.close()
        db = Database(url, pool_size=4, max_overflow=0)
        kernel = Kernel(db, setup.objects)
        try:
            with db.uow() as repo:
                repo.conn.execute(text("UPDATE nf_durable_admission_policy SET enabled=true, "
                             "total_limit=50000,work_limit=50000,principal_limit=10000,resource_limit=50000"))
            actors = population(kernel, min(args.clients,max(args.populations)))
            for n in args.populations:
                for pct in args.mcp_pct:
                    for k, mode in enumerate(args.order):
                        result = asyncio.run(trial(kernel, actors, n, pct, mode == "B", args.operation))
                        result["environment"] = hardware()
                        result["environment"]["benchmark_source_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
                        name = f"{args.operation}-n{n}-c{args.clients}-p{pct}-{k}{mode}.json.gz"
                        with gzip.open(args.output/name, "wt", encoding="utf8") as out:
                            json.dump(result, out, separators=(",", ":"))
                        print(name, result["verified_responses"], round(result["goodput_s"],2),
                              result["latency"], flush=True)
        finally:
            db.close()


if __name__ == "__main__":
    main()
