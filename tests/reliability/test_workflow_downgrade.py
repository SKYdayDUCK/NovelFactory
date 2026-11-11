"""A failed downgrade on a populated wait table must roll back every prior DDL."""
import os
from pathlib import Path
import subprocess
import sys

import pytest
from sqlalchemy import text

from novel_factory.infrastructure.recovery import database_snapshot
from novel_factory.orchestration.workflow import WorkflowService
from .test_workflow import blocked, scope_args

pytestmark = [pytest.mark.postgres]


def test_downgrade_preserves_populated_extensions(rel_env, rel_scope):
    first, second = (blocked(rel_env, rel_scope) for _ in range(2))
    WorkflowService(rel_env["service"]).wait(first, 0, [second], mode="AND", **scope_args(rel_scope))
    db = rel_env["db"]
    with db.uow(write=False) as repo:
        before = database_snapshot(repo.conn)
    command = subprocess.run([sys.executable, "-m", "alembic", "downgrade", "0003"],
        env={**os.environ, "NF_DATABASE_URL": rel_env["url"]}, cwd=Path(__file__).resolve().parents[2],
        capture_output=True, text=True)
    assert command.returncode != 0 and "discard workflow evidence" in command.stderr
    with db.uow(write=False) as repo:
        assert database_snapshot(repo.conn) == before
        assert repo.conn.scalar(text("SELECT count(*) FROM nf_workflow_edges")) == 1
