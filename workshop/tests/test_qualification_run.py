import hashlib
import json
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

CHECKER = Path(__file__).resolve().parents[1] / "qualification" / "verify_run.py"
SHA = "a" * 40


@pytest.fixture
def source(tmp_path):
    archive = tmp_path / "workshop-results.zip"
    archive.write_bytes(b"original artifact")
    run = {
        "id": 7,
        "run_attempt": 2,
        "repository": {"full_name": "SKYdayDUCK/NovelFactory", "id": 11},
        "path": ".github/workflows/agent-workshop.yml",
        "status": "completed",
        "conclusion": "success",
        "event": "push",
        "head_branch": "main",
        "head_sha": SHA,
    }
    job = {
        "id": 12,
        "name": "workshop",
        "run_id": run["id"],
        "run_attempt": run["run_attempt"],
        "status": "completed",
        "conclusion": "success",
        "started_at": "2026-02-01T00:00:00Z",
        "completed_at": "2026-02-01T00:05:00Z",
        "head_sha": SHA,
    }
    artifact = {
        "id": 13,
        "name": "workshop-results",
        "workflow_run": {"id": run["id"], "head_sha": SHA, "repository_id": 11},
        "created_at": job["completed_at"],
        "digest": "sha256:" + hashlib.sha256(archive.read_bytes()).hexdigest(),
    }
    return run, job, artifact, archive


def test_earlier_success_survives_partial_workflow_rerun(source):
    run, job, artifact, archive = source
    checker = runpy.run_path(str(CHECKER))
    job["run_attempt"] = 1
    other_job = {**job, "name": "another job", "run_attempt": 2}
    selected = checker["effective_job"](run, [job, other_job], "workshop")
    receipt, raw = checker["verify_archive"](archive, run, selected, [artifact])
    assert receipt["job_attempt"] == 1 and run["run_attempt"] == 2
    assert raw == archive.read_bytes()


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ("run", "mixed run IDs"),
        ("attempt", "wrong job attempt"),
        ("archive_time", "archive outside job attempt"),
        ("foreign_zip", "archive digest mismatch"),
        ("missing", "workshop-results.zip"),
        ("superseded", "latest job attempt 2 did not pass"),
        ("superseded_archive", "archive outside job attempt"),
        ("truncated", "incomplete GitHub pagination"),
    ],
)
def test_offline_gate_rejects_mixed_or_missing_receipts(tmp_path, source, change, expected):
    run, job, artifact, archive = source
    jobs = [job]
    if change == "run":
        artifact["workflow_run"]["id"] = 6
    elif change == "attempt":
        job["run_attempt"] = 3
    elif change == "archive_time":
        artifact["created_at"] = "2026-01-31T23:59:00Z"
    elif change == "foreign_zip":
        archive.write_bytes(b"archive from another run at the same checkout")
    elif change == "missing":
        archive.unlink()
    elif change in {"superseded", "superseded_archive"}:
        job["run_attempt"] = 1
        jobs.append(
            {
                **job,
                "id": 24,
                "run_attempt": 2,
                "started_at": "2026-02-01T04:00:00Z",
                "completed_at": "2026-02-01T04:05:00Z",
                "conclusion": "failure" if change == "superseded" else "success",
            }
        )
    for name, document in (
        ("run", run),
        ("jobs", {"jobs": jobs, "total_count": len(jobs) + int(change == "truncated")}),
        ("artifacts", {"artifacts": [artifact], "total_count": 1}),
    ):
        (tmp_path / f"{name}.json").write_text(json.dumps(document))
    proc = subprocess.run(
        [
            sys.executable,
            str(CHECKER),
            "--metadata-dir",
            str(tmp_path),
            "--archives",
            str(tmp_path),
            "--checkout-sha",
            SHA,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1
    result = json.loads(proc.stdout)
    assert result["status"] == "FAIL" and expected in result["error"]
