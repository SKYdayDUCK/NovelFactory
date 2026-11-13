"""Compare two scheduler selectors on one disposable, populated PostgreSQL queue."""
import ast
import json
from pathlib import Path
import subprocess
import time

from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from novel_factory.orchestration import dispatch


FROZEN = "434840593179e6c1faa6b7aeca3d7d747644506d"


def frozen_selector():
    source = subprocess.check_output(
        ["git", "show", FROZEN + ":src/novel_factory/orchestration/dispatch.py"], text=True)
    for stmt in ast.parse(source).body:
        if isinstance(stmt, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "_NEXT" for t in stmt.targets):
            return ast.literal_eval(stmt.value.args[0])
    raise ValueError("Frozen scheduler selector unavailable")


def compare(kernel, branch, directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    summary = []
    variants = {"baseline": frozen_selector(), "candidate": dispatch._NEXT.text}
    with kernel.db.uow() as repo:
        conn = repo.conn
        conn.execute(text("""INSERT INTO nf_dispatch_waiters(branch_id,requested_at,valid_until)
                          VALUES (:branch,now(),now()+interval '5 minutes')"""), {"branch": branch})
        for idx, variant in enumerate(("baseline", "candidate", "candidate", "baseline"), 1):
            sql = variants[variant]
            params = {"branch": branch}
            plan = conn.scalar(text("EXPLAIN (SETTINGS, FORMAT JSON) " + sql), params)
            row = {"variant": variant, "estimated_plan": plan}
            start = time.monotonic()
            try:
                with conn.begin_nested():
                    row["actual_plan"] = conn.scalar(text("EXPLAIN (ANALYZE, BUFFERS, SETTINGS, FORMAT JSON) " + sql), params)
                    row["selected_task"] = conn.scalar(text(sql), params)
                row["status"] = "COMPLETED"
            except OperationalError as exc:
                if exc.orig.sqlstate != "57014":
                    raise
                row.update(status="QUERY_CANCELLED", sqlstate=exc.orig.sqlstate)
            row["elapsed_seconds_including_identity_check"] = time.monotonic() - start
            filename = f"{idx}-{variant}.json"
            (directory / filename).write_text(json.dumps(row, indent=2) + "\n", encoding="utf-8")
            summary.append({"sample": filename, "status": row["status"],
                            "execution_ms": row.get("actual_plan", [{}])[0].get("Execution Time")})
    return {"gate": "PROFILED", "frozen_source": FROZEN, "samples": summary}
