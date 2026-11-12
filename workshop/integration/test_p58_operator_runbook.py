"""Exercise the public, zero-egress WSL operator sequence against two real PG clusters."""

import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "reports" / "p6_readiness" / "templates"


@pytest.mark.integration
def test_manager_to_two_private_books(dual_databases, tmp_path):
    base = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("NF_MODEL_", "NF_RUNTIME_", "NF_OPENAI_", "NF_DEEPSEEK_"))
    }
    aw_env = {**base, "AW_DATABASE_URL": dual_databases["aw"]}
    nf_env = {
        **base,
        "NF_DATABASE_URL": dual_databases["nf"],
        "NF_OBJECT_ROOT": str(tmp_path / "objects"),
        "NF_RUNTIME_PROFILES": str(TEMPLATES / "providers.json"),
    }

    def run(env, *args):
        return subprocess.run(
            [sys.executable, *map(str, args)],
            cwd=ROOT,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    admin = ("-m", "agent_workshop.admin")
    inst, org = str(uuid4()), str(uuid4())
    run(aw_env, *admin, "install", inst, str(uuid4()))
    run(aw_env, *admin, "bootstrap", org, "--preset", "Minimal", "--installation", inst)
    mgr = tmp_path / "manager.json"
    run(aw_env, *admin, "enroll", inst, org, "manager", mgr, "--revision", "1")
    budgets, works = set(), set()
    for name in ("a", "b"):
        folder = tmp_path / name
        created = json.loads(
            run(
                nf_env,
                "scripts/p58_bootstrap_book.py",
                "--book",
                TEMPLATES / "book.json",
                "--private-dir",
                folder,
            )
        )
        assert created["model_requests"] == 0
        works.add(created["world_ref"]["work_id"])
        budgets.add(created["budget_id"])
        access = folder / "supervisor.json"
        assert access.stat().st_mode & 0o077 == 0
        wr = created["world_ref"]
        policy = run(
            aw_env,
            "-m",
            "agent_workshop.policy_cli",
            "--binding",
            mgr,
            "publish",
            "--work",
            wr["work_id"],
            "--branch",
            wr["branch_id"],
            "--rules",
            TEMPLATES / "routing-rules.json",
            "--expected-revision",
            "0",
        )
        handoff = folder / "policy.json"
        handoff.write_text(policy)
        ctl = ("scripts/runtime_policy.py", "--access-file", access)
        run(nf_env, *ctl, "activate", "--policy", handoff, "--expected-revision", "0")
        for role, kind, profile in (
            ("writer", "scene_draft", "deepseek-v1"),
            ("volume_director", "window_plan", "codex-v1"),
        ):
            assert (
                json.loads(run(nf_env, *ctl, "preview", "--role", role, "--kind", kind))[
                    "profile_id"
                ]
                == profile
            )
        sup = ("scripts/run_supervisor.py", "--access-file", access)
        run(nf_env, *sup, "--register-pipeline", folder / "pipeline.json")
        status = json.loads(run(nf_env, *sup, "--pipeline-status"))
        assert status
    assert len(works) == len(budgets) == 2
