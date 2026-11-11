"""Check shared Token reservations using actual concurrent PostgreSQL transactions."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from pathlib import Path
from uuid import uuid4
import json
import os
import subprocess
import sys

import pytest

from novel_factory.application.budget_limits import SharedBudgetService
from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import create_bound_task, load_fixture, point
from novel_factory.infrastructure.recovery import fence_recovered_tasks

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


def test_cross_task_reservation_unknown_idempotency_and_recovery(kernel_env, tmp_path):
    kernel = kernel_env["kernel"]
    w = load_fixture(kernel)
    wr, owner = w["world_ref"], w["principal"]
    work, branch = wr["work_id"], wr["branch_id"]
    service = SharedBudgetService(kernel)
    budget = kernel.tasks.create_budget(work, "2")
    scope = (owner, work, branch, budget)
    assert service.status(*scope)["total_tokens"] is None
    a, b = [create_bound_task(kernel, owner, wr, point(1)) for _ in range(2)]

    def reserve(task, key, count):
        return kernel.tasks.reserve_call(
            task["task_id"],
            task["generation"],
            task["worker_id"],
            budget_id=budget,
            amount="0.10",
            call_key=key,
            request_hash="b" * 64,
            token_amount=count,
        )

    assert service.set_limit(*scope, 100)["available_tokens"] == 100
    # Concurrent tasks each need 60 Tokens from the same pool.
    barrier = Barrier(2)

    def enter(task):
        barrier.wait(timeout=10)
        try:
            return reserve(task, "first", 60)
        except DomainError as e:
            return e.code

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(enter, (a, b)))
    assert results.count("BUDGET_EXCEEDED") == 1
    source, other = (a, b) if isinstance(results[0], dict) else (b, a)
    first = next(result for result in results if isinstance(result, dict))
    assert reserve(source, "first", 60)["attempt_id"] == first["attempt_id"]
    with pytest.raises(DomainError) as conflict:
        reserve(source, "first", 12)
    assert conflict.value.code == "IDEMPOTENCY_CONFLICT"
    assert kernel.tasks.mark_call_sent(first["attempt_id"], worker_id=source["worker_id"])
    assert not kernel.tasks.mark_call_sent(first["attempt_id"], worker_id=source["worker_id"])
    tail = reserve(other, "second", 40)
    kernel.tasks.settle_call(tail["attempt_id"], state="FAILED", actual_amount="0", actual_tokens=0)
    with pytest.raises(DomainError):
        reserve(other, "never", 41)
    fence_recovered_tasks(kernel.db.engine, kernel.objects)
    status = service.status(*scope)
    assert (
        status["reserved_tokens"] == 60
        and status["available_tokens"] == 40
        and status["spent_tokens"] == 0
    )
    with kernel.db.uow(write=False) as repo:
        assert repo.one("call_attempts", {"attempt_id": first["attempt_id"]})["state"] == "UNKNOWN"
    with pytest.raises(DomainError) as low:
        service.set_limit(*scope, 59, expected_total_tokens=100)
    assert low.value.code == "BUDGET_EXCEEDED"
    for _ in range(2):
        kernel.tasks.settle_call(
            first["attempt_id"],
            state="SUCCEEDED",
            actual_amount="0.03",
            actual_tokens=17,
            provider_receipt="synthetic-usage-proof",
        )
    assert service.status(*scope)["available_tokens"] == 83
    assert service.set_limit(*scope, 19, expected_total_tokens=100)["available_tokens"] == 2
    assert service.set_limit(*scope, 19, expected_total_tokens=100)["spent_tokens"] == 17
    with pytest.raises(DomainError) as stale:
        service.set_limit(*scope, 40, expected_total_tokens=100)
    assert stale.value.code == "IDEMPOTENCY_CONFLICT"
    with kernel.db.uow(write=False) as repo:
        assert (
            len(
                repo.many("outbox", {"topic": "budget.token_limit_changed", "aggregate_id": budget})
            )
            == 2
        )
    restricted = kernel.issue_grant(
        owner, work, role_id="control_operator", branch_ids=[branch], capabilities=["ADMIN"]
    )["principal"]
    with pytest.raises(DomainError) as denied:
        service.status(restricted, work, branch, budget)
    assert denied.value.code == "VIEW_DENIED"
    binding = tmp_path / "project.json"
    with os.fdopen(os.open(binding, os.O_EXCL | os.O_WRONLY | os.O_CREAT, 0o600), "w") as f:
        json.dump(
            dict(
                token=w["token"],
                database_url=kernel_env["url"],
                object_root=str(kernel.objects.root),
                binding=dict(work_id=work, branch_id=branch),
            ),
            f,
        )
    proc = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[2] / "scripts/p6_budget.py"),
            "--binding",
            str(binding),
            "show",
            "--budget-id",
            budget,
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == service.status(*scope)


def test_legacy_budget_and_unknown_zero_reservation(kernel_env):
    kernel = kernel_env["kernel"]
    w = load_fixture(kernel)
    wr, owner = w["world_ref"], w["principal"]
    s = SharedBudgetService(kernel)
    budget = kernel.tasks.create_budget(wr["work_id"], "1")
    scope = (owner, wr["work_id"], wr["branch_id"], budget)
    task = create_bound_task(kernel, owner, wr, point(1))

    def hold(key, count):
        return kernel.tasks.reserve_call(
            task["task_id"],
            task["generation"],
            task["worker_id"],
            budget_id=budget,
            amount="0.1",
            call_key=key,
            request_hash="c" * 64,
            token_amount=count,
        )

    legacy = hold("uncapped-legacy", 0)
    assert s.status(*scope)["unquantified_attempts"] == 1
    with pytest.raises(DomainError) as unresolved:
        s.set_limit(*scope, 5)
    assert unresolved.value.code == "CALL_RECONCILIATION_REQUIRED"
    kernel.tasks.settle_call(
        legacy["attempt_id"], state="FAILED", actual_amount="0", actual_tokens=0
    )
    s.set_limit(*scope, 5)
    with pytest.raises(DomainError) as zero:
        hold("unpriced", 0)
    assert zero.value.code == "BUDGET_EXCEEDED"
    assert hold("exact-limit", 5)["reserved_tokens"] == 5
    with pytest.raises(DomainError):
        hold("another-call", 1)
    assert s.status(*scope)["reserved_amount"] == "0.100000"
