"""Launch a recovered P6 worker from its frozen source and the host credential vault."""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from agent_runtime.credentials import resolve
from novel_factory.infrastructure.private_files import read_private_bytes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--book", choices=("A", "B", "C"), required=True)
    parser.add_argument("--run", action="store_true", help="Dispatch existing authorized tasks; may spend model budget")
    args = parser.parse_args()
    source, state = args.source.resolve(), args.state.resolve()
    manifest = json.loads(read_private_bytes(state / "batch/batch.json", 100_000))
    sha = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    dirty = subprocess.check_output(["git", "-C", str(source), "status", "--porcelain"], text=True)
    if sha != manifest["source_sha"] or dirty or manifest["objects"] != str(state / "objects"):
        raise ValueError("Recovered source or object binding differs")
    password = resolve("NF_P6_DATABASE_PASSWORD")
    if not password:
        raise ValueError("Recovered database credential is not configured")
    env = {**os.environ, "NF_DATABASE_URL": manifest["database"], "NF_OBJECT_ROOT": manifest["objects"],
           "PGPASSWORD": password, "PYTHONUTF8": "1", "PYTHONPATH": os.pathsep.join(
               str(source / p) for p in ("src", "workshop/src", "scripts", "."))}
    env.pop("PGPASSFILE", None)
    env["NF_RUNTIME_PROFILES"] = str(source / "configs/deepseek-p6.example.json")
    if not args.run:
        # Read only task identities and states; never render private manuscripts.
        code = (
            "import json,os;from sqlalchemy import text;from novel_factory.infrastructure.database import Database;"
            "from novel_factory.infrastructure.private_files import read_private_bytes;from pathlib import Path;"
            "a=json.loads(read_private_bytes(Path(os.environ['NF_P6_ACCESS']),100000));"
            "d=Database();c=d.engine.connect();"
            "print(json.dumps({'work_id':a['work_id'],'tasks':[dict(r) for r in c.execute(text("
            "'SELECT task_id::text,generation,state,role_id,task_kind FROM nf_tasks WHERE work_id=:w ORDER BY created_at DESC LIMIT 8'"
            "),{'w':a['work_id']}).mappings()]}));c.close();d.close()")
        env["NF_P6_ACCESS"] = str(state / "batch" / args.book / "supervisor.json")
        return subprocess.run([sys.executable, "-c", code], cwd=source, env=env, check=False).returncode
    key = resolve("NF_DEEPSEEK_API_KEY")
    if not key:
        raise ValueError("DeepSeek credential is not configured")
    env.update(NF_DEEPSEEK_API_KEY=key, NF_RUNTIME_LIVE_APPROVED="1")
    return subprocess.run([sys.executable, str(source / "scripts/p6_batch.py"), "worker", "--private-dir",
                           str(state / "batch"), "--book", args.book], cwd=source, env=env, check=False).returncode


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, subprocess.CalledProcessError):
        sys.exit("Recovered worker unavailable; verify its private state, source and credentials.")
