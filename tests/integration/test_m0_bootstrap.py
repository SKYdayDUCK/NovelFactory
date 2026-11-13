"""Double-run the real bootstrap CLI, with no external endpoint contacted."""
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest


pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def test_bootstrap_replay_scopes_and_permissions(kernel_env, tmp_path):
    root = Path(__file__).resolve().parents[2]
    private = tmp_path / "service"
    env = {**os.environ, "NF_DATABASE_URL": kernel_env["url"], "NF_OBJECT_ROOT": str(kernel_env["store"].root),
           "NF_MODEL_ENDPOINT": "https://example.test/v1/chat/completions", "NF_MODEL_NAME": "sample"}
    cmd = [sys.executable, "scripts/bootstrap_p5_m0.py", "--private-dir", str(private)]
    # A missing endpoint fails before committing an orphan world or task.
    incomplete = dict(env)
    incomplete.pop("NF_MODEL_ENDPOINT")
    failed = subprocess.run(cmd, cwd=root, env=incomplete, capture_output=True)
    assert failed.returncode
    work_id = json.loads((private / "sample.json").read_text())["work_id"]
    kernel = kernel_env["kernel"]
    with kernel.db.uow(write=False) as repo:
        assert repo.one("works", {"work_id": work_id}) is None
    first = subprocess.run(cmd, cwd=root, env=env, capture_output=True, text=True, check=True)
    second = subprocess.run(cmd, cwd=root, env=env, capture_output=True, text=True, check=True)
    expected, replayed = json.loads(first.stdout), json.loads(second.stdout)
    assert expected["status"] == "CREATED" and replayed["status"] == "EXISTS"
    assert expected["work_id"] == replayed["work_id"] == work_id
    binding = json.loads((private / "resume.json").read_text())
    admin = json.loads((private / "supervisor.json").read_text())
    assert all(token not in first.stdout + first.stderr + second.stdout + second.stderr
               for token in [binding["token"], admin["token"]])
    if os.name == "nt":
        from novel_factory.infrastructure.private_files import check_private_directory, read_private_bytes

        check_private_directory(private)
        for path in private.glob("*.json"):
            assert read_private_bytes(path, 16384)
    else:
        assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in private.iterdir())
    assert binding["token"] != admin["token"]
    assert kernel.authenticate(binding["token"]) != kernel.authenticate(admin["token"])
    with kernel.db.uow(write=False) as repo:
        jobs = repo.many("tasks", {"work_id": work_id})
        assert sorted(row["state"] for row in jobs) == ["BLOCKED", "READY"]
        assert len(repo.many("budgets", {"work_id": work_id})) == 1
        assert all(repo.one("supervisor_jobs", {"task_id": row["task_id"]}) for row in jobs)
        grant = repo.one("grants", {"grant_id": kernel.authenticate(binding["token"]).grant_id})
        assert grant["capabilities"] == ["PROJECT_RESUME"] and grant["branch_ids"] == [expected["branch_id"]]
