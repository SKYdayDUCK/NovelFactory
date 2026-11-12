"""Fixed/mixed/failed local HTTP providers with 1, 2 and 4 independent works."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import json
import os
from pathlib import Path
import resource
import statistics
import tempfile
import time
from sqlalchemy import text

from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.model_routing import activate
from novel_factory.application.supervisor import RuntimeSupervisor
from novel_factory.application.kernel import Kernel
from novel_factory.fixtures import load_fixture
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.objects import ObjectStore
from test_p5_supervisor import _job, backend
from test_p58_routing import policy


def trial(kernel, count, mode):
    worlds=[load_fixture(kernel) for _ in range(count)]
    with ExitStack() as endpoints:
        one,sent_one,handler=endpoints.enter_context(backend(concurrent=True))
        two,sent_two,_=endpoints.enter_context(backend(concurrent=True))
        one.provider_key,one.account_key="p59-local-one","p59-one-account"
        two.provider_key,two.account_key="p59-local-two","p59-two-account"
        gateway=AgentGateway(kernel,{"one":one,"two":two})
        supervisor=RuntimeSupervisor(kernel,gateway)
        jobs=[]
        for i,w in enumerate(worlds):
            wr=w["world_ref"]
            route="one" if mode=="fixed" or i%2==0 else "two"
            activate(kernel,w["principal"],wr,policy(w,route),gateway.adapters,expected_revision=0)
            budget=kernel.tasks.create_budget(wr["work_id"],"1")
            job=_job(kernel,gateway,supervisor,w,budget,"READY",adapter_id="@policy")
            jobs.append((w,route,job))
        handler.lose_response=(mode=="failure")
        def execute(row):
            world,route,job=row
            wr=world["world_ref"]
            start=time.monotonic_ns()
            idle=0
            while True:
                status=supervisor.run_once(world["principal"],wr["work_id"],wr["branch_id"])["status"]
                if status!="IDLE" or time.monotonic_ns()-start>30_000_000_000:
                    break
                idle+=1
                time.sleep(.05)
            return {"route":route,"idle_checks":idle,"status":status,"latency_ms":(time.monotonic_ns()-start)/1e6,"task_id":job.task["task_id"],"work_id":wr["work_id"]}
        start=time.monotonic()
        with ThreadPoolExecutor(count) as pool:
            results=list(pool.map(execute,jobs))
        elapsed=time.monotonic()-start
        with kernel.db.uow(write=False) as repo:
            for row in results:
                attempts=repo.many("call_attempts",{"task_id":row["task_id"]})
                row["attempt_states"]=[r["state"] for r in attempts]
                row["attempt_count"]=len(attempts)
                assert len(attempts)==1
        assert len(sent_one)+len(sent_two)==count
        assert len(set(row["work_id"] for row in results))==count
        for row in results:
            if row["route"]=="one" and mode=="failure":
                assert row["attempt_states"]==["UNKNOWN"],row
            else:
                assert row["status"]=="RESULT_STORED",row
        return {"works":count,"mode":mode,"workers":count,"jobs":results,"elapsed_seconds":elapsed,"completed_tasks_per_second":sum(r["status"]=="RESULT_STORED" for r in results)/elapsed,"sent_by_endpoint":[len(sent_one),len(sent_two)],"max_rss_kib":resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}

def run(path):
    db=Database(pool_size=8,max_overflow=0)
    observations=[]
    try:
        with tempfile.TemporaryDirectory() as tmp:
            kernel=Kernel(db,ObjectStore(tmp))
            for n in [1,2,4]:
                for mode in ["fixed","mixed","failure"]:
                    observations.append(trial(kernel,n,mode))
                    path.write_text(json.dumps({"pg":"17","application_source": "frozen main","mode":"per-project authorized competing supervisors; local synthetic HTTP; failure endpoint returns 503 after send","provider_inference_calls":0,"actual_charge":None,"trials":observations},indent=2)+"\n")
    finally:
        db.close()
if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("output",type=Path)
    run(p.parse_args().output)
