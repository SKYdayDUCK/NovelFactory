"""A synchronized authorized call from each real SDK client into a shared loopback replica."""
import argparse
import asyncio
from collections import Counter
import gzip
import hashlib
import json
import logging
import os
from pathlib import Path
import subprocess
import tempfile
from time import perf_counter

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
import psutil
from sqlalchemy import text

from benchmarks.bound_stdio_throughput import provision
from benchmarks.p2_behavioral_load import isolated_kernel
from benchmarks.p59_mcp_remote import replicas
from novel_factory.fixtures import load_fixture
from novel_factory.interfaces.bindings import read_private_config


async def burst(kernel, roles, directory, host_window=None):
    limit = asyncio.Semaphore(2)
    outbound = asyncio.Semaphore(host_window) if host_window else None
    gate = asyncio.Event()
    ready = asyncio.Event()
    rows = []
    arrived = 0
    zero = None
    clients = psutil.Process()
    async with replicas(roles, 1, directory=directory) as (ports, cold):
        url = f"http://127.0.0.1:{ports[0]}/mcp"
        server = next(p for p in clients.children() if 'benchmarks.p59_mcp_remote' in p.cmdline())

        async def bind(i, role):
            nonlocal arrived
            record = dict(role=i, book=i % 2, initialized=False, http=[], status="INIT_FAILED")
            boundary = False

            async def sending(req):
                if gate.is_set() and not boundary:
                    record["http"].append(dict(send_ms=(perf_counter()-zero)*1000))

            async def received(response):
                if gate.is_set() and not boundary and record["http"]:
                    record["http"][-1].update(status=response.status_code, header_ms=(perf_counter()-zero)*1000)
            token = read_private_config(role.path)["token"]
            await limit.acquire()
            released = False
            try:
                async with httpx.AsyncClient(headers={"Authorization": "Bearer "+token}, trust_env=False, timeout=30,
                                             event_hooks={"request":[sending], "response":[received]}) as client:
                    async with streamable_http_client(url, http_client=client) as streams:
                        async with ClientSession(streams[0], streams[1]) as session:
                            init = await session.initialize()
                            names = {x.name for x in (await session.list_tools()).tools}
                            assert init.protocolVersion == "2025-11-25" and len(names) == 4 and streams[2]() is None
                            limit.release()
                            released = True
                            record["initialized"] = True
                            arrived += 1
                            if arrived == len(roles):
                                ready.set()
                            await gate.wait()
                            if outbound is not None:
                                await outbound.acquire()
                            try:
                                record["call_ms"] = (perf_counter()-zero)*1000
                                async with asyncio.timeout(30):
                                    result = await session.call_tool("nf_task_get", {})
                            finally:
                                if outbound is not None:
                                    outbound.release()
                            if result.isError:
                                record["status"] = (result.structuredContent or {}).get("error", {}).get("code", "MCP_ERROR")
                            else:
                                record["status"] = "OK" if result.structuredContent == role.expected["nf_task_get"] else "WRONG_CONTENT"
                            record["end_ms"] = (perf_counter()-zero)*1000
                            boundary = True
            except (Exception, asyncio.CancelledError) as exc:
                record["error_type"] = type(exc).__name__
                if not boundary:
                    if any(h.get("status") == 429 for h in record["http"]):
                        record["status"] = "HTTP_429"
                    elif record["initialized"]:
                        record["status"] = type(exc).__name__
                    if zero is not None:
                        record["end_ms"] = (perf_counter()-zero)*1000
                boundary = True
            finally:
                if not released:
                    limit.release()
                    arrived += 1
                    if arrived == len(roles):
                        ready.set()
                rows.append(record)
        work = [asyncio.create_task(bind(i,r)) for i,r in enumerate(roles)]
        await ready.wait()
        samples = []
        def snapshot():
            with kernel.db.uow(write=False) as repo:
                connections, locks = repo.conn.execute(text(
                    "SELECT count(*), count(*) FILTER (WHERE wait_event_type='Lock') "
                    "FROM pg_stat_activity WHERE application_name LIKE 'nf_stdio_load_%' AND datname=current_database()")).one()
            def detail(process):
                cpu = process.cpu_times()
                return dict(cpu_s=cpu.user+cpu.system,rss=process.memory_info().rss,threads=process.num_threads())
            return dict(ms=(perf_counter()-zero)*1000, client=detail(clients), server=detail(server), pg=connections, locks=locks)
        zero = perf_counter()
        samples.append(snapshot())
        gate.set()
        done = asyncio.gather(*work)
        while not done.done():
            samples.append(await asyncio.to_thread(snapshot))
            await asyncio.sleep(.1)
        await done
        samples.append(snapshot())
    elapsed = max((r.get("end_ms", 0) for r in rows), default=0)/1000
    counts = Counter(r["status"] for r in rows)
    return dict(rows=sorted(rows,key=lambda r:r["role"]), states=counts, cold_s=cold, samples=samples,
                host_window=host_window,
                initialized=sum(r["initialized"] for r in rows), replica_count=1, server_session_ids=0,
                correct_per_s=counts["OK"]/elapsed if elapsed else 0, drain_s=elapsed,
                correct_per_book=[sum(r["status"]=="OK" and r["book"]==i for r in rows) for i in (0,1)])


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--population",type=int,choices=(10,200,500,1000),required=True)
    p.add_argument("--output",type=Path,required=True)
    p.add_argument("--host-window", type=int, choices=(1, 2),
                   help="After the simultaneous release, cap actual SDK calls across all role sessions")
    args=p.parse_args()
    logging.disable(logging.CRITICAL)
    with tempfile.TemporaryDirectory() as temp, isolated_kernel(os.environ["NF_TEST_DATABASE_URL"],Path(temp)/"objects") as (kernel,_):
        directory=Path(temp)
        books=[load_fixture(kernel) for _ in range(2)]
        roles=[provision(kernel,directory,i,world=books[i%2],lease_seconds=3600) for i in range(args.population)]
        assert len({r.principal_id for r in roles})==args.population
        data=asyncio.run(burst(kernel,roles,directory,args.host_window))
        with kernel.db.uow(write=False) as repo:
            heads=[b["world_ref"] for b in books]
            data["heads_unchanged"]=all(kernel.head(repo,h["work_id"],h["branch_id"])==h for h in heads)
            data["attempts"]=repo.conn.scalar(text("SELECT count(*) FROM nf_call_attempts"))
        data.update(population=args.population,git_sha=subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip(),
                    driver_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
        with gzip.open(args.output,"wt") as out:
            json.dump(data,out,separators=(",",":"))
        print(args.population,data["states"],data["correct_per_s"],flush=True)
        if data["initialized"]!=args.population or not data["heads_unchanged"] or data["attempts"] or data["states"].get("WRONG_CONTENT"):
            raise SystemExit("Setup or semantic check failed")


if __name__ == "__main__":
    main()
