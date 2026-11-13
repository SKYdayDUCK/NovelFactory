"""SDK/REST read matrix using independent client, service, and PostgreSQL processes."""
import argparse
import asyncio
from contextlib import AsyncExitStack
import gzip
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import resource
import subprocess
from tempfile import TemporaryDirectory
from time import monotonic_ns, process_time

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import text

from benchmarks.hera_mixed import population, task_spec
from benchmarks.http_load import loopback_server, percentiles
from benchmarks.p2_behavioral_load import isolated_kernel
from novel_factory.application.kernel import Kernel
from novel_factory.application.task_operations import TaskOperationsBinding
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.objects import ObjectStore
from novel_factory.interfaces.http import HttpSettings, create_app


def serve(pipe, url, objects, bindings, affinity):
    os.sched_setaffinity(0, affinity)
    db = Database(url, pool_size=4, max_overflow=0)
    kernel = Kernel(db, ObjectStore(Path(objects)))
    settings = HttpSettings(fenced_task_status=True, fuse_status_auth=True, max_inflight_requests=64,
                            max_queued_ingress=1500, max_queued_operations=128, queue_timeout_seconds=180,
                            rate_burst=2000, rate_per_second=2000)
    app = create_app(kernel, settings=settings, mcp_bindings=bindings)
    with loopback_server(app, lifespan="on", keepalive=300) as base:
        pipe.send(base)
        while pipe.recv() == "snapshot":
            pipe.send({"server_cpu_s": process_time(), "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                       "queues": {key: getattr(app.state, key).snapshot() for key in
                                  ("ingress", "admission", "authentication_admission")}})
    db.close()


async def measure(base, actors, specs, size, percent):
    count = min(len(actors), size)
    # Every group of ten identities gets the same protocol split. With a
    # partial round (50/20), both the first ten and the last ten have that split.
    mcp_lanes = {i for i in range(count) if (i*7 % 10) < percent/10}
    ready, release = asyncio.Queue(), asyncio.Event()
    handshake = asyncio.Semaphore(8)
    raw, lane_errors = [], {}
    epoch = peak = inflight = 0

    async def lane(i):
        nonlocal peak, inflight
        sdk = i in mcp_lanes
        registered = False
        principal, _ = actors[i]
        try:
            async with AsyncExitStack() as stack:
                async with handshake:
                    client = await stack.enter_async_context(httpx.AsyncClient(
                        base_url=base, headers={"Authorization":"Bearer " + principal["token"]}, trust_env=False,
                        timeout=180, limits=httpx.Limits(max_connections=2, max_keepalive_connections=1,
                                                        keepalive_expiry=300)))
                    if sdk:
                        rd, wr, _ = await stack.enter_async_context(streamable_http_client(base + "/mcp", http_client=client))
                        session = await stack.enter_async_context(ClientSession(rd, wr))
                        assert (await session.initialize()).protocolVersion == "2025-11-25"
                    else:
                        assert (await client.get("/api/v1/tasks/" + specs[i]["task_id"])).status_code == 200
                registered = True
                await ready.put(True)
                await release.wait()
                for j in range(i, size, count):
                    spec = specs[j]
                    tid = spec["task_id"]
                    start = monotonic_ns()
                    inflight += 1
                    peak = max(peak, inflight)
                    try:
                        if sdk:
                            msg = await session.call_tool("nf_task_status", {"task_id":tid})
                            body = msg.structuredContent or {}
                            status = body.get("error",{}).get("code","SDK_ERROR") if msg.isError else "OK"
                        else:
                            msg = await client.get("/api/v1/tasks/" + tid)
                            body = msg.json()
                            status = "OK" if msg.status_code == 200 else str(msg.status_code)
                        correct = (status == "OK" and body.get("task_id") == tid and body.get("state") == "READY"
                                   and body.get("world_ref") == spec["world_ref"] and body.get("generation") == 0)
                    except Exception as err:
                        status, correct = type(err).__name__, False
                    finally:
                        inflight -= 1
                    raw.append((j, i, tid, sdk, start-epoch, monotonic_ns()-epoch, status, correct))
        except Exception as err:
            lane_errors[i] = type(err).__name__
            if not registered:
                await ready.put(False)

    async with asyncio.TaskGroup() as group:
        for i in range(count):
            group.create_task(lane(i))
        successful_clients = sum([await asyncio.wait_for(ready.get(), 300) for _ in range(count)])
        epoch = monotonic_ns()
        release.set()
    good = sum(r[-1] for r in raw)
    return dict(rows=raw, logical_agents=size, live_clients=successful_clients, requested_clients=count,
                authorized_identities=count, http_clients=count-len(mcp_lanes), mcp_sessions_requested=len(mcp_lanes),
                mcp_sessions=sum(i not in lane_errors for i in mcp_lanes), peak_inflight=peak, lane_errors=lane_errors,
                good=good, returned=len(raw), goodput=good*1e9/max((r[5] for r in raw), default=1),
                latency=percentiles([(r[5]-r[4])/1e9 for r in raw]))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", nargs="+", type=int, default=[50,200,500,1000,2000,5000])
    parser.add_argument("--percent", nargs="+", type=int, default=[0,20,50,80,100])
    parser.add_argument("--clients", type=int, default=20)
    parser.add_argument("--server-cpus", type=int, nargs="+", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args=parser.parse_args()
    if args.clients % 10 or min(args.sizes)<args.clients or any(x%10 or not 0<=x<=100 for x in args.percent):
        parser.error("Use multiples of ten for client counts and protocol percentages")
    args.output.mkdir(exist_ok=True,parents=True)
    with TemporaryDirectory() as tmp, isolated_kernel(os.environ["NF_TEST_DATABASE_URL"], Path(tmp)) as (setup,url):
        actors=population(setup,args.clients)
        bindings={g["principal"]:TaskOperationsBinding(w["work_id"],w["branch_id"]) for g,w in actors}
        specs=[]
        for i in range(max(args.sizes)):
            grant, wr=actors[i%len(actors)]
            p=grant["principal"]
            spec=task_spec(p,wr)
            setup.create_task(p,spec,"seed-"+spec["task_id"])
            specs.append(spec)
        with setup.db.uow(write=False) as repo:
            total=repo.conn.scalar(text("SELECT count(*) FROM nf_tasks WHERE state='READY'"))
            version=repo.conn.scalar(text("SHOW server_version"))
        assert total==len(specs)
        setup.db.close()
        ctx=multiprocessing.get_context("spawn")
        parent, child=ctx.Pipe()
        server=ctx.Process(target=serve,args=(child,url,tmp,bindings,args.server_cpus))
        server.start()
        try:
            assert parent.poll(60)
            base=parent.recv()
            for size in args.sizes:
                for pct in args.percent:
                    parent.send("snapshot")
                    before=parent.recv()
                    cpu=process_time()
                    result=asyncio.run(measure(base,actors,specs,size,pct))
                    cpu=process_time()-cpu
                    parent.send("snapshot")
                    after=parent.recv()
                    result.update(source=subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip(),
                        driver_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                        client_affinity=sorted(os.sched_getaffinity(0)), server_affinity=args.server_cpus,
                        client_cpu_s=cpu, client_peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                        server_before=before, server_after=after, postgres=version, pool_budget=4,
                        model_slots=0, worker_processes=0, server_processes=1, persistent_tasks=total,
                        distinct_tasks_addressed=size, mcp_percent_requested=pct,
                        raw_fields=["logical_agent","client","task","is_mcp","start_ns","end_ns","status","verified"])
                    path=args.output/f"n{size}-c{args.clients}-p{pct}.json.gz"
                    with gzip.open(path,"wt") as f:
                        json.dump(result,f,separators=(",",":"))
                    print(path,result["good"],round(result["goodput"],2),result["latency"],flush=True)
            parent.send("stop")
        finally:
            server.join(5)
            if server.is_alive():
                server.terminate()
            server.join()
            parent.close()
            child.close()


if __name__=="__main__":
    main()
