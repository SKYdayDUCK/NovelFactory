"""Verify and recover immutable synthetic S9 experiment archives with read-only access."""
import base64
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import zipfile

ROOT = "repos/SKYdayDUCK/NovelFactory"
FOLDER = Path(__file__).resolve().parent
SIZE = 300000
# The original report contains timings, roles and request outcomes. A secret or a
# populated credential field requires separate review before the archive is published.
PRIVATE = re.compile(r"(?:-----BEGIN (?:OPENSSH |RSA |EC )?PRIVATE KEY-----|\b(?:ghp_|github_pat_|ghs_|sk-proj-)[A-Za-z0-9_-]{18,}|(?:postgres(?:ql)?(?:\+psycopg)?|redis(?:s)?|https?)://[^\s/:@]+:[^\s/@]+@|[?&](?:x-amz-signature|sig)=|[\"'](?:password|api_key|access_token|binding_secret|private_key|authorization)[\"']\s*:\s*[\"'][^\"']+|Bearer [A-Za-z0-9_.-]{20,})", re.I)

def api(path, raw=False):
    value = subprocess.check_output(["gh", "api", f"{ROOT}/{path}"])
    return value if raw else json.loads(value)

def digest(value):
    return hashlib.sha256(value).hexdigest()

def check(value, message):
    if not value:
        raise ValueError(message)

def screen(z):
    members = {}
    for m in z.infolist():
        if m.is_dir():
            continue
        filename = PurePosixPath(m.filename)
        check(not filename.is_absolute() and ".." not in filename.parts and m.filename not in members, "unsafe archive path")
        check(0 <= m.file_size <= 90_000_000, "unexpected member size")
        value = z.read(m)
        content = gzip.decompress(value) if value[:2] == b"\x1f\x8b" else value
        try:
            decoded = content.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError("unexpected binary experiment member: " + m.filename) from None
        check(not PRIVATE.search(decoded) and os.environ["GH_TOKEN"] not in decoded, "review selected archive member: " + m.filename)
        members[m.filename] = {"bytes":len(value),"sha256":digest(value)}
    return members

def main():
    request = json.loads((FOLDER/"archive-request.json").read_text())
    idx, part = int(os.environ["ARCHIVE_ID"]), int(os.environ["PART"])
    entries = [i for i in request if i["artifact"]["id"] == idx]
    check(len(entries) == 1, "artifact not fixed by request")
    entry = entries[0]
    run = api(f"actions/runs/{entry['run']}")
    check(run["repository"]["full_name"] == "SKYdayDUCK/NovelFactory" and run["head_branch"] == entry.get("branch", "evidence/p55-55d897-guard")
          and run["head_sha"] == entry["head"] and run["event"] == entry["event"] and run["status"] == "completed"
          and run["conclusion"] == entry["conclusion"] and Path(run["path"]).name == entry["workflow"], "run provenance mismatch")
    info = api(f"actions/artifacts/{idx}")
    required = entry["artifact"]
    check(info["name"] == required["name"] and info["size_in_bytes"] == required["size"]
          and info["digest"] == required["digest"] and info["workflow_run"]["id"] == run["id"]
          and info["workflow_run"]["head_sha"] == entry["head"] and not info["expired"], "artifact provenance mismatch")
    archive = api(f"actions/artifacts/{idx}/zip", raw=True)
    check(len(archive) == required["size"] and "sha256:"+digest(archive) == required["digest"], "download differs from GitHub digest")
    with zipfile.ZipFile(io.BytesIO(archive)) as z:
        members = screen(z)
        if "expected_result_sha256" in entry:
            check(members["result.json"]["sha256"] == entry["expected_result_sha256"], "published historical source digest mismatch")
            results = json.loads(z.read("result.json"))
            check(len(results["records"]) == results["scheduled_count"] == 12000 and results["real_model_calls"] == results["new_world_commits"] == 0, "historical work is not the registered deterministic probe")
    count = (len(archive)+SIZE-1)//SIZE
    check(0 <= part < count, "invalid part")
    if part == 0:
        print("S9_MEMBERS", idx, base64.b64encode(json.dumps(members,sort_keys=True).encode()).decode())
    payload = archive[part*SIZE:(part+1)*SIZE]
    encoded = base64.b64encode(payload).decode()
    chunks = [encoded[n:n+5000] for n in range(0,len(encoded),5000)]
    for n,chunk in enumerate(chunks):
        print("S9_ORIGINAL",idx,part,count,n,len(chunks),len(payload),digest(payload),chunk)

if __name__ == "__main__":
    main()
