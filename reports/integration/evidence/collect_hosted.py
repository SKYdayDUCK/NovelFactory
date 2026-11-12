"""Read an explicit Actions request from the evidence branch and stream bounded, screened receipts."""
import base64
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import zipfile
import xml.etree.ElementTree as ET

REPO = "SKYdayDUCK/NovelFactory"
BASE = f"repos/{REPO}"
WORKFLOW = sys.argv[1]
REQUEST = Path("reports/integration/evidence/collector-request.json")
VERIFIER = "33a64afa1f63092301f4975fd97e0a233ba00d29"
P55_CAP = {"p2-load.yml", "p3-p4-evidence.yml", "p3-scale-experiments.yml"}
PLAIN = {
    "p6-offline-checks": {"checkout.txt", "preflight.log", "preflight-unconfigured.json", "release-checks.xml"},
    "p6-shared-token-recovery": {"result.json"},
    "workshop-results": {"workshop-results.xml", "dual-results.xml", "dual-service-results.xml", "paired-restore.json", "w3-redis-recovery.json", "aw-offline.json", "codex-dual/receipt.json"},
    "workshop-scene-recovery-results": {"scene-results.xml", "capacity-results.xml"},
    "workshop-comparisons": set(), "nf-bridge-results": {"dual-service.xml", "nf-delegation.xml"},
}
AUTHOR = {"author-receipt.json", "trace.json", "release.txt", "reader-A.md", "reader-B.md"}
FIXTURE = {"release.txt": "12c1d512f5bad133466c9164354207642237c62349f4231fcaeb3049d2135781",
           "reader-A.md": "620fffee4e19a0a513d7ab20f5a47915be0bf71cc90885f7dd19a37272d09f24",
           "reader-B.md": "061c6f3da3e8daca87fa69664ed258a1e8f5cbe07e637e0350c953096b25c290"}
SENSITIVE = re.compile(r"""(?ix)(?:postgres(?:ql)?(?:\+\w+)?|rediss?|https?)://[^/\s:@]+:[^\s/@]+@
    |-----BEGIN[ A-Z]*PRIVATE\sKEY-----|\b(?:ghp_|github_pat_|sk-)[A-Za-z0-9_-]{15,}
    |\bbearer\s+[A-Za-z0-9._~+/-]{10,}|[?&](?:sig|X-Amz-Signature)=
    |["'](?:token|password|api_key|binding_secret|database_url|private_key|access_token)["']\s*:\s*["'][^"']+
    |\b(?:PGPASSWORD|NF_MODEL_API_KEY|GH_TOKEN|GITHUB_TOKEN)\s*=\s*[^*\s]+""")
TRANSFERRED = 0


def check(condition, why):
    if not condition:
        raise ValueError(why)


def api(path):
    return json.loads(subprocess.check_output(["gh", "api", BASE + "/" + path]))


def sha(data):
    return hashlib.sha256(data).hexdigest()


def encoded(data):
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()


def emit(key, data):
    """Raw digest always survives. Larger screened members use deterministic gzip on the wire."""
    global TRANSFERRED
    check(re.fullmatch(r"[A-Za-z0-9_./-]+", key) and ".." not in key, "bad output key")
    source = sha(data)
    if len(data) > 1024 * 1024 or os.environ["GH_TOKEN"].encode() in data or SENSITIVE.search(data.decode("utf-8")):
        print("NF_DIGEST", key, len(data), source, "UNTRANSFERRED")
        return
    wire = gzip.compress(data, mtime=0) if len(data) > 16384 else data
    name = key + ".gz" if wire is not data else key
    payload = base64.b64encode(wire).decode("ascii")
    TRANSFERRED += len(payload)
    check(TRANSFERRED <= 850000, "transfer cap")
    chunks = [payload[i:i+5000] for i in range(0, len(payload), 5000)] or [""]
    for i, chunk in enumerate(chunks):
        print("NF_RECEIPT_V1", name, len(data), source, sha(wire), i+1, len(chunks), chunk)


def author(z):
    files = {n.split("/",1)[1]: z.read(n) for n in z.namelist() if n.startswith("p6-author/") and not n.endswith("/")}
    check(files.keys() == AUTHOR, "author evidence membership")
    doc = json.loads(files["author-receipt.json"])
    check(doc["execution_kind"] == "TEST_FIXTURE" and doc["real_model_calls"] == doc["human_reviews_received"] == 0,
          "author evidence class")
    check(doc["literary_acceptance"] == "NOT_EVALUATED" and doc["fixture_calls"] == 15, "author evidence boundary")
    check(doc["sha256"] == {n: sha(data) for n,data in files.items() if n != "author-receipt.json"}, "author manifest mismatch")
    for n, digest in FIXTURE.items():
        check(sha(files[n]) == digest, "fixture/form bytes differ: "+n)
    allowed = {"execution_kind","real_model_calls","fixture_calls","fixture_cost","literary_acceptance","human_reviews_received","author_status","export","trace_id","trace_event_types","sha256"}
    check(doc.keys() == allowed and doc["export"].keys() == {"release_ref","manifest_sha256","text_sha256","bytes","scene_count","external_delivery"}, "author fields")
    check(doc["export"]["release_ref"].keys() == {"artifact_id","artifact_revision_id","sha256"} and doc["export"]["text_sha256"] == FIXTURE["release.txt"]
          and doc["export"]["scene_count"] == 6 and doc["export"]["external_delivery"] == "NOT_REQUESTED", "author release identity")
    rows = json.loads(files["trace.json"])
    check(rows and all(row["trace_id"] == doc["trace_id"] and row["event_type"] == "selection.release_freeze" for row in rows), "unexpected author trace")
    for row in rows:
        for key,value in row.items():
            if value in (None, "", {}):
                continue
            if key in {"work_id","branch_id","world_revision_id","principal_id","event_id","evidence_id","trace_id","span_id"}:
                check(re.fullmatch(r"[a-f0-9-]{36}", str(value)), "unexpected trace identifier")
            elif key == "event_time":
                check(re.fullmatch(r"[0-9T: .+Z-]+", str(value)), "unexpected trace time")
            else:
                check(key == "event_type", "unexpected public trace field")
    for n,data in files.items():
        emit("p6-author/"+n, data)


def selected(name):
    if name in PLAIN:
        return PLAIN[name]
    if re.fullmatch(r"p1-schema-unit-\d+", name):
        return {"unit.xml","schema-validation.log"}
    if re.fullmatch(r"p1-postgres-concurrency-\d+", name):
        return {"postgres-all.xml","migration.log","r1-acceptance.json"}
    if re.fullmatch(r"p4-real-pitr-\d+", name):
        return {"pitr/result.json"}
    return set()


def capacity(z, idx):
    metrics = ("profile logical_agent_task_count engineer_correctness first_attempt_capacity_gate capacity_gate "
               "bounded_recovery_business_gate offered_wire_rate_gate correctness_errors world_unchanged new_world_commits "
               "scheduled_requests completed_requests status_counts validated_successes observed_wire_start_rps "
               "successful_query_rps probe_gate long_duration_gate duration_seconds scheduled_count actual_call_start_rps").split()
    result = []
    for n in z.namelist():
        if n.split("/")[-1] not in {"result.json","load.json","context-p3.json"}:
            continue
        raw = z.read(n)
        d = json.loads(raw)
        row = {k:d[k] for k in metrics if k in d}
        row.update(member=n, member_sha256=sha(raw))
        if "environment" in d:
            row["recorded_checkout"] = d["environment"].get("git_sha")
        row["metrics"] = {k:v for k,v in d.get("metrics",{}).items() if k in
                          ("planned_requests","completed_requests","first_attempt_status_counts","status_counts","valid_successful_calls","api_success_rps")}
        row["offered_first_attempts"] = {k:v for k,v in d.get("open_loop_offered_rate",{}).items()
                                        if k in ("gate","target_rps","first_attempts","actual_call_start_rps","transport_header_starts","transport_header_start_rps")}
        result.append(row)
    emit(f"capacity/{idx}.json", encoded(result))


def main():
    check(os.environ["GITHUB_REPOSITORY"] == REPO and os.environ["GITHUB_REF"] == "refs/heads/evidence/serial-delivery", "wrong collector scope")
    req = json.loads(REQUEST.read_bytes())
    check(req["schema"] == 1 and req["repository"] == REPO and req["event"] in {"pull_request","push"}, "request scope")
    if WORKFLOW not in req["runs"]:
        print("Workflow absent from this request")
        return
    head, checkout, tree = (req[k] for k in ("head","checkout","tree"))
    check(all(re.fullmatch(r"[a-f0-9]{40}", s) for s in (head,checkout,tree,req["base"])), "git ids")
    git = api("git/commits/"+checkout)
    check(git["sha"] == checkout and git["tree"]["sha"] == tree and git["parents"][0]["sha"] == req["base"], "checkout identity")
    if req["event"] == "pull_request":
        check(git["parents"][1]["sha"] == head and api("git/commits/"+head)["tree"]["sha"] == tree, "PR synthetic merge")
    else:
        check(head == checkout, "main checkout")
    wanted = req["runs"][WORKFLOW]
    rid = wanted["id"]
    check(type(rid) is int, "run ID")
    run = api(f"actions/runs/{rid}")
    check(run["id"] == rid and run["repository"]["full_name"] == REPO and run["path"] == ".github/workflows/"+WORKFLOW
          and run["head_sha"] == head and run["event"] == req["event"] and run["status"] == "completed", "run identity")
    check(req["event"] != "push" or run["head_branch"] == "main", "main branch")
    jobs = api(f"actions/runs/{rid}/jobs?filter=all&per_page=100")
    meta = api(f"actions/runs/{rid}/artifacts?per_page=100")
    check(len(jobs["jobs"]) == jobs["total_count"] and all(j["run_id"] == rid and j["head_sha"] == head for j in jobs["jobs"]), "job list")
    check(len(meta["artifacts"]) == meta["total_count"], "artifact pagination")
    official = {a["id"]:a for a in meta["artifacts"]}
    check(sorted(official) == sorted(a["id"] for a in wanted["artifacts"]), "requested artifact set changed")
    facts = {"repository":REPO,"workflow":WORKFLOW,"run_id":rid,"attempt":run["run_attempt"],"head":head,"checkout":checkout,"tree":tree,"event":run["event"],"conclusion":run["conclusion"],"jobs":[{"id":j["id"],"name":j["name"],"attempt":j["run_attempt"],"conclusion":j["conclusion"],"failed_steps":[s["name"] for s in j["steps"] if s["conclusion"]=="failure"]} for j in jobs["jobs"]],"artifacts":[]}
    with tempfile.TemporaryDirectory() as temp:
        local = Path(temp)
        archives = local/"archives"
        archives.mkdir()
        for a in wanted["artifacts"]:
            idx = a["id"]
            b = official[idx]
            name = a["name"]
            check(re.fullmatch(r"[A-Za-z0-9_.-]+",name) and b["name"]==name and b["digest"]==a["digest"] and not b["expired"]
                  and b["workflow_run"]["head_sha"]==head and b["workflow_run"]["id"]==rid and b["size_in_bytes"]<=100_000_000, "artifact identity")
            fn=archives/(name+".zip")
            with fn.open("wb") as stream:
                subprocess.run(["gh","api",BASE+f"/actions/artifacts/{idx}/zip"],stdout=stream,check=True)
            with fn.open("rb") as source:
                digest = hashlib.file_digest(source, "sha256").hexdigest()
            check(fn.stat().st_size == b["size_in_bytes"] and "sha256:"+digest == b["digest"], "original ZIP differs")
            facts["artifacts"].append({"id":idx,"name":name,"sha256":digest})
            with zipfile.ZipFile(fn) as z:
                check(len(set(z.namelist()))==len(z.namelist()) and all(m.file_size<=80_000_000 for m in z.infolist()), "ZIP shape")
                if WORKFLOW in P55_CAP:
                    capacity(z,idx)
                for n in sorted(selected(name)):
                    if n in z.namelist():
                        raw = z.read(n)
                        emit(name+"/"+n,raw)
                        if n in {"postgres-all.xml","unit.xml","dual-results.xml","release-checks.xml"}:
                            doc = ET.fromstring(raw)
                            suites = [doc] if doc.tag=="testsuite" else list(doc.iter("testsuite"))
                            totals = {k:sum(int(x.get(k,0)) for x in suites) for k in ("tests","failures","errors","skipped")}
                            nodes = [{"module":t.get("classname"),"test":t.get("name"),"result":"FAIL" if t.find("failure") is not None or t.find("error") is not None else "SKIP" if t.find("skipped") is not None else "PASS"} for t in doc.iter("testcase") if "test_p6_" in (t.get("classname") or "") or "test_engineering_release_ci" in (t.get("classname") or "")]
                            emit(name+"/"+n+"-summary.json",encoded({"raw_sha256":sha(raw),"totals":totals,"p6_nodes":nodes}))
                            if n=="postgres-all.xml":
                                values = [json.loads(x.get("value")) for x in doc.iter("property") if x.get("name")=="p6.register_only"]
                                if values:
                                    check(len(values)==1,"registration property multiplicity")
                                    emit("p6-register-only.json",encoded(values[0]))
                if re.fullmatch(r"p1-postgres-concurrency-\d+",name) and any(n.startswith("p6-author/") for n in z.namelist()):
                    author(z)
        if WORKFLOW == "agent-workshop.yml" and run["conclusion"] == "success":
            for n,obj in (("run",run),("jobs",jobs),("artifacts",meta)):
                (local/(n+".json")).write_bytes(encoded(obj))
            checker = base64.b64decode(api("git/blobs/"+VERIFIER)["content"])
            check(hashlib.sha1(b"blob "+str(len(checker)).encode()+b"\0"+checker).hexdigest()==VERIFIER,"archived checker changed")
            (local/"verify_run.py").write_bytes(checker)
            result = subprocess.run([sys.executable,str(local/"verify_run.py"),"--metadata-dir",str(local),"--archives",str(archives),"--checkout-sha",checkout],
                                    cwd=local,env={"PATH":os.defpath},capture_output=True,timeout=90)
            check(result.returncode == 0 and json.loads(result.stdout)["status"] == "PASS", "independent Workshop verifier")
            emit("workshop-qualifier.json",result.stdout)
        emit("workflow-proof.json",encoded(facts))


if __name__ == "__main__":
    main()
