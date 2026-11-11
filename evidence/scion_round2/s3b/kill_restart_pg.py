"""Kill an actual Kernel process before commit and restart on the same isolated PG17."""
import argparse
import contextlib
import gzip
import hashlib
import importlib
import itertools
import json
import os
from pathlib import Path
import select
import shutil
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
from datetime import datetime,timedelta,UTC
from unittest.mock import patch
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import create_engine,text
from sqlalchemy.engine import make_url
from novel_factory.application.authorization import Principal
from novel_factory.application.kernel import Kernel
from novel_factory.application.replay_proofs import ReplayProofs
from novel_factory.domain.canonical import fingerprint
from novel_factory.domain.world import replay
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.objects import ObjectStore
from novel_factory.infrastructure.repository import KernelRepository
from novel_factory.orchestration.tasks import TaskService
from novel_factory.fixtures import load_fixture, run_command, prepare_command, create_bound_task, point
from benchmarks.scion_paired import clone,drop
from benchmarks.p2_behavioral_load import isolated_kernel
from benchmarks.scion_profile import Attribution


def transfer(ids,tick):
    return {"from_account":ids["warehouse"],"to_account":ids["relief"],"resource_id":ids["grain"],
            "quantity":"1","unit":"bag","at":point(tick)}


def fixed_inputs(stack,now,phase):
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):
            return now.astimezone(tz) if tz else now.replace(tzinfo=None)
    def stream(name):
        serial=itertools.count()
        def new():
            return UUID(bytes=uuid5(NAMESPACE_URL,f"nf-prime-restart/{phase}/{name}/{next(serial)}").bytes,version=4)
        return new
    for name in ["novel_factory.application.kernel","novel_factory.application.queries","novel_factory.orchestration.tasks",
                 "novel_factory.fixtures"]:
        module=importlib.import_module(name)
        if hasattr(module,"uuid4"):stack.enter_context(patch.object(module,"uuid4",stream(name)))
        if getattr(module,"datetime",None) is datetime:stack.enter_context(patch.object(module,"datetime",Clock))
    def after_real_sql(original):
        def read(*args):
            original(*args)
            return now
        return read
    stack.enter_context(patch.object(KernelRepository,"now",after_real_sql(KernelRepository.now)))
    stack.enter_context(patch.object(TaskService,"_now",staticmethod(after_real_sql(TaskService._now))))


def child(phase,msg):
    k=Kernel(Database(msg["url"]),ObjectStore(msg["objects"]))
    k.scion.replay=k.scion.replay_lazy=msg["arm"]!="full"
    k.scion.replay_prime=msg["arm"]=="prime"
    k.replay_proofs=ReplayProofs(capacity=1)
    principal=Principal(**msg["principal"]); ids=msg["ids"]; stages=[]
    assert k.scion.counts()=={} and not k.replay_proofs._proofs and not k.replay_proofs._prime_hints
    now=datetime.fromisoformat(msg["clock"])+timedelta(seconds=int(phase=="after"))
    with contextlib.ExitStack() as stack:
        fixed_inputs(stack,now,phase)
        meter=stack.enter_context(Attribution(k))
        evicted=[]
        current_label=["setup"]
        original_store=k.replay_proofs._store_proof
        def observed_store(proof):
            # Single request per process in this fault trial; record actual
            # displaced fixed-content proof tuples, not guesses from misses.
            before=set(k.replay_proofs._proofs)
            original_store(proof)
            removed=before-set(k.replay_proofs._proofs)
            evicted.extend({"stage":current_label[0],"state_sha256":item[2]} for item in sorted(removed))
        stack.enter_context(patch.object(k.replay_proofs,"_store_proof",observed_store))
        def snapshot(label,start,cpu,before):
            values=meter.snapshot(); counts=k.scion.counts()
            return {"label":label,"wall_ms":(time.perf_counter()-start)*1000,"cpu_ms":(time.process_time()-cpu)*1000,
                    "sql":values["sql"]["statements"],"sql_rows":values["sql"]["selected_rows_reported_by_cursor"],
                    "decode_B":values["database_json_codec"].get("decode",{}).get("bytes",0),
                    "encode_B":values["database_json_codec"].get("encode",{}).get("bytes",0),
                    "full_replays":values["replay_event_counts"],
                    "counts":{key:n-before.get(key,0) for key,n in counts.items() if n>before.get(key,0)}}
        def measure(label,fn):
            current_label[0]=label
            meter.reset();before=k.scion.counts();meter.active=True
            start,cpu=time.perf_counter(),time.process_time()
            try:return fn()
            finally:
                meter.active=False;stages.append(snapshot(label,start,cpu,before));current_label[0]="setup"
        def current(ref):
            with k.db.uow(write=False,repeatable_read=True) as r:
                assert k.head(r,ref["work_id"])==ref
                row=k.revision(r,ref)
                prior=r.one("revisions",{"world_revision_id":row["parent_revision_id"],"work_id":ref["work_id"],
                                        "branch_id":ref["branch_id"]})
                dbrows=r.many("events",{"work_id":ref["work_id"]})
                all_events={x["event_version_id"]:x["payload"] for x in dbrows}
                new,=set(row["selection"].values())-set(prior["selection"].values())
                order=[*prior["selection"].values(),new]
                genesis=r.one("genesis",{"work_id":ref["work_id"]})["payload"]
                oracle=replay(genesis,[all_events[e] for e in order])
                assert row["snapshot"]==oracle and row["state_hash"]==fingerprint(oracle)
                return {"ref":ref,"state":row["state_hash"],"manifest":row["manifest_hash"],"events":len(row["selection"]),
                        "commits":r.conn.scalar(text("select count(*) from nf_commits"))}
        if phase=="before":
            first=measure("before.commit_confirmed",lambda:k.accept(principal,msg["confirmed_request"]))
            ref=first["world_ref"];before_state=current(ref)
            prepared=prepare_command(k,principal,ref,"RESOURCE_TRANSFER",transfer(ids,8),key="interrupt")
            fixed_tasks=[create_bound_task(k,principal,ref,point(10)) for _ in range(3)]
            handoff={"confirmed_receipt":first,"pending_request":prepared["request"],"before_state":before_state,
                     "tasks":[[t["task_id"],t["generation"]] for t in fixed_tasks]}
            current_label[0]="before.uncommitted_before_sigkill"
            meter.reset(); counts=k.scion.counts();meter.active=True;start,cpu=time.perf_counter(),time.process_time()
            def killpoint(stage):
                if stage=="before_commit":
                    meter.active=False
                    stages.append(snapshot("before.uncommitted_before_sigkill",start,cpu,counts))
                    print(json.dumps({"fault":"before_commit","pid":os.getpid(),"stages":stages,"evicted":evicted,"handoff":handoff}),flush=True)
                    while True:signal.pause()
            k.accept(principal,prepared["request"],fault=killpoint)
            raise AssertionError("parent failed to kill process")
        hand=msg["handoff"]; ref=hand["confirmed_receipt"]["world_ref"];req=hand["pending_request"]
        assert current(ref)==hand["before_state"]
        durable=measure("after.same_confirmed_receipt",lambda:k.accept(principal,msg["confirmed_request"]))
        assert durable==hand["confirmed_receipt"] and current(ref)==hand["before_state"] and not k.replay_proofs._proofs
        second=measure("after.retry_interrupted_request",lambda:k.accept(principal,req));new=second["world_ref"]
        assert measure("after.repeat_exact_receipt",lambda:k.accept(principal,req))==second
        final_state=current(new)
        assert final_state["commits"]==hand["before_state"]["commits"]+1 and final_state["events"]==hand["before_state"]["events"]+1
        def propose(task,label):
            return measure(label,lambda:k.propose(principal,*task,"RESOURCE_TRANSFER",transfer(ids,10),label))
        def make(ref):
            t=create_bound_task(k,principal,ref,point(10))
            return [t["task_id"],t["generation"]]
        # Successful replay of the retried commit seeded its new parent in the
        # one-entry cache. A different fixed historical parent evicts it on
        # a successful local prime. Revisit that same new parent and check costs.
        saved=[]
        saved.append(fingerprint(propose(make(new),"after.new_parent_before_eviction")))
        for i,task in enumerate(hand["tasks"]):
            saved.append(fingerprint(propose(task,"after.historical_"+str(i+1))))
        for i in range(3):
            saved.append(fingerprint(propose(make(new),"after.revisit_"+str(i+1))))
        assert current(new)==final_state
        print(json.dumps({"pid":os.getpid(),"stages":stages,"initial_empty_cache":True,"capacity":1,
                "entries_final":len(k.replay_proofs._proofs),"evicted":evicted,"totals":k.scion.counts(),
                "confirmed_receipt_sha":fingerprint(durable),"interrupted_request_sha":fingerprint(req),
                "recovered_receipt_sha":fingerprint(second),"final_state":final_state,"pinned_returns":saved}),flush=True)
    k.db.close()


def parent(output):
    url=os.environ["NF_TEST_DATABASE_URL"];admin=create_engine(make_url(url).set(database="postgres"),isolation_level="AUTOCOMMIT")
    result={"scenario":"SIGKILL during before_commit, separate OS process on same PG17 database, same object store; capacity=1",
            "cpu_affinity":sorted(os.sched_getaffinity(0)),"trials":{},"notes":"One sequential fault trial per arm; warm DB, distinct process caches, real SQL clock sampled then fixed for paired application timestamps. Engine observer excludes network and physical I/O. Partial interrupted timing ends at pre-commit hook."}
    with TemporaryDirectory() as temp,isolated_kernel(url,Path(temp)/"seed") as (seed,seed_url):
        w=load_fixture(seed,"white_bridge");ref=w["world_ref"]
        for idx in range(2,5):
            ref=run_command(seed,w["principal"],ref,"RESOURCE_TRANSFER",transfer(w["ids"],idx))["world_ref"]
        req=prepare_command(seed,w["principal"],ref,"RESOURCE_TRANSFER",transfer(w["ids"],6),key="durable")["request"]
        seed.db.close();clock=datetime.now(UTC).isoformat()
        for arm in ("full","lazy","prime"):
            name="nf_kill_"+uuid4().hex;clone(admin,name,make_url(seed_url).database)
            try:
                obj=shutil.copytree(Path(temp)/"seed",Path(temp)/name,copy_function=os.link)
                data={"url":make_url(url).set(database=name).render_as_string(hide_password=False),"objects":str(obj),
                      "clock":clock,"arm":arm,"principal":vars(w["principal"]),"ids":w["ids"],"confirmed_request":req}
                process=subprocess.Popen([sys.executable,__file__,"before"],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                                         stderr=None,text=True)
                try:
                    process.stdin.write(json.dumps(data));process.stdin.close()
                    assert select.select([process.stdout],[],[],150)[0],"precommit timeout"
                    line=process.stdout.readline(); assert line and process.poll() is None
                    old=json.loads(line);assert old["fault"]=="before_commit" and old["pid"]==process.pid
                    process.kill();process.wait(20);assert process.returncode == -signal.SIGKILL
                finally:
                    if process.poll() is None:process.kill();process.wait(20)
                data["handoff"]=old.pop("handoff")
                run=subprocess.run([sys.executable,__file__,"after"],input=json.dumps(data),capture_output=True,text=True,timeout=150)
                if run.returncode:sys.stderr.write(run.stderr);run.check_returncode()
                new=json.loads(run.stdout)
                assert new["pid"]!=old["pid"]
                result["trials"][arm]={"old":old,"signal_exit":process.returncode,"new":new,
                                        "confirmed_state_before_sigkill":data["handoff"]["before_state"]}
                print(arm,new["totals"],flush=True)
            finally:drop(admin,name)
    admin.dispose()
    baseline=result["trials"]["full"]["new"]
    for t in result["trials"].values():
        assert all(t["new"][k]==baseline[k] for k in ["confirmed_receipt_sha","interrupted_request_sha","recovered_receipt_sha","final_state","pinned_returns"])
    result["source_sha256"]={p:hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in [
                       "src/novel_factory/application/replay_proofs.py","src/novel_factory/application/kernel.py"]}
    result["script_sha256"]=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    output.write_text(json.dumps(result,indent=2)+'\n')

if __name__=='__main__':
    if sys.argv[1] in ('before','after'):child(sys.argv[1],json.load(sys.stdin))
    else:parent(Path(sys.argv[1]))
