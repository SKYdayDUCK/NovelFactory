"""Migration and timer ceiling against an isolated, populated PostgreSQL database."""
import os
from pathlib import Path
import subprocess
import sys

import pytest
from sqlalchemy import text

from novel_factory.fixtures import create_bound_task, load_fixture, point
from novel_factory.orchestration.runtime_admission import delay_repeated_denial

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def test_capped_retry_and_rollback_preserve_refunded_attempts(kernel_env):
    kernel = kernel_env["kernel"]
    book = load_fixture(kernel)
    wr, owner = book["world_ref"], book["principal"]
    task = create_bound_task(kernel, owner, wr, point(1))
    budget = kernel.tasks.create_budget(wr["work_id"], "1")
    task_id, gen = task["task_id"], task["generation"]
    for n in range(7):
        call = kernel.tasks.reserve_call(task_id, gen, task["worker_id"], budget_id=budget, call_key=str(n),
                                         amount="0.01", request_hash="1" * 64)
        kernel.tasks.settle_call(call["attempt_id"], state="FAILED", actual_amount="0")
        with kernel.db.uow() as repo:
            repo.conn.execute(text("INSERT INTO nf_runtime_unsent(attempt_id,generation) VALUES (:attempt,:gen)"),
                              {"attempt": call["attempt_id"], "gen": gen})
    with kernel.db.uow() as repo:
        row = repo.one("tasks", {"task_id": task_id}, lock="update")
        ready = kernel.tasks._recover_task(repo.conn, row, reason="RUNTIME_CAPACITY_LIMIT")
        start = repo.now()
        delay_repeated_denial(repo.conn, task_id, call["attempt_id"])
        deadline = repo.conn.scalar(text("SELECT retry_not_before FROM nf_quota_retry_deadlines WHERE task_id=:task"),
                                    {"task": task_id})
        assert (deadline - start).total_seconds() >= 30
        assert (deadline - repo.now()).total_seconds() <= 30
    root = Path(__file__).resolve().parents[2]

    def migrate(operation, revision):
        return subprocess.run([sys.executable, "-m", "alembic", operation, revision],
                              cwd=root, env={**os.environ, "NF_DATABASE_URL": kernel_env["url"]},
                              capture_output=True, text=True, timeout=25)

    refused = migrate("downgrade", "0021_p58_admission")
    assert refused.returncode and "Drain active quota retry timers" in refused.stderr
    kernel.cancel_task(owner, task_id, ready["generation"], "cancel-wait")
    assert migrate("downgrade", "0021_p58_admission").returncode == 0
    assert migrate("upgrade", "head").returncode == 0
    with kernel.db.uow(write=False) as repo:
        assert not repo.conn.scalar(text("SELECT count(*) FROM nf_quota_retry_deadlines"))
        assert repo.conn.scalar(text("SELECT count(*) FROM nf_runtime_unsent")) == 7
        assert repo.one("tasks", {"task_id": task_id})["state"] == "CANCELLED"
        assert repo.one("budgets", {"budget_id": budget})["reserved_amount"] == 0
