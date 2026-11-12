"""Prove populated observation upgrades preserve earlier evidence rows."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import text

from novel_factory.application.audit_log import record
from novel_factory.fixtures import create_bound_task, load_fixture, point

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def test_populated_0019_history_and_guarded_downgrade(kernel_env):
    kernel = kernel_env["kernel"]
    cmd = [sys.executable, "-m", "alembic"]
    cli = {
        "env": {**os.environ, "NF_DATABASE_URL": kernel_env["url"]},
        "cwd": Path(__file__).resolve().parents[2],
        "capture_output": True,
        "text": True,
    }
    subprocess.run([*cmd, "downgrade", "0019_p57_context_sources"], **cli, check=True)
    world = load_fixture(kernel)
    base = world["world_ref"]
    with kernel.db.uow() as repo:
        event = record(repo.conn, "operator.preexisting", **base)
        original = repo.conn.scalar(
            text("SELECT to_jsonb(a) FROM nf_audit_events a WHERE event_id=:event"), {"event": event}
        )
    subprocess.run([*cmd, "upgrade", "head"], **cli, check=True)
    with kernel.db.uow() as repo:
        row = repo.conn.scalar(
            text("SELECT to_jsonb(a) FROM nf_audit_events a WHERE event_id=:event"), {"event": event}
        )
        assert row.pop("harness_details") is None and row == original
    job = create_bound_task(kernel, world["principal"], base, point(1))
    with kernel.db.uow() as repo:
        observed = repo.conn.scalar(
            text(
                "SELECT to_jsonb(a) FROM nf_audit_events a WHERE task_id=:t AND event_type='context.compiled'"
            ),
            {"t": job["task_id"]},
        )
    assert observed["harness_details"]["packet_bytes"] > 0
    failed = subprocess.run([*cmd, "downgrade", "0019_p57_context_sources"], **cli)
    assert failed.returncode and "preserve recorded Harness observations" in failed.stderr
    with kernel.db.uow() as repo:
        assert observed == repo.conn.scalar(
            text("SELECT to_jsonb(a) FROM nf_audit_events a WHERE event_id=:id"), {"id": observed["event_id"]}
        )
