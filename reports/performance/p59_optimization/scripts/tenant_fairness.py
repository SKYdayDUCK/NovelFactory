"""Four equally weighted ready queues compete for one real admission slot."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import sys
import tempfile
from threading import Barrier, Lock
import time
from sqlalchemy import text

from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.kernel import Kernel
from novel_factory.application.model_routing import activate
from novel_factory.application.supervisor import RuntimeSupervisor
from novel_factory.fixtures import load_fixture
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.objects import ObjectStore
from test_p5_supervisor import _job, backend
from test_p58_routing import policy

def run(out):
    db=Database(pool_size=8,max_overflow=0)
    rows=[]
    quotas=[]
    try:
        with tempfile.TemporaryDirectory() as tmp, backend(concurrent=True) as (adapter,sent,server):
            kernel=Kernel(db,ObjectStore(tmp))
            gateway=AgentGateway(kernel,{"one":adapter})
            sup=RuntimeSupervisor(kernel,gateway)
            worlds=[]
            for i in range(4):
                world=load_fixture(kernel)
                wr=world["world_ref"]
                activate(kernel,world["principal"],wr,policy(world),gateway.adapters,expected_revision=0)
                budget=kernel.tasks.create_budget(wr["work_id"],"2")
                for job in range(3):
                    _job(kernel,gateway,sup,world,budget,"READY",adapter_id="@policy")
                worlds.append(world)
            with db.uow() as r:
                rows_set=r.conn.execute(text("UPDATE nf_dispatch_limits SET slots=1 WHERE scope='GLOBAL' AND subject='*'")).rowcount
                assert rows_set==1
            entered=Barrier(4)
            add=Lock()
            def drain(i):
                w=worlds[i]
                wr=w["world_ref"]
                entered.wait(timeout=10)
                start=time.monotonic()
                count=0
                idle=0
                while count<3 and time.monotonic()-start<90:
                    res=sup.admit_model(w["principal"],wr["work_id"], wr["branch_id"])
                    if res["status"]=="IDLE":
                        idle+=1
                        time.sleep(.02)
                        continue
                    assert res["status"]=="RESULT_STORED"
                    count+=1
                    with add:
                        rows.append({"project":i,"ordinal":count,"task_id":res["task_id"],"elapsed_ms":1000*(time.monotonic()-start),"idle_returns_before_completion":idle})
                    idle=0
                return count
            with ThreadPoolExecutor(4) as pool:
                completed=list(pool.map(drain,range(4)))
            with db.uow(write=False) as r:
                for w in worlds:
                    quotas.append(r.conn.scalar(text("SELECT virtual_finish FROM nf_runtime_work_share WHERE work_id=:w"),{"w":w["world_ref"]["work_id"]}))
            out.write_text(json.dumps({"projects":4,"ready_per_project":3,"global_slots":1,"local_http_sends":len(sent),"provider_inference_calls":0,"completions":completed,"virtual_finish":list(map(str,quotas)),"rows":rows},indent=2)+"\n")
            assert completed==[3,3,3,3] and len(sent)==12
            assert all(int(x)==3 for x in quotas)
    finally:
        db.close()
if __name__=="__main__":
    run(Path(sys.argv[1]))
