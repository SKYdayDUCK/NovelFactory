"""Observe pre-existing charges and UNKNOWN reservations across the real 0017 upgrade."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from novel_factory.application.budget_limits import SharedBudgetService
from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import create_bound_task, load_fixture, point

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


def test_install_limit_on_populated_0017(kernel_env):
    def migrate(*args):
        subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            check=True,
            cwd=Path(__file__).resolve().parents[2],
            env={**os.environ, "NF_DATABASE_URL": kernel_env["url"]},
        )

    migrate("downgrade", "0017_nf_workshop_delegation")
    kernel = kernel_env["kernel"]
    w = load_fixture(kernel)
    wr, owner = w["world_ref"], w["principal"]
    budget = kernel.tasks.create_budget(wr["work_id"], "1")
    task = create_bound_task(kernel, owner, wr, point(1))

    def reserve(k):
        call = kernel.tasks.reserve_call(
            task["task_id"],
            task["generation"],
            task["worker_id"],
            budget_id=budget,
            amount="0.1",
            call_key=k,
            request_hash="a" * 64,
            token_amount=10,
        )
        assert kernel.tasks.mark_call_sent(call["attempt_id"], worker_id=task["worker_id"])
        return call["attempt_id"]

    kernel.tasks.settle_call(
        reserve("finished"),
        state="SUCCEEDED",
        actual_amount=".02",
        actual_tokens=6,
        provider_receipt="test-invoice",
    )
    kernel.tasks.settle_call(reserve("unconfirmed"), state="UNKNOWN")
    migrate("upgrade", "head")
    service = SharedBudgetService(kernel)
    scope = (owner, wr["work_id"], wr["branch_id"], budget)
    view = service.status(*scope)
    assert (
        view["spent_tokens"] == 6 and view["reserved_tokens"] == 10 and view["total_tokens"] is None
    )
    with pytest.raises(DomainError) as low:
        service.set_limit(*scope, 15)
    assert low.value.code == "BUDGET_EXCEEDED"
    assert service.set_limit(*scope, 16)["available_tokens"] == 0
    with pytest.raises(DomainError) as exhausted:
        reserve("after-upgrade")
    assert exhausted.value.code == "BUDGET_EXCEEDED"
    with pytest.raises(subprocess.CalledProcessError):
        migrate("downgrade", "0017_nf_workshop_delegation")
    assert service.status(*scope)["reserved_tokens"] == 10
