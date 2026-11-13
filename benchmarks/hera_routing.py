"""Preregistered same-budget routing comparison on independent spawned HERA replicas."""
import argparse
import asyncio
from collections import Counter
from contextlib import AsyncExitStack, ExitStack, contextmanager
import gzip
import json
import logging
import multiprocessing
import os
from pathlib import Path
import random
import resource
import subprocess
from tempfile import TemporaryDirectory
from time import perf_counter

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from benchmarks.hera_mixed import population, task_spec
from benchmarks.http_load import SqlMeasurements, loopback_server, percentiles
from benchmarks.p2_behavioral_load import hardware, isolated_kernel
from novel_factory.application.kernel import Kernel
from novel_factory.application.task_operations import TaskOperationsBinding
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.objects import ObjectStore
from novel_factory.interfaces.admission import ConnectionBudget
from novel_factory.interfaces.http import HttpSettings, create_app


def serve(channel, url, objects, roster, replicas):
    db = Database(url, pool_size=4 // replicas, max_overflow=0)
    budget = ConnectionBudget(api_processes=replicas, api_pool_size=4 // replicas, worker_processes=0)
    settings = HttpSettings(max_inflight_requests=64 // replicas, max_queued_ingress=256 // replicas,
                            max_queued_operations=256 // replicas, max_queued_per_principal=8,
                            queue_timeout_seconds=30, rate_burst=1000, fenced_task_status=True)
    app = create_app(Kernel(db, ObjectStore(objects)), connection_budget=budget, settings=settings, mcp_bindings=roster)
    try:
        with loopback_server(app, lifespan="on") as base:
            channel.send(base)
            if channel.recv() != "measure":
                return
            before = resource.getrusage(resource.RUSAGE_SELF)
            with SqlMeasurements(db.engine) as meter:
                started = perf_counter()
                channel.send("measuring")
                if channel.recv() != "sample":
                    return
                after = resource.getrusage(resource.RUSAGE_SELF)
                channel.send({"sql": meter.result(perf_counter() - started), "pid": os.getpid(),
                              "operation": app.state.admission.snapshot(),
                              "auth": app.state.authentication_admission.snapshot(),
                              "rss_peak_kib": after.ru_maxrss,
                              "cpu_s": (after.ru_utime + after.ru_stime - before.ru_utime - before.ru_stime)})
            channel.recv()
    finally:
        db.close()
        channel.close()


@contextmanager
def replica(url, objects, roster, replicas):
    ctx = multiprocessing.get_context("spawn")
    host, worker = ctx.Pipe()
    child = ctx.Process(target=serve, args=(worker, url, objects, roster, replicas))
    child.start()
    worker.close()
    try:
        if not host.poll(60):
            raise RuntimeError("Replica startup deadline")
        yield host.recv(), host
    finally:
        try:
            host.send("stop")
        except (OSError, EOFError):
            pass
        child.join(15)
        if child.is_alive():
            child.terminate()
            child.join(5)
        host.close()


def route(mode, outstanding, sequence, rng):
    if mode == "rr" or len(outstanding) == 1:
        return sequence % len(outstanding)
    eligible = (list(range(len(outstanding))) if mode == "least"
                else rng.sample(range(len(outstanding)), 2))
    return min(eligible, key=lambda index: (outstanding[index], index))


async def measure(servers, records, mode):
    start, ready = asyncio.Event(), asyncio.Queue()
    rng, raw = random.Random(807), []
    outstanding = [0] * len(servers)
    peak, sequence, epoch = 0, 0, 0.0
    init_slots = asyncio.Semaphore(8)
    protocols = [i % 2 for i in range(len(records))]
    rng.shuffle(protocols)

    async def lane(i, identity, spec):
        nonlocal peak, sequence
        async with AsyncExitStack() as stack:
            clients = []
            async with init_slots:
                for base, _ in servers:
                    http = await stack.enter_async_context(httpx.AsyncClient(
                        base_url=base, headers={"Authorization": "Bearer " + identity["token"]},
                        trust_env=False, timeout=30))
                    if protocols[i]:
                        read, write, _ = await stack.enter_async_context(
                            streamable_http_client(base + "/mcp", http_client=http))
                        sdk = await stack.enter_async_context(ClientSession(read, write))
                        assert (await sdk.initialize()).protocolVersion == "2025-11-25"
                        assert "nf_task_status" in {t.name for t in (await sdk.list_tools()).tools}
                        clients.append(sdk)
                    else:
                        assert (await http.get("/health")).status_code == 200
                        clients.append(http)
            await ready.put(i)
            await start.wait()
            for _ in range(128 // len(records)):
                seq = sequence
                selected = route(mode, outstanding, seq, rng)
                sequence += 1
                depth = list(outstanding)
                outstanding[selected] += 1
                peak = max(peak, sum(outstanding))
                t0 = perf_counter()
                code, ok = "OK", False
                try:
                    if protocols[i]:
                        res = await clients[selected].call_tool("nf_task_status", {"task_id": spec["task_id"]})
                        body = res.structuredContent or {}
                        if res.isError:
                            code = body.get("error", {}).get("code", "MCP_ERROR")
                    else:
                        res = await clients[selected].get("/api/v1/tasks/" + spec["task_id"])
                        body = res.json()
                        if res.status_code != 200:
                            code = str(res.status_code)
                    ok = (code == "OK" and body.get("task_id") == spec["task_id"]
                          and body.get("world_ref") == spec["world_ref"] and body.get("state") == "READY"
                          and body.get("generation") == 0)
                except Exception as exc:
                    code = type(exc).__name__
                finally:
                    outstanding[selected] -= 1
                raw.append([seq, i, "mcp" if protocols[i] else "rest", spec["task_id"], selected,
                            depth, code, ok, t0 - epoch, perf_counter() - epoch])

    async with asyncio.TaskGroup() as group:
        for i, (identity, spec) in enumerate(records):
            group.create_task(lane(i, identity, spec))
        for _ in records:
            await asyncio.wait_for(ready.get(), 120)
        for _, channel in servers:
            channel.send("measure")
            assert channel.poll(20) and channel.recv() == "measuring"
        epoch = perf_counter()
        start.set()
    metrics = []
    for _, channel in servers:
        channel.send("sample")
        if not channel.poll(30):
            raise RuntimeError("Replica measurement deadline")
        metrics.append(channel.recv())
    duration = max(r[-1] for r in raw)
    ok = sum(r[7] for r in raw)
    return {"mode": mode, "clients": len(records), "offered": len(raw), "verified": ok, "seconds": duration,
            "mcp_sessions": sum(protocols)*len(servers), "peak_inflight": peak, "goodput_s": ok/duration,
            "pool_budget_total": 4, "operation_budget_total": 2, "auth_budget_total": 2,
            "service_processes": len(servers), "client_processes": 1, "metrics": metrics,
            "latency": percentiles([r[-1]-r[-2] for r in raw]), "outcomes": dict(Counter(r[6] for r in raw)),
            "raw_fields": ["seq","client","transport","task","replica","outstanding_before","status","correct","start_s","end_s"],
            "raw": sorted(raw)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    logging.getLogger("mcp").setLevel(logging.WARNING)
    args.output.mkdir(parents=True, exist_ok=True)
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    with TemporaryDirectory() as directory, isolated_kernel(os.environ["NF_TEST_DATABASE_URL"], Path(directory)) as (kernel, url):
        records, roster = [], {}
        for identity, wr in population(kernel, 32):
            spec = task_spec(identity["principal"], wr)
            kernel.create_task(identity["principal"], spec, spec["task_id"])
            records.append((identity, spec))
            roster[identity["principal"]] = TaskOperationsBinding(wr["work_id"], wr["branch_id"])
        kernel.db.close()

        def run(label, n, mode):
            replicas = 1 if mode == "single" else 2
            with ExitStack() as stack:
                servers = [stack.enter_context(replica(url, kernel.objects.root, roster, replicas))
                           for _ in range(replicas)]
                result = asyncio.run(measure(servers, records[:n], mode))
            result.update(source_sha=sha, host=hardware(), phase=label)
            with gzip.open(args.output / (label + ".json.gz"), "wt", encoding="utf8") as handle:
                json.dump(result, handle, separators=(",", ":"))
            print(label, result["verified"], round(result["goodput_s"], 2), result["latency"], flush=True)
            return result

        saturation = [run(f"saturation-{i}-{n}", n, "single") for i, n in enumerate([8,32,32,8])]
        if not all(r["verified"] == 128 and r["metrics"][0]["operation"]["peak_active"] == 2 and
                   r["metrics"][0]["operation"]["peak_queued"] > 0 for r in saturation):
            print("Single-instance saturation gate missed; see raw measurements.")
            return
        if any(saturation[h]["goodput_s"] >= saturation[l]["goodput_s"] * 1.3 for l, h in [(0,1),(3,2)]):
            print("Single-instance plateau gate missed; see raw measurements.")
            return
        for candidate in ("rr","least","p2c"):
            for i, mode in enumerate(("single",candidate,candidate,"single")):
                run(f"{candidate}-{i}-{mode}", 32, mode)


if __name__ == "__main__":
    main()
