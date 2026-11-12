"""Isolated contract experiment. All candidate functions exist only in this harness."""
from collections import Counter
from contextlib import contextmanager
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import statistics
import sys
import tempfile
import time

from benchmarks.context_profile import make_world,prepare
from novel_factory.application.kernel import Kernel
from novel_factory.application.queries import WorldQueryService
from novel_factory.contracts import registry
from novel_factory.domain.canonical import fingerprint
from novel_factory.domain.errors import DomainError
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.objects import ObjectStore

BASE=registry.validate_contract

def native_tree(x):
    t=type(x)
    if t is dict:
        return all(type(k) is str and native_tree(v) for k,v in x.items())
    if t is list:
        return all(native_tree(v) for v in x)
    return x is None or t in (str,int,bool,float)

def first_error(name,payload,version="2.0"):
    value=registry.plain(payload)
    if not registry._validator(name,version).is_valid(value):
        raise DomainError("INVALID_ARGUMENT",f"Invalid {name}: contract constraints failed")
    return deepcopy(value)

def guarded_copy(name,payload,version="2.0"):
    value=registry.plain(payload)
    errors=sorted(registry._validator(name,version).iter_errors(value),key=lambda e:str(list(e.path)))
    if errors:
        raise DomainError("INVALID_ARGUMENT",f"Invalid {name}: contract constraints failed")
    return value if native_tree(payload) else deepcopy(value)

ARMS={"BASELINE":BASE,"FIRST_ERROR":first_error,"NATIVE_COPY":guarded_copy}
@contextmanager
def swap_aliases(replacement):
    edits=[]
    for key,module in tuple(sys.modules.items()):
        if key.startswith("novel_factory.") and module is not None:
            for name,fn in tuple(vars(module).items()):
                if fn is BASE:
                    setattr(module,name,replacement)
                    edits.append((module,name))
    try:
        yield
    finally:
        for mod,name in edits:
            setattr(mod,name,BASE)

def outcome(fn,name,value,version):
    try:
        return ("OK",fn(name,value,version))
    except DomainError as e:
        return (e.code,e.safe_message)

def named_fixtures():
    path=Path("contracts/examples")
    out=[]
    for index,version in [("index.json","2.0"),("r1_index.json","2.1"),("literary_index.json","literary")]:
        meta=json.loads((path/index).read_text())
        for filename,name in meta["examples"].items():
            out.append((filename,name,json.loads((path/filename).read_text()),version))
        for filename,name in meta.get("invalid_examples",{}).items():
            if isinstance(name,dict):
                name=name.get("definition",name.get("contract"))
            out.append((filename,name,json.loads((path.parent/"invalid_examples"/filename).read_text()),version))
    for filename,spec in json.loads((path.parent/"invalid_examples/core_index.json").read_text()).items():
        out.append((filename,spec["definition"],json.loads((path.parent/"invalid_examples"/filename).read_text()),"2.0"))
    return out

def run(out):
    db=Database(pool_size=4)
    timings=[]
    checked=[]
    saved={}
    for filename,name,val,version in named_fixtures():
        baseline=outcome(BASE,name,val,version)
        assert all(outcome(fn,name,val,version)==baseline for fn in ARMS.values())
        checked.append({"fixture":filename,"name":name,"version":version,"status":baseline[0]})
    sample=json.loads(Path("contracts/examples/query_known_none.json").read_text())
    sample["value_status"]="VALUE"
    sample["data"]=bytearray(b"nf")
    plain_value=registry.plain(sample)
    assert registry._validator("QueryAnswer","2.0").is_valid(plain_value)
    assert plain_value["data"] is sample["data"]
    for fn in ARMS.values():
        copied=fn("QueryAnswer",sample)
        assert copied["data"]==sample["data"] and copied["data"] is not sample["data"]
    native=deepcopy(sample);native["data"]={"nested":[1,2]}
    for fn in ARMS.values():
        copy=fn("QueryAnswer",native)
        copy["data"]["nested"].append(3)
        assert native["data"]=={"nested":[1,2]}
    with tempfile.TemporaryDirectory() as temp:
        kernel=Kernel(db,ObjectStore(temp))
        world,items,_=make_world(kernel,250)
        service=WorldQueryService(kernel)
        for count in (1,100):
            reference=None
            for rep in range(9):
                names=list(ARMS)
                order=names[rep%3:]+names[:rep%3]
                for arm in order:
                    req=prepare(kernel,world,items[:count])
                    with swap_aliases(ARMS[arm]):
                        t=time.perf_counter_ns();cpu=time.thread_time_ns()
                        result=service.context_compile(world["principal"],req)
                        wall=time.perf_counter_ns()-t; used=time.thread_time_ns()-cpu
                    compiled=result["compiled"]
                    assert len(compiled["bundle"]["facts"])==count
                    evidence={"facts":compiled["bundle"]["facts"],"limitations":compiled["bundle"]["limitations"],"permissions":compiled["bundle"]["permitted_evidence_refs"],"role_id":compiled["bundle"]["role_id"],"cut":compiled["read_cut"]["world_ref"],"tokens":compiled["estimated_tokens"],"hard":compiled["hard_requirements_status"]}
                    digest=fingerprint(evidence)
                    if reference is None: reference=digest
                    assert digest==reference
                    saved[count]=compiled
                    timings.append({"case":"full_compile","subjects":count,"repeat":rep,"arm":arm,"wall_ms":wall/1e6,"cpu_ms":used/1e6,"normalized_evidence":digest})
    corpus={"valid_small":saved[1],"valid_large":saved[100]}
    for label,index in (("invalid_early",0),("invalid_late",-1)):
        broken=deepcopy(saved[100])
        broken["bundle"]["facts"][index]["status"]="MALFORMED_FIXTURE"
        corpus[label]=broken
    for name,value in corpus.items():
        gold=outcome(BASE,"CompiledContext",value,"2.1")
        assert (gold[0]=="OK")==name.startswith("valid")
        for arm,fn in ARMS.items():assert outcome(fn,"CompiledContext",value,"2.1")==gold
        for rep in range(15):
            order=list(ARMS);order=order[rep%3:]+order[:rep%3]
            for arm in order:
                start=time.perf_counter_ns();cpu=time.thread_time_ns()
                result=outcome(ARMS[arm],"CompiledContext",value,"2.1")
                wall=time.perf_counter_ns()-start;used=time.thread_time_ns()-cpu
                assert result==gold
                timings.append({"case":name,"subjects":1 if name=="valid_small" else 100,"repeat":rep,"arm":arm,"wall_ms":wall/1e6,"cpu_ms":used/1e6})
    db.close()
    cases={}
    for row in timings:
        group=f"{row['case']}-{row['subjects']}"
        cases.setdefault(group,{}).setdefault(row["arm"],[]).append(row["wall_ms"])
    summary={k:{arm:statistics.median(vals) for arm,vals in arms.items()} for k,arms in cases.items()}
    out.write_text(json.dumps({"baseline_sha":"5e322e8af8ddb4465f68f8cab84c9685832461a9","product_changes":0,"model_calls":0,"fixture_results":checked,"timings":timings,"median_ms":summary,"isolated_input_mutation_tests":"native nested JSON data, opaque bytearray under unconstrained QueryAnswer.data","unguarded_plain_result_aliases_mutable_leaf":True,"guarded_copy_and_first_error_results_equal":True,"e2e_note":"Three rotated arm orders. Each full compile receives a new actual task at one fixed world cut. Task-bound IDs differ; all complete facts, evidence, role, permitted refs, hard-requirements status, token estimate, world reference and limitations match within a size."},indent=2)+"\n")
if __name__=="__main__":
    run(Path(sys.argv[1]))
