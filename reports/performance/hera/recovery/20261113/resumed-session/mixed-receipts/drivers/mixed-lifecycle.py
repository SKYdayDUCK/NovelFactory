"""Alternate true REST and MCP client sessions over six idempotent task operations from a shared start."""
import argparse
import asyncio
from contextlib import AsyncExitStack
from datetime import UTC, datetime
import gzip
import hashlib
import importlib.util
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from time import perf_counter
from uuid import UUID
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import text
from benchmarks.p2_behavioral_load import isolated_kernel
from novel_factory.application.task_operations import TaskOperationsBinding
from standard_http_load import percentiles
from mixed_support import population, serve, task_spec

async def measure(base, actors, count, size, ratio):
    setup_gate=asyncio.Semaphore(8)
    ready=asyncio.Queue()
    start=asyncio.Event()
    specs=[task_spec(actors[i%count][0]["principal"],actors[i%count][1]) for i in range(size)]
    rows=[]
    active=peak=0
    times=[]
    async def lane(i):
        nonlocal active,peak
        async with AsyncExitStack() as stack:
            mcp=i*7%10<ratio/10
            async with setup_gate:
                client=await stack.enter_async_context(httpx.AsyncClient(base_url=base,
                    headers={"Authorization":"Bearer "+actors[i][0]["token"]},trust_env=False,timeout=180))
                if mcp:
                    rd,wr,_=await stack.enter_async_context(streamable_http_client(base+"/mcp",http_client=client))
                    sdk=await stack.enter_async_context(ClientSession(rd,wr))
                    assert (await sdk.initialize()).protocolVersion=="2025-11-25"
                else:
                    assert (await client.get("/health")).status_code==200
            await ready.put(i)
            await start.wait()
            for j in range(i,size,count):
                spec=specs[j]; tid=spec["task_id"]; body={"spec":spec,"idempotency_key":"c-"+tid}
                payload={"task_id":tid,"generation":0,"idempotency_key":"x-"+tid}
                ops=[("create","nf_task_submit","/api/v1/tasks",body),("ready","nf_task_status","/api/v1/tasks/"+tid,None),
                     ("replay","nf_task_submit","/api/v1/tasks",body),("cancel","nf_task_cancel","/api/v1/tasks/cancel",payload),
                     ("cancel_replay","nf_task_cancel","/api/v1/tasks/cancel",payload),("terminal","nf_task_status","/api/v1/tasks/"+tid,None)]
                first=None; cancelled=None; cycle_start=perf_counter(); passed=True
                for op,name,path,pay in ops:
                    t0=perf_counter(); active+=1; peak=max(peak,active)
                    try:
                        if mcp:
                            r=await sdk.call_tool(name,pay if pay is not None else {"task_id":tid}); val=r.structuredContent or {}
                            code="error" if r.isError else "OK"
                        else:
                            r=await client.post(path,json=pay) if pay is not None else await client.get(path)
                            val=r.json(); code="OK" if r.status_code==200 else str(r.status_code)
                        ok=code=="OK" and val.get("task_id")==tid and val.get("generation")==0
                        if op in ("ready","terminal"):
                            ok &= val.get("world_ref")==spec["world_ref"] and val.get("state")==("READY" if op=="ready" else "CANCELLED")
                        elif op=="create":
                            first=val;ok &= val.get("state")=="READY"
                        elif op=="replay": ok &= val==first
                        elif op=="cancel":
                            cancelled=val;ok &= val.get("status")=="CANCELLED" and bool(UUID(val["receipt_id"]))
                        elif op=="cancel_replay": ok &= val==cancelled
                    except Exception as exc:
                        code,ok=type(exc).__name__,False
                    finally: active-=1
                    end=perf_counter()
                    passed &= ok
                    rows.append([i,j,tid,mcp,op,code,ok,t0-epoch,end-epoch])
                times.append([j,tid,passed,cycle_start-epoch,perf_counter()-epoch])
    async with asyncio.TaskGroup() as tg:
        for i in range(count): tg.create_task(lane(i))
        for _ in range(count): await asyncio.wait_for(ready.get(),240)
        epoch=perf_counter();start.set()
    valid=sum(c[2] for c in times)
    return {"request_fields":["lane","logical_task","task_id","is_mcp","operation","code","valid","start","end"],
            "cycles":times,"rows":rows,"correct_cycles":valid,"goodput":valid/max(r[-1] for r in times),
            "latency":percentiles([r[-1]-r[-2] for r in rows]),"peak_inflight":peak, "live_clients":count,
            "mcp_clients":sum(i*7%10<ratio/10 for i in range(count)),"offered":size}
def main():
    p=argparse.ArgumentParser();p.add_argument("--output",required=True,type=Path);p.add_argument("--sizes",type=int,nargs="+",default=[200])
    p.add_argument("--percent",type=int,nargs="+",default=[0,20,50,80,100]);p.add_argument("--clients",type=int,default=20)
    p.add_argument("--server-cpu",type=int,required=True)
    a=p.parse_args();a.output.mkdir(parents=True,exist_ok=True)
    with TemporaryDirectory() as tmp,isolated_kernel(os.environ["NF_TEST_DATABASE_URL"],Path(tmp)) as (k,url):
        actors=population(k,a.clients); bindings={g["principal"]:TaskOperationsBinding(w["work_id"],w["branch_id"]) for g,w in actors}
        k.db.close();ctx=multiprocessing.get_context("spawn");parent,child=ctx.Pipe();srv=ctx.Process(target=serve,args=(child,url,tmp,bindings,[a.server_cpu]));srv.start();child.close()
        try:
            assert parent.poll(60);base=parent.recv()
            for size in a.sizes:
                for ratio in a.percent:
                    parent.send("snapshot");before=parent.recv()
                    r=asyncio.run(measure(base,actors,a.clients,size,ratio))
                    parent.send("snapshot");after=parent.recv()
                    ids=[c[1] for c in r["cycles"]];heads=[wr for _,wr in actors[:2]]
                    with k.db.uow(write=False) as repo:
                        states=repo.conn.execute(text("""SELECT t.task_id::text,t.state,t.generation,
                        (SELECT count(*) FROM nf_task_executions x WHERE x.task_id=t.task_id),
                        (SELECT count(*) FROM nf_task_commands c WHERE c.receipt->>'task_id'=t.task_id::text),
                        (SELECT count(*) FROM nf_outbox o WHERE o.aggregate_id=t.task_id::text AND o.topic='task.changed')
                        FROM nf_tasks t WHERE t.task_id=ANY(CAST(:ids AS uuid[]))"""),{"ids":ids}).all()
                        attempts=repo.conn.scalar(text("SELECT count(*) FROM nf_call_attempts"))
                        unchanged=all(k.head(repo,w["work_id"],w["branch_id"])==w for w in heads)
                    db_valid=(len(states)==size and set(ids)=={x[0] for x in states} and all(tuple(x[1:])==("CANCELLED",0,0,2,2) for x in states) and unchanged and attempts==0)
                    r.update({"source":subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip(),"server_before":before,"server_after":after,"oracle":[list(x) for x in states],"world_heads_unchanged":unchanged,"attempts":attempts,
                              "verified":db_valid and r["correct_cycles"]==size,"workers":0,"model_slots":0,"db_pool":4,"percent":ratio,"driver_sha256":hashlib.sha256(Path(__file__).read_bytes()).hexdigest()})
                    out=a.output/f"n{size}-c{a.clients}-p{ratio}.json.gz"
                    with gzip.open(out,"wt") as fp: json.dump(r,fp,separators=(",",":"))
                    print(out.name,r["correct_cycles"],round(r["goodput"],3),r["verified"],flush=True)
        finally:
            if srv.is_alive(): parent.send("stop")
            srv.join(30)
            if srv.is_alive(): srv.kill();srv.join()
            parent.close()
if __name__=="__main__": main()
