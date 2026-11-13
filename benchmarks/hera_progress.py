"""Compare authorized end-to-end workflow progress at a fixed, populated branch."""
import argparse
import asyncio
from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta
import gzip
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import text

from benchmarks.hera_mixed import task_spec
from benchmarks.http_load import loopback_server, percentiles, SqlMeasurements
from benchmarks.p2_behavioral_load import isolated_kernel, hardware
from novel_factory.application.kernel import Kernel
from novel_factory.application.mcp_runtime import McpRuntime, ProjectBinding
from novel_factory.fixtures import load_fixture
from novel_factory.infrastructure.database import Database
from novel_factory.interfaces.http import create_app, HttpSettings
from novel_factory.orchestration.progress import configure_progress


async def measure(kernel, wr, actor, expected, count):
    w,b = wr["work_id"],wr["branch_id"]
    app = create_app(kernel, settings=HttpSettings(rate_burst=10000), mcp_bindings={actor["principal"]:ProjectBinding(w,b)})
    path = f"/api/v1/works/{w}/branches/{b}/workflow"
    with loopback_server(app,lifespan="on") as base:
        async with AsyncExitStack() as stack:
            clients = []
            for i in range(4):
                c = await stack.enter_async_context(httpx.AsyncClient(base_url=base,headers={"Authorization":"Bearer "+actor["token"]},trust_env=False))
                if i%2:
                    wires=await stack.enter_async_context(streamable_http_client(base+"/mcp",http_client=c))
                    sdk=await stack.enter_async_context(ClientSession(*wires[:2]))
                    await sdk.initialize()
                    await sdk.list_tools()
                    clients.append(sdk)
                else:
                    clients.append(c)
            rows = []
            async def worker(index,c):
                for i in range(index,count,len(clients)):
                    started=perf_counter()
                    if index%2:
                        res=await c.call_tool("nf_project_status",{})
                        ok=(not res.isError and res.structuredContent==expected)
                    else:
                        res=await c.get(path)
                        ok=res.is_success and res.json()==expected
                    rows.append((i, "mcp" if index%2 else "http", ok, perf_counter()-started))
            with SqlMeasurements(kernel.db.engine) as measurements:
                started = perf_counter()
                await asyncio.gather(*(worker(i,c) for i,c in enumerate(clients)))
                elapsed=perf_counter()-started
            return dict(goodput=sum(r[2] for r in rows)/elapsed,seconds=elapsed,verified=sum(r[2] for r in rows),
                        latency=percentiles([r[3] for r in rows]), sql=measurements.result(elapsed), raw=sorted(rows))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--tasks",type=int,default=5000)
    p.add_argument("--reads",type=int,default=128)
    p.add_argument("--output",required=True,type=Path)
    args=p.parse_args()
    if not 1 <= args.tasks <= 5000 or not 4 <= args.reads <= 1000:
        p.error("invalid experiment size")
    with TemporaryDirectory() as tmp, isolated_kernel(os.environ["NF_TEST_DATABASE_URL"],Path(tmp)) as (setup,url):
        setup.db.close()
        kernel=Kernel(Database(url,pool_size=4),setup.objects)
        try:
            book=load_fixture(kernel)
            w,b=book["world_ref"]["work_id"],book["world_ref"]["branch_id"]
            creator=kernel.issue_grant(book["principal"],w,role_id="writer")["principal"]
            actor=kernel.issue_grant(book["principal"],w,role_id="resume_operator",capabilities=["PROJECT_RESUME"],
                                     branch_ids=[b],expires_at=datetime.now(UTC)+timedelta(hours=1))
            # Seed existing task generations and receipts with the regular write path.
            for i in range(args.tasks):
                spec=task_spec(creator,book["world_ref"])
                kernel.create_task(creator,spec,"progress-"+str(i))
            expected=McpRuntime(kernel,actor["principal"],ProjectBinding(w,b)).project()
            assert expected["ready_count"]==args.tasks
            with kernel.db.uow() as repo:
                configure_progress(repo.conn,w,b,enabled=True)
            results=[]
            for mode in "ABBA":
                os.environ["NF_HERA_PROJECT_PROGRESS"]="1" if mode=="B" else "0"
                row=asyncio.run(measure(kernel,book["world_ref"],actor,expected,args.reads))
                row.update(mode=mode)
                results.append(row)
            with kernel.db.uow(write=False) as repo:
                size=repo.conn.scalar(text("SELECT pg_total_relation_size('nf_hera_progress')"))
            with gzip.open(args.output,"wt") as out:
                json.dump(dict(environment=hardware(),tasks=args.tasks,projection_bytes=size,trials=results),out,separators=(",",":"))
            print([(r["mode"],r["verified"],r["goodput"],r["latency"]) for r in results])
        finally:
            kernel.db.close()


if __name__=="__main__":
    main()
