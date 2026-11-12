"""Hash original evidence, check compressed round trips, and detect credential-shaped output."""
import argparse
import gzip
from hashlib import sha256
import json
from pathlib import Path
import re
ROOT=Path(__file__).resolve().parents[1]
def run():
    entries=[]
    snapshot_hashes=set()
    gate_results=[]
    markers={
        "private_key":rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
        "provider_token":rb"\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{30,}|gh[pousr]_[A-Za-z0-9]{32,})\b",
        "bearer_value":rb"Authorization.{0,12}Bearer [A-Za-z0-9_.-]{16,}",
        "password_in_dsn":rb"postgres(?:ql)?(?:\+psycopg)?://[^\s/:]+:[^\s/@]+@",
    }
    for path in sorted(ROOT.rglob("*")):
        rel=path.relative_to(ROOT).as_posix()
        if not path.is_file() or rel=="EVIDENCE_MANIFEST.json" or "__pycache__" in path.parts:
            continue
        data=path.read_bytes()
        record={"path":rel,"size":len(data),"sha256":sha256(data).hexdigest()}
        original=gzip.decompress(data) if path.suffix==".gz" else data
        if path.suffix==".gz":
            record.update(original_bytes=len(original),original_sha256=sha256(original).hexdigest())
        if rel.startswith("raw/"):
            for label,pattern in markers.items():
                if re.search(pattern,original):
                    raise ValueError(f"credential-shaped material: {label} in {rel}")
            if "source_snapshots" in path.parts:
                assert sha256(original).hexdigest()==path.stem
                snapshot_hashes.add(path.stem)
            if path.name=="load.json.gz":
                raw=json.loads(original)
                outside=raw.get("real_model_calls",raw.get("external_model",{}).get("actual_calls"))
                if outside not in (None,0):
                    raise ValueError(f"unexpected provider activity: {rel}")
                gate_results.append({"path":rel,"gate":raw.get("capacity_gate",raw.get("probe_gate")),"process_exit":json.loads((path.parent/"status.json").read_text())["exit_code"]})
            if path.name=="samples.json.gz":
                assert all(set(row).issubset({"operation","test","wall_ms","thread_cpu_ms","sql_cursor_calls","outcome","max_rss_kib"}) for row in json.loads(original)["samples"])
        entries.append(record)
    manifest={"frozen_sha":"5e322e8af8ddb4465f68f8cab84c9685832461a9","candidate_scope_commit":json.loads((ROOT/"environment.json").read_text())["candidate_scope_commit"],"manifest_scope":"Relative to performance evidence root; excludes this manifest itself. Compressed assets include a second checksum for decompressed bytes.","credential_check":"Raw records and decompressed bytes scanned for private keys, provider token shapes, bearer values and credential-bearing PG DSNs. Diagnostic source snapshots match their frozen source hashes. Only synthetic endpoints were configured.","benchmark_source_snapshots":sorted(snapshot_hashes),"capacity_exit_checks":gate_results,"file_count":len(entries),"files":entries}
    (ROOT/"EVIDENCE_MANIFEST.json").write_text(json.dumps(manifest,indent=2)+"\n")
if __name__=="__main__":
    run()
