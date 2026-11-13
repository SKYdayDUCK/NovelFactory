"""Exercise the documented operator path with a native Python process and PG17."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def test_provision_status_resume_and_replay(kernel_env, tmp_path):
    private = tmp_path / "作品 bindings"
    objects = str(kernel_env["store"].root)
    env = {**os.environ, "NF_OBJECT_ROOT": objects, "NF_DATABASE_URL": kernel_env["url"]}

    def cli(*args, arguments=None, check=True):
        result = subprocess.run([sys.executable, "scripts/nf.py", *args], env=env, cwd=ROOT,
                              input=arguments, encoding="utf-8", capture_output=True, timeout=45)
        if check:
            assert result.returncode == 0, result.stderr
        return result

    book_args = ("init-book", "--book", "reports/p6_readiness/templates/book.json", "--private-dir", private)
    created = json.loads(cli(*book_args).stdout)
    assert created["status"] == "CREATED" and created["model_requests"] == 0
    assert json.loads(cli(*book_args).stdout)["status"] == "EXISTS"
    work = created["world_ref"]
    control = private / "control.json"
    resume = private / "resume.json"
    assert json.loads(cli("call", "--binding", control, "nf_project_status",
                          arguments="{}", check=False).stdout)["error"]["code"] == "VIEW_DENIED"
    grant_args = ("grant-resume", "--admin-binding", control, "--work-id", work["work_id"],
                  "--branch-id", work["branch_id"], "--object-root", objects, "--hours", "24", "--output", resume)
    assert "Resume grant" in cli(*grant_args).stdout
    saved = resume.read_bytes()
    assert cli(*grant_args, check=False).returncode != 0 and resume.read_bytes() == saved
    for name in ("nf_project_status", "nf_resume_project"):
        reply = json.loads(cli("call", "--binding", resume, name, arguments="{}").stdout)
        assert reply["ready_count"] == 0 and reply["counts"]["READY"] == 0
        if name == "nf_resume_project":
            assert reply["awakened"] == reply["recovered"] == 0
    # The book bootstrap prepares the request for operator registration; an
    # unregistered pipeline is not presented as an active production board.
    inactive = cli("supervisor", "--access-file", private / "supervisor.json", "--pipeline-status", check=False)
    assert inactive.returncode != 0 and inactive.stderr.strip() == "INVALID_ARGUMENT"
