"""Execute the identical ordinary HTTP lifecycle from either frozen checkout."""
import argparse
import asyncio
from collections import Counter
from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta
import gzip
import hashlib
import json
import os
from pathlib import Path
import resource
import subprocess
from tempfile import TemporaryDirectory
from time import perf_counter, process_time
from uuid import NAMESPACE_DNS, uuid5

import httpx
from sqlalchemy import text

from benchmarks.http_load import SqlMeasurements, loopback_server, percentiles
from benchmarks.p2_behavioral_load import isolated_kernel
from novel_factory.application.kernel import Kernel
from novel_factory.fixtures import load_fixture
from novel_factory.infrastructure.database import Database
from novel_factory.interfaces import http


async def run(app, fixtures, kernel):
    records, workflows = [], []
    async def lane(principal, base, identity, wr, idx, start):
        async with httpx.AsyncClient(base_url=base, headers={"Authorization": "Bearer " + identity["token"]},
                                     trust_env=False, timeout=30) as client:
            assert (await client.get("/health")).status_code == 200
            await ready.put(idx)
            await start.wait()
            for j in range(4):
                tid = str(uuid5(NAMESPACE_DNS, f"hera-rest-compare:{idx}:{j}"))
                spec = {"schema_version": "2.0", "task_id": tid, "generation": 0, "role_id": "writer",
                        "task_kind": "hera-comparable", "state": "READY", "world_ref": wr, "story_time":{"tick":0,"phase":0},
                        "context_bundle_id":None,"policy_version":"nf-policy-1","dependency_fingerprint":None,
                        "runtime_contract_version":"1"*64,"capability_grant_id":principal.grant_id,"input_artifacts":[],
                        "budget":{"currency":"USD","max_amount":"1","token_budget":10},
                        "wall_deadline":deadline}
                create = {"spec":spec,"idempotency_key":"create-"+tid}
                track = (("create","/api/v1/tasks",create),("ready","/api/v1/tasks/"+tid,None),
                         ("replay","/api/v1/tasks",create),("claim","/api/v1/tasks/claim",
                          {"worker_id":"w-"+tid,"task_id":tid,"lease_seconds":300}),
                         ("running","/api/v1/tasks/"+tid,None),("cancel","/api/v1/tasks/cancel",
                          {"task_id":tid,"generation":1,"idempotency_key":"cancel-"+tid}),
                         ("cancelled","/api/v1/tasks/"+tid,None))
                original = None
                all_ok = True
                began = perf_counter()
                for name, path, payload in track:
                    t = perf_counter()
                    try:
                        response = (await client.get(path) if payload is None else await client.post(path,json=payload))
                        body = response.json()
                        status = str(response.status_code)
                        good = status == "200" and isinstance(body,dict) and body.get("task_id") == tid
                        if name == "replay":
                            good = good and original == body
                        elif name == "create":
                            original = body
                            good = good and body.get("state") == "READY" and body.get("generation") == 0
                        elif name == "cancel":
                            good = good and body.get("status") == "CANCELLED" and body.get("generation")==1
                        else:
                            state = {"ready":"READY","claim":"RUNNING","running":"RUNNING","cancelled":"CANCELLED"}[name]
                            good = good and body.get("state")==state and body.get("world_ref")==wr and body.get("generation")==int(name!="ready")
                    except Exception as error:
                        status,good = type(error).__name__,False
                    end = perf_counter()
                    records.append([tid,idx,j,name,status,good,t-epoch,end-epoch])
                    all_ok = all_ok and good
                workflows.append([tid,all_ok,began-epoch,end-epoch])
    start, ready = asyncio.Event(), asyncio.Queue()
    deadline=(datetime.now(UTC)+timedelta(hours=1)).isoformat()
    with loopback_server(app) as base:
        async with asyncio.TaskGroup() as tasks:
            for idx,(identity,wr) in enumerate(fixtures):
                tasks.create_task(lane(identity["principal"],base,identity,wr,idx,start))
            for _ in fixtures:
                await asyncio.wait_for(ready.get(),60)
            with SqlMeasurements(kernel.db.engine) as meter:
                cpu,epoch=process_time(),perf_counter()
                start.set()
                while len(workflows)<64:
                    await asyncio.sleep(.005)
                elapsed=max(r[3] for r in workflows)
                sql,cpu=meter.result(elapsed),process_time()-cpu
    correct=sum(row[1] for row in workflows)
    return {"requests":records,"cycles":workflows,"verified_http":sum(r[5] for r in records),
            "verified_cycles":correct,"seconds":elapsed,"cycle_goodput_s":correct/elapsed,
            "http_goodput_s":sum(r[5] for r in records)/elapsed,"cpu_s":cpu,
            "statuses":dict(Counter(r[4] for r in records)),"peak_rss_kib":resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            "request_latency":percentiles([r[7]-r[6] for r in records]),"cycle_latency":percentiles([r[3]-r[2] for r in workflows]),
            "per_operation":{name:percentiles([r[7]-r[6] for r in records if r[3]==name]) for name in sorted({r[3] for r in records})},
            "sql":sql,"admission":app.state.admission.snapshot(),"auth":app.state.authentication_admission.snapshot()}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arm",choices=("A","B"),required=True)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    root=Path.cwd().resolve()
    assert Path(http.__file__).resolve().is_relative_to(root/"src")
    sha=subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip()
    assert not subprocess.check_output(["git","status","--porcelain"]) and sha.startswith("bcb03637" if args.arm=="A" else "0fcf453")
    with TemporaryDirectory() as tmp, isolated_kernel(os.environ["NF_TEST_DATABASE_URL"],Path(tmp)) as (original,url):
        original.db.close()
        db=Database(url,pool_size=4,max_overflow=0)
        kernel=Kernel(db,original.objects)
        try:
            books=[load_fixture(kernel),load_fixture(kernel)]
            fixtures=[]
            for i in range(16):
                book=books[i%2]
                wr=book["world_ref"]
                grant=kernel.issue_grant(book["principal"],wr["work_id"],role_id="writer",branch_ids=[wr["branch_id"]])
                fixtures.append((grant,wr))
            configuration={"max_inflight_requests":128,"max_queued_operations":128,
                           "rate_per_second":2000,"rate_burst":2000,"queue_timeout_seconds":60}
            if args.arm=="B":
                configuration["fenced_task_status"]=True
                with db.uow() as repo:
                    repo.conn.execute(text("UPDATE nf_durable_admission_policy SET enabled=true"))
            app=http.create_app(kernel,settings=http.HttpSettings(**configuration))
            assert app.state.admission.capacity == app.state.authentication_admission.capacity == 2
            r=asyncio.run(run(app,fixtures,kernel))
            with db.uow(write=False) as repo:
                rows=repo.conn.execute(text("""
                    SELECT t.task_id::text, t.state,t.generation,
                      (SELECT count(*) FROM nf_task_executions e WHERE e.task_id=t.task_id),
                      (SELECT count(*) FROM nf_outbox o WHERE o.aggregate_id=t.task_id::text AND o.topic='task.changed'),
                      (SELECT count(*) FROM nf_task_commands c WHERE c.receipt->>'task_id'=t.task_id::text)
                    FROM nf_tasks t ORDER BY t.task_id
                """)).all()
                r["db_oracle"]=[list(row) for row in rows]
                r["domain_correct"]=len(rows)==64 and all(tuple(row[1:])==("CANCELLED",1,1,3,2) for row in rows)
                r["world_heads_unchanged"]=all(kernel.head(repo,b["world_ref"]["work_id"],b["world_ref"]["branch_id"])==b["world_ref"] for b in books)
                r["model_attempts"]=repo.conn.scalar(text("SELECT count(*) FROM nf_call_attempts"))
                r["postgres"]=repo.conn.scalar(text("SHOW server_version"))
            r.update(sha=sha,arm=args.arm,options=configuration,request_fields=["task","lane","cycle","op","status","ok","sent_s","end_s"],
                     cycle_fields=["task","ok","start_s","end_s"],driver_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
            args.output.parent.mkdir(parents=True,exist_ok=True)
            with gzip.open(args.output,"wt",encoding="utf8") as out:
                json.dump(r,out,separators=(",",":"))
            print(args.arm,r["verified_http"],r["verified_cycles"],r["domain_correct"],round(r["cycle_goodput_s"],2),r["cycle_latency"],flush=True)
            assert r["verified_http"]==448 and r["verified_cycles"]==64 and r["domain_correct"] and r["world_heads_unchanged"] and not r["model_attempts"]
        finally:
            db.close()


if __name__=="__main__":
    main()
