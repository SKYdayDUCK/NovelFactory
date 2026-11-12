"""Paired benchmark for runtime/routing.py. Run from a candidate repo at the tested path."""
import argparse
import copy
import hashlib
import json
import math
from pathlib import Path
import platform
from statistics import median
import subprocess
import time
from types import ModuleType


def module(name, source):
    obj = ModuleType(name)
    exec(compile(source, name, "exec"), obj.__dict__)
    return obj


def observe(mod, policy, actor, override):
    try:
        return ["return", mod.resolve(policy, actor, override=override)]
    except mod.RoutingError as exc:
        return ["error", str(exc)]


def stats(samples):
    ordered = sorted(samples)
    return {f"p{p}_us": ordered[math.ceil(len(ordered)*p/100)-1] for p in (50, 95, 99)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="5e322e8af8ddb4465f68f8cab84c9685832461a9")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    filename = "agent_runtime/routing.py"
    baseline = subprocess.check_output(["git", "show", args.base + ":" + filename])
    candidate = Path(filename).read_bytes()
    impl = {"A": module("baseline_routing", baseline), "B": module("candidate_routing", candidate)}
    scope = dict(work_id="novel", branch_id="main", installation_id="site", org_id="studio")
    actor = dict(task_id="current", instance_id="author-1", role_id="writer", task_kind="scene_draft")
    production = json.loads(Path("reports/p6_readiness/templates/routing-rules.json").read_text())
    inputs = {"operator_template": production}
    for n in (1,16,256):
        inputs[f"rules_{n}"] = [{"id": "default", "when": {}, "profile": "fallback"}] + [
            {"id": f"role-{i}", "when": {"role_id": f"writer-{i}"}, "profile": "dedicated"} for i in range(n-1)]
    sources = {name: hashlib.sha256(data).hexdigest() for name, data in (("A",baseline), ("B",candidate))}
    out = {"base_sha":args.base, "module_hashes":sources, "python":platform.python_version(),
           "schedule":"ABBA BAAB, 256 timed calls per block and 12 warmups",
           "timings":{}, "equivalence_checks":0}
    subject = {**scope, **actor}
    levels = impl["A"].LEVELS
    all_rules = [{"id": f"level-{i}", "profile": f"chosen-{i}", "when":{k:subject[k] for k in level}}
                 for i,level in enumerate(levels)]
    checks = [(all_rules[i:], actor, override) for i in range(len(levels)) for override in (None,"operator")]
    for when in ({"unexpected":"writer"}, {"role_id": "writer", "work_id":"novel"}, {"role_id": ""}):
        checks.append(([all_rules[-1],{"id":"bad","profile":"fixed","when":when}],actor,None))
    checks.extend([([],actor,None),([all_rules[-1],all_rules[-1]],actor,None)])
    for rules in inputs.values():
        for name in ("writer","reviewer","volume_director","missing","writer-3"):
            checks.append((rules,{**actor,"role_id":name},None))
    for rules, identity, override in checks:
        pol = {**scope, "revision":2, "rules":rules}
        assert observe(impl["A"],pol,identity,override) == observe(impl["B"],pol,identity,override)
        out["equivalence_checks"] += 1
    for workload,rules in inputs.items():
        pol = {**scope, "revision":2, "rules":rules}
        actors = [{**actor,"role_id": role} for role in ("writer","reviewer","volume_director","missing")]
        if workload!="operator_template":
            actors += [{**actor,"role_id":f"writer-{i}"} for i in range(len(rules))]
        blocks = []
        for label in "ABBABAAB":
            resolve = impl[label].resolve
            for i in range(12):
                resolve(pol,actors[i % len(actors)])
            us=[]
            for i in range(256):
                identity = actors[i%len(actors)]
                start = time.perf_counter_ns()
                resolve(pol,identity)
                us.append((time.perf_counter_ns()-start)/1000)
            blocks.append({"implementation":label,"samples_us":us,**stats(us)})
        out["timings"][workload] = {"blocks":blocks, "pooled":{
            label:stats([x for row in blocks if row["implementation"]==label for x in row["samples_us"]])
            for label in ("A","B")}}
    args.output.write_text(json.dumps(out)+"\n")
    print(json.dumps({k:v["pooled"] for k,v in out["timings"].items()},indent=2))


if __name__ == "__main__":
    main()
