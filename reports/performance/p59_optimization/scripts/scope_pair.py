"""Paired real PG17 measurements of scope updates; identical state before each arm."""
import argparse
from collections import Counter
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import platform
import statistics
import subprocess
import tempfile
import time
from unittest.mock import patch

from sqlalchemy import text
from benchmarks.context_profile import SQLMeter, make_world
from novel_factory.application.kernel import Kernel
from novel_factory.domain.canonical import fingerprint
from novel_factory.fixtures import entity_value, point, prepare_command
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.objects import ObjectStore
from novel_factory.infrastructure.repository import KernelRepository

BASE = "5e322e8af8ddb4465f68f8cab84c9685832461a9"
SRC = "src/novel_factory/infrastructure/repository.py"

class Rollback(Exception):
    pass

def run(out):
    db=Database(pool_size=4)
    meter=SQLMeter(db.engine)
    rows=[]
    with tempfile.TemporaryDirectory() as tmp:
        baseline=Path(tmp)/"baseline.py"
        baseline.write_bytes(subprocess.check_output(["git","show",BASE+":"+SRC]))
        spec=importlib.util.spec_from_file_location("old_scope_repo",baseline)
        module=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        arms={"BASELINE":module.KernelRepository.bump,"CANDIDATE":KernelRepository.bump}
        kernel=Kernel(db,ObjectStore(Path(tmp)/"objects"))
        def pair(bench,count,repeat,invoke):
            order=["BASELINE","CANDIDATE"] if repeat%2==0 else ["CANDIDATE","BASELINE"]
            witnesses=[]
            for arm in order:
                meter.reset()
                with patch.object(KernelRepository,"bump",arms[arm]):
                    start=time.perf_counter_ns()
                    cpu=time.thread_time_ns()
                    witness=invoke()
                    wall=time.perf_counter_ns()-start
                    used=time.thread_time_ns()-cpu
                digest=fingerprint(witness)
                witnesses.append(digest)
                rows.append({"bench":bench,"size":count,"repeat":repeat,"arm":arm,"wall_ms":wall/1e6,"thread_cpu_ms":used/1e6,"sql":meter.count,"sql_seconds":meter.seconds,"witness_sha256":digest})
            assert witnesses[0]==witnesses[1]
        for size in [5,100]:
            world,items,owner=make_world(kernel,size)
            wr=world["world_ref"]
            work,branch=wr["work_id"],wr["branch_id"]
            with db.uow(write=False) as r:
                original=r.scope_map(work,branch)
            if size==5:
                for keys_count in [0,1,4,64,256]:
                    keys=([next(iter(original))]+["TEST_SCOPE:"+str(i) for i in range(max(0,keys_count-1))])[:keys_count]
                    def isolated():
                        answer=None
                        try:
                            with db.uow() as r:
                                answer=r.bump(work,branch,[*keys,*reversed(keys)])
                                raise Rollback()
                        except Rollback:
                            return answer
                    for i in range(10):
                        pair("scope_transaction_rollback",keys_count,i,isolated)
            req=prepare_command(kernel,world["principal"],wr,"SET_RELATION",{"subject_id":items[0],"predicate":"legal_owner","object_value":entity_value(owner),"at":point(3)},key="p59-frozen")["request"]
            original_add=KernelRepository.add
            def acceptance():
                evidence={}
                def capture(repo,table,values,**kwargs):
                    if table=="revisions":
                        evidence.update({k:values[k] for k in ["selection","frontiers","scope_versions","state_hash","manifest_hash","branch_seq","interpretation_ref"]})
                    return original_add(repo,table,values,**kwargs)
                def fault(phase):
                    if phase=="before_commit":
                        raise Rollback()
                try:
                    with patch.object(KernelRepository,"add",capture):
                        kernel.accept(world["principal"],req,fault=fault)
                except Rollback:
                    assert evidence
                    return evidence
                raise AssertionError("guarded write unexpectedly committed")
            for i in range(12):
                pair("full_accept_before_commit_rollback",size,i,acceptance)
            with db.uow(write=False) as r:
                assert r.scope_map(work,branch)==original
                assert kernel.head(r,work,branch)==wr
            receipt=kernel.accept(world["principal"],req)
            assert kernel.accept(world["principal"],req)==receipt
        out.write_text(json.dumps({"base_sha":BASE,"baseline_source_sha256":sha256(baseline.read_bytes()).hexdigest(),"candidate_source_sha256":sha256(Path(SRC).read_bytes()).hexdigest(),"python":platform.python_version(),"real_provider_calls":0,"paid_charge":None,"samples":rows,"witnesses_equal":True,"same_input_replayed_after_rollback":True,"replay_receipts_equal":True},indent=2)+"\n")
    db.close()
if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("out",type=Path)
    run(parser.parse_args().out)
