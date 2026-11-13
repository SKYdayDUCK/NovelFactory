"""Independent live role grants, same complete nf_task_get oracle as P5.9 fan-in."""
import argparse
import asyncio
from collections import Counter
from contextlib import AsyncExitStack
import gzip
import json
import logging
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import text
from benchmarks.bound_stdio_throughput import provision
from benchmarks.http_load import loopback_server, percentiles
from benchmarks.p2_behavioral_load import isolated_kernel, hardware
from novel_factory.application.kernel import Kernel
from novel_factory.infrastructure.database import Database
from novel_factory.fixtures import load_fixture
from novel_factory.interfaces.bindings import parse_binding, read_private_config
from novel_factory.interfaces.http import HttpSettings, create_app


async def actor_burst(kernel, roles, *, shared=False):
    roster, tokens = {}, []
    for role in roles:
        cfg=read_private_config(role.path)
        roster[kernel.authenticate(cfg["token"])]=parse_binding(cfg["binding"])
        tokens.append(cfg["token"])
    # Split two physical connections into one auth and one active operation.
    # The historical experiment allowed both SQL workers on either phase.
    settings=HttpSettings(max_inflight_requests=64,max_queued_ingress=1500,max_queued_operations=1024,
                          queue_timeout_seconds=180,compact_remote_role_reads=True,share_auth_slots=shared)
    app=create_app(kernel,mcp_bindings=roster,settings=settings)
    gate, ready, init=asyncio.Event(),asyncio.Queue(),asyncio.Semaphore(4)
    rows=[]
    started=0.0
    async def lane(i,base):
        initialized=False
        status="INIT_FAILURE"
        try:
            async with AsyncExitStack() as stack:
                async with init:
                    c=await stack.enter_async_context(httpx.AsyncClient(headers={"Authorization":"Bearer "+tokens[i]},
                        trust_env=False,timeout=240, limits=httpx.Limits(max_connections=2,max_keepalive_connections=1,
                                                                        keepalive_expiry=300)))
                    wires=await stack.enter_async_context(streamable_http_client(base+"/mcp",http_client=c))
                    sdk=await stack.enter_async_context(ClientSession(*wires[:2]))
                    await sdk.initialize()
                    assert {t.name for t in (await sdk.list_tools()).tools} == {"nf_task_get","nf_context_get","nf_query_batch","nf_artifact_put"}
                initialized=True
                ready.put_nowait(i)
                await gate.wait()
                result=await sdk.call_tool("nf_task_get",{})
                status=("OK" if not result.isError and result.structuredContent==roles[i].expected["nf_task_get"]
                        else (result.structuredContent or {}).get("error",{}).get("code","WRONG_PAYLOAD"))
                elapsed=perf_counter()-started
        except Exception as exc:
            if not initialized:
                ready.put_nowait(i)
            await gate.wait()
            status,elapsed=type(exc).__name__,perf_counter()-started
        rows.append([i,initialized,status,elapsed])
    with loopback_server(app,lifespan="on",keepalive=300) as base:
        async with asyncio.TaskGroup() as g:
            for i in range(len(roles)):
                g.create_task(lane(i,base))
            for _ in roles:
                await asyncio.wait_for(ready.get(),120)
            started=perf_counter()
            gate.set()
    return dict(rows=sorted(rows),latency=percentiles([r[3] for r in rows]),outcomes=dict(Counter(r[2] for r in rows)),
                drain_s=max(r[3] for r in rows),within_30s=sum(r[2]=="OK" and r[3]<=30 for r in rows),
                initialized=sum(r[1] for r in rows),admission=app.state.admission.snapshot(),ingress=app.state.ingress.snapshot())


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--clients",type=int,required=True)
    p.add_argument("--output",type=Path,required=True)
    p.add_argument("--order",choices=["A","ABBA"],default="A")
    args=p.parse_args()
    if not 2<=args.clients<=1000:
        p.error("use 2..1000 clients")
    logging.getLogger("mcp").setLevel(logging.WARNING)
    with TemporaryDirectory() as tmp, isolated_kernel(os.environ["NF_TEST_DATABASE_URL"],Path(tmp)/"objects") as (setup,url):
        books=[load_fixture(setup) for _ in range(2)]
        roles=[provision(setup,Path(tmp),i,world=books[i%2],lease_seconds=3600) for i in range(args.clients)]
        assert len({r.principal_id for r in roles})==args.clients
        setup.db.close()
        kernel=Kernel(Database(url,pool_size=2,max_overflow=0),setup.objects)
        try:
            for i, mode in enumerate(args.order):
                data=asyncio.run(actor_burst(kernel,roles,shared=mode=="B"))
                with kernel.db.uow(write=False) as repo:
                    data["unchanged_heads"]=all(kernel.head(repo,b["world_ref"]["work_id"],b["world_ref"]["branch_id"])==b["world_ref"] for b in books)
                    data["attempts"]=repo.conn.scalar(text("SELECT count(*) FROM nf_call_attempts"))
                data.update(environment=hardware(), shared_auth_slots=mode=="B")
                path=args.output if args.order=="A" else args.output.parent/f"actor-{args.clients}-{i}{mode}.json.gz"
                with gzip.open(path,"wt") as out:
                    json.dump(data,out,separators=(",",":"))
                print(mode, {k:v for k,v in data.items() if k not in ("environment","rows")},flush=True)
        finally:
            kernel.db.close()


if __name__=="__main__":
    main()
