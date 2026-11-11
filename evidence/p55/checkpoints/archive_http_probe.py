"""Store a fixed synthetic two-arm result without runtime paths or credentials."""
import collections
import gzip
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

source = Path("download")
dest = Path("receipt/evidence/p55/http-validation")
dest.mkdir(parents=True, exist_ok=True)
arms = {"baseline": "55d8979b5361a4837701d762feacd83036096b11", "candidate": "df30c306057d26b7b32e3cec0df1ad9926a6824a"}
registration = json.loads((source / "query-comparison.json").read_text())
assert all(registration[k] == v for k, v in arms.items())
assert registration["cases"] == [{"agents":500,"seconds":600,"read_rps":20,"api_processes":2}]
(dest / "registration.json").write_text(json.dumps(registration, indent=2) + "\n")

junit = (source / "candidate-queries.xml").read_bytes()
suites = list(ET.fromstring(junit).iter("testsuite"))
assert suites and all(s.get("errors") == s.get("failures") == s.get("skipped") == "0" for s in suites)
assert any("checks_its_full_contract_once" in c.get("name","") for c in ET.fromstring(junit).iter("testcase"))
assert not re.search(rb'Bearer [a-zA-Z0-9._-]+|postgres(?:ql)?[^ ]*://[^ ]+:[^ ]+@|-----BEGIN .* PRIVATE KEY', junit)
(dest / "candidate-junit.xml").write_bytes(junit)
allowed = {"index","agent","kind","started_seconds","arrival_delay_ms","valid","status","code","latency_ms","correctness_error","error_type"}
summaries = {}
for arm, expected in arms.items():
    assert (source / (arm+"-sha.txt")).read_text().strip() == expected
    raw = (source / (arm+".json")).read_bytes()
    report = json.loads(raw)
    assert report["configuration"]["agents"] == 500 and report["configuration"]["seconds"] == 600
    assert report["configuration"]["read_rps"] == 20 and report["configuration"]["api_processes"] == 2
    assert report["new_world_commits"] == report["real_model_calls"] == 0
    rows = report["records"]
    assert len(rows) == report["scheduled_count"] == 12000
    assert len({r["index"] for r in rows}) == 12000
    for r in rows:
        assert r.keys() <= allowed
        assert all(type(v) in (int,float,bool,str,type(None)) for v in r.values())
        assert all(type(v) is not str or re.fullmatch(r"[a-zA-Z0-9_.:-]{0,100}",v) for v in r.values())
    assert dict(collections.Counter(str(r["status"]) for r in rows)) == report["status_counts"]
    compressed = gzip.compress(json.dumps(rows,separators=(",", ":")).encode(), mtime=0)
    (dest / (arm+"-records.json.gz")).write_bytes(compressed)
    summary = {k:report[k] for k in ("scheduled_count","duration_seconds","actual_call_start_rps","successful_query_rps","latency_all_attempts","status_counts","world_unchanged","new_world_commits","real_model_calls","probe_gate","long_duration_gate")}
    summary.update(sha=expected, original_exit=int((source / (arm+"-exit.txt")).read_text()),
                   correctness_error_count=len(report["correctness_errors"]), source_result_sha256=hashlib.sha256(raw).hexdigest(),
                   retained_records_sha256=hashlib.sha256(compressed).hexdigest(),
                   cpu_models=report["environment"].get("cpu_models"),initial_load_average=report["environment"].get("load_average"),
                   service_resources=report["service_final"].get("resources"),sql_execution_seconds=report["service_final"]["database"].get("sql_cursor_execution_seconds"))
    summaries[arm] = summary
record = {"run":43041082878, "order":["baseline","candidate"], "arms":summaries, "acceptance":"original exit codes; one ordered pair"}
(dest / "comparison.json").write_text(json.dumps(record, indent=2) + "\n")
