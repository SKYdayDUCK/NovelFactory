"""Bounded same-grant host batches on real mixed REST and SDK traffic."""
import argparse
import asyncio
from collections import Counter
from contextlib import AsyncExitStack
import gzip
import hashlib
import json
import logging
import os
from pathlib import Path
import platform
import subprocess
from tempfile import TemporaryDirectory
from time import perf_counter, process_time

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from benchmarks.hera_mixed import population, task_spec
from benchmarks.http_load import SqlMeasurements, loopback_server, percentiles
from benchmarks.p2_behavioral_load import isolated_kernel
from novel_factory.application.kernel import Kernel
from novel_factory.application.task_operations import TaskOperations, TaskOperationsBinding
from novel_factory.infrastructure.database import Database
from novel_factory.interfaces.http import HttpSettings, create_app


async def trial(kernel, sources, mode, profile):
    lane_count, per_lane = {"low": (4,16), "medium": (8,32), "high": (20,50)}[profile]
    sources = sources[:lane_count]
    bindings = {identity["principal"]: binding for identity, binding, tasks in sources}
    app = create_app(kernel, settings=HttpSettings(fenced_task_status=True, rate_burst=2000,
                     rate_per_second=2000, max_queued_operations=256, queue_timeout_seconds=60),
                     mcp_bindings=bindings)
    ready, start = asyncio.Queue(), asyncio.Event()
    epoch = 0.0
    records = []
    wire = []
    inflight = peak = 0

    async def lane(index, identity, binding, tasks):
        nonlocal inflight, peak
        async with AsyncExitStack() as stack:
            http = await stack.enter_async_context(httpx.AsyncClient(
                base_url=base, headers={"Authorization": "Bearer " + identity["token"]}, trust_env=False, timeout=90))
            sdk = None
            if index % 2:
                read, write, _ = await stack.enter_async_context(streamable_http_client(base + "/mcp", http_client=http))
                sdk = await stack.enter_async_context(ClientSession(read,write))
                await sdk.initialize()
                assert "nf_task_status_batch" in {t.name for t in (await sdk.list_tools()).tools}
            else:
                assert (await http.get("/health")).status_code == 200
            await ready.put(index)
            await start.wait()

            def offered(j):
                if profile == "low":
                    return epoch + (j * lane_count + index) * .03
                if profile == "medium":
                    return epoch + (j // 4) * .120
                return epoch

            i = 0
            while i < per_lane:
                original = offered(i)
                if perf_counter() < original:
                    await asyncio.sleep(original - perf_counter())
                if mode == "fixed" and (i + 7 >= per_lane or offered(i+7) > perf_counter()):
                    # The fill timer belongs to the oldest pending request. A late
                    # poll must not add a second full wait after its deadline.
                    next_full = offered(i+7) if i+7 < per_lane else float("inf")
                    lag = min(next_full, original + .010) - perf_counter()
                    if lag > 0:
                        await asyncio.sleep(lag)
                sent = perf_counter()
                size = 1
                if mode != "direct":
                    while size < 8 and i + size < per_lane and offered(i+size) <= sent:
                        size += 1
                group = tasks[i:i+size]
                ids = [r["task_id"] for r in group]
                batch = size > 1 or mode == "fixed"
                params = dict(work_id=binding.work_id, branch_id=binding.branch_id, task_ids=ids)
                inflight += 1
                peak = max(peak, inflight)
                code = "OK"
                result = []
                try:
                    if sdk:
                        reply = await sdk.call_tool("nf_task_status_batch" if batch else "nf_task_status",
                                                    params if batch else {"task_id": ids[0]})
                        if reply.isError:
                            code = (reply.structuredContent or {}).get("error", {}).get("code", "MCP_ERROR")
                        else:
                            result = reply.structuredContent["tasks"] if batch else [reply.structuredContent]
                    else:
                        reply = (await http.post("/api/v1/hera/tasks/status-batch", json=params) if batch
                                 else await http.get("/api/v1/tasks/" + ids[0]))
                        if reply.status_code != 200:
                            code = str(reply.status_code)
                        else:
                            result = reply.json()["tasks"] if batch else [reply.json()]
                except Exception as exc:
                    code = type(exc).__name__
                finally:
                    inflight -= 1
                end = perf_counter()
                w = len(wire)
                wire.append([index, "mcp" if sdk else "http", size, code, sent-epoch, end-epoch])
                for j, expected in enumerate(group):
                    ok = code == "OK" and len(result) == size and result[j] == expected
                    records.append([index, i+j, ids[j], w, ok, offered(i+j)-epoch, end-epoch])
                i += size

    with loopback_server(app, lifespan="on") as base:
        async with asyncio.TaskGroup() as tg:
            for i, (identity, binding, tasks) in enumerate(sources):
                tg.create_task(lane(i, identity, binding, tasks))
            for _ in sources:
                await asyncio.wait_for(ready.get(), timeout=120)
            with SqlMeasurements(kernel.db.engine) as sql:
                cpu, epoch = process_time(), perf_counter()
                start.set()
                while len(records) < per_lane * lane_count:
                    await asyncio.sleep(.005)
                elapsed = max(r[6] for r in records)
                sql_metrics, cpu = sql.result(elapsed), process_time()-cpu
    correct = sum(r[4] for r in records)
    return dict(profile=profile, mode=mode, lanes=lane_count, mcp_sessions=lane_count//2, offered=len(records),
                correct=correct, seconds=elapsed, goodput_s=correct/elapsed, sql=sql_metrics, cpu_s=cpu,
                peak_wire_inflight=peak, wire_calls=len(wire), actual_batch_sizes=dict(Counter(w[2] for w in wire)),
                latency=percentiles([r[6]-r[5] for r in records]), records=records, wire=wire)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    args.output.mkdir(exist_ok=True, parents=True)
    logging.getLogger("mcp").setLevel(logging.WARNING)
    sha = subprocess.check_output(["git","rev-parse","HEAD"], text=True).strip()
    with TemporaryDirectory() as tmp, isolated_kernel(os.environ["NF_TEST_DATABASE_URL"],Path(tmp)) as (setup,url):
        setup.db.close()
        db = Database(url, pool_size=4)
        kernel = Kernel(db, setup.objects)
        try:
            sources = []
            for identity, wr in population(kernel,20):
                p = identity["principal"]
                binding = TaskOperationsBinding(wr["work_id"],wr["branch_id"])
                expected = []
                op = TaskOperations(kernel,p,binding=binding,fenced_status=True)
                for i in range(50):
                    spec = task_spec(p,wr)
                    kernel.create_task(p,spec,spec["task_id"])
                    expected.append(op.status(spec["task_id"]))
                sources.append((identity,binding,expected))
            for profile in ("low","medium","high"):
                for candidate in ("fixed","adaptive"):
                    for i, mode in enumerate(("direct",candidate,candidate,"direct")):
                        r = asyncio.run(trial(kernel,sources,mode,profile))
                        r.update(source_sha=sha, source_digest=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                                 python=platform.python_version(), record_fields=["owner_lane","seq","task","wire","ok","offered_s","ended_s"],
                                 wire_fields=["owner_lane","protocol","count","status","sent_s","ended_s"])
                        name = f"{profile}-{candidate}-{i}-{mode}"
                        with gzip.open(args.output / (name+".json.gz"),"wt",encoding="utf8") as out:
                            json.dump(r,out,separators=(",",":"))
                        print(name,r["correct"],r["wire_calls"],round(r["goodput_s"],2),r["latency"],flush=True)
        finally:
            db.close()


if __name__ == "__main__":
    main()
