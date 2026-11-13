"""Check shared Token reservations using actual concurrent PostgreSQL transactions."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from pathlib import Path
from uuid import uuid4
import json
import os
import subprocess
import time
import sys

import pytest
from sqlalchemy import text

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


def test_budget_cannot_be_read_changed_or_spent_across_works(kernel_env):
    kernel = kernel_env["kernel"]
    local, foreign = load_fixture(kernel), load_fixture(kernel)
    admin, wr = local["principal"], local["world_ref"]
    other_admin, other_wr = foreign["principal"], foreign["world_ref"]
    budget = kernel.tasks.create_budget(wr["work_id"], "1")
    service = SharedBudgetService(kernel)
    scope = (admin, wr["work_id"], wr["branch_id"], budget)
    baseline = service.set_limit(*scope, 100)

    # Both callers have authority over their own work. A guessed real budget
    # and an absent budget must have the same public read/write result.
    errors = []
    for target in (budget, str(uuid4())):
        foreign_scope = (other_admin, other_wr["work_id"], other_wr["branch_id"], target)
        for operation in (service.status, lambda *args: service.set_limit(*args, 5)):
            with pytest.raises(DomainError) as denied:
                operation(*foreign_scope)
            errors.append((denied.value.code, denied.value.safe_message))
    assert len(set(errors)) == 1 and errors[0][0] == "VIEW_DENIED"
    with pytest.raises(DomainError) as impersonated:
        service.status(other_admin, wr["work_id"], wr["branch_id"], budget)
    assert impersonated.value.code == "VIEW_DENIED"

    task = create_bound_task(kernel, other_admin, other_wr, point(1))
    with pytest.raises(DomainError) as spend:
        kernel.tasks.reserve_call(task["task_id"], task["generation"], task["worker_id"],
                                  budget_id=budget, amount=".1", token_amount=5,
                                  request_hash="a" * 64, call_key="other-work")
    assert spend.value.code == "PERMISSION_DENIED"
    assert service.status(*scope) == baseline


def _reserve_cancellable(kernel, task, budget, key, tokens, amount):
    return kernel.tasks.reserve_call(
        task["task_id"], task["generation"], task["worker_id"],
        budget_id=budget, call_key=key, request_hash="a" * 64,
        token_amount=tokens, amount=amount,
    )["attempt_id"]


def test_cancel_refunds_only_provably_unsent_calls(kernel_env):
    kernel = kernel_env["kernel"]
    fixture = load_fixture(kernel)
    owner, wr = fixture["principal"], fixture["world_ref"]
    budget = kernel.tasks.create_budget(wr["work_id"], "1")
    scope = (owner, wr["work_id"], wr["branch_id"], budget)
    service = SharedBudgetService(kernel)
    service.set_limit(*scope, 100)
    task = create_bound_task(kernel, owner, wr, point(1))
    unsent = _reserve_cancellable(kernel, task, budget, "unsent", 40, ".40")
    sent = _reserve_cancellable(kernel, task, budget, "sent", 32, ".32")
    unknown = _reserve_cancellable(kernel, task, budget, "unknown", 28, ".28")
    for attempt in (sent, unknown):
        assert kernel.tasks.mark_call_sent(attempt, worker_id=task["worker_id"])
    kernel.tasks.settle_call(unknown, state="UNKNOWN")

    scope_cancel = (owner, task["task_id"], task["generation"], "cancel-current")
    receipt = kernel.cancel_task(*scope_cancel)
    assert receipt["status"] == "CANCELLED"
    assert kernel.cancel_task(*scope_cancel) == receipt
    with kernel.db.uow(write=False) as repo:
        settled = repo.one("call_attempts", {"attempt_id": unsent})
        assert (settled["state"], settled["actual_amount"], settled["actual_tokens"]) == ("FAILED", 0, 0)
        assert settled["provider_receipt"] is None and settled["reserved_tokens"] == 40
        assert repo.one("call_attempts", {"attempt_id": sent})["state"] == "SENT"
        assert repo.one("call_attempts", {"attempt_id": unknown})["state"] == "UNKNOWN"
    with pytest.raises(DomainError) as cancelled:
        kernel.tasks.mark_call_sent(unsent, worker_id=task["worker_id"])
    assert cancelled.value.code == "TASK_NOT_ACCEPTABLE"
    view = service.status(*scope)
    assert (view["reserved_amount"], view["reserved_tokens"], view["available_tokens"]) == (
        "0.600000", 60, 40,
    )
    # A later timeout cannot release a sent call. The exact invoice settles
    # it once; the unrelated UNKNOWN call remains fully held.
    kernel.tasks.settle_call(sent, state="UNKNOWN")
    for _ in range(2):
        kernel.tasks.settle_call(
            sent, state="SUCCEEDED", actual_amount=".20", actual_tokens=18,
            provider_receipt="fixture-reconciled",
        )
        final = service.status(*scope)
        assert (final["reserved_amount"], final["spent_amount"]) == ("0.280000", "0.200000")
        assert (final["reserved_tokens"], final["spent_tokens"], final["available_tokens"]) == (28, 18, 54)


def test_cancel_after_recovery_preserves_previous_generation_holds(kernel_env):
    kernel = kernel_env["kernel"]
    fixture = load_fixture(kernel)
    owner, wr = fixture["principal"], fixture["world_ref"]
    budget = kernel.tasks.create_budget(wr["work_id"], "1")
    service = SharedBudgetService(kernel)
    scope = (owner, wr["work_id"], wr["branch_id"], budget)
    service.set_limit(*scope, 100)
    task = create_bound_task(kernel, owner, wr, point(1))
    prepared = _reserve_cancellable(kernel, task, budget, "prepared-at-checkpoint", 40, ".40")
    sent = _reserve_cancellable(kernel, task, budget, "sent-at-checkpoint", 60, ".60")
    assert kernel.tasks.mark_call_sent(sent, worker_id=task["worker_id"])

    fence_recovered_tasks(kernel.db.engine, kernel.objects)
    recovered = kernel.get_task(owner, task["task_id"])
    assert recovered["generation"] == task["generation"] + 1
    receipt = kernel.cancel_task(owner, task["task_id"], recovered["generation"], "cancel-recovered")
    assert receipt["status"] == "CANCELLED"
    with kernel.db.uow(write=False) as repo:
        assert repo.one("call_attempts", {"attempt_id": prepared})["state"] == "PREPARED"
        assert repo.one("call_attempts", {"attempt_id": sent})["state"] == "UNKNOWN"
    view = service.status(*scope)
    assert (view["reserved_amount"], view["reserved_tokens"], view["available_tokens"]) == (
        "1.000000", 100, 0,
    )


@pytest.mark.parametrize("first", ["cancel", "send"])
def test_cancel_and_dispatch_serialize_at_the_task_lock(kernel_env, first):
    kernel = kernel_env["kernel"]
    fixture = load_fixture(kernel)
    owner, wr = fixture["principal"], fixture["world_ref"]
    budget = kernel.tasks.create_budget(wr["work_id"], "1")
    service = SharedBudgetService(kernel)
    scope = (owner, wr["work_id"], wr["branch_id"], budget)
    service.set_limit(*scope, 100)
    task = create_bound_task(kernel, owner, wr, point(1))
    attempt = _reserve_cancellable(kernel, task, budget, "racing", 100, "1")

    def send():
        try:
            return kernel.tasks.mark_call_sent(attempt, worker_id=task["worker_id"])
        except DomainError as error:
            return error.code

    def cancel():
        return kernel.cancel_task(owner, task["task_id"], task["generation"], "racing")["status"]

    # Queue both real API calls on the task row. Follow the SQL lock graph
    # before releasing it so scheduler timing cannot choose a different order.
    actions = {"cancel": cancel, "send": send}
    order = (first, "send" if first == "cancel" else "cancel")
    futures = {}
    with ThreadPoolExecutor(2) as pool:
        with kernel.db.engine.begin() as gate, kernel.db.engine.connect() as observer:
            observer = observer.execution_options(isolation_level="AUTOCOMMIT")
            gate_pid = gate.scalar(text("SELECT pg_backend_pid()"))
            gate.execute(text("SELECT task_id FROM nf_tasks WHERE task_id=:id FOR UPDATE"),
                         {"id": task["task_id"]})
            blocked_count = text("""
                WITH RECURSIVE blocked(pid) AS (
                    SELECT CAST(:gate AS int) UNION
                    SELECT a.pid FROM pg_stat_activity a JOIN blocked b
                      ON b.pid = ANY(pg_blocking_pids(a.pid))
                    WHERE a.datname = current_database()
                ) SELECT count(*) - 1 FROM blocked
            """)
            for n, action in enumerate(order, 1):
                futures[action] = pool.submit(actions[action])
                deadline = time.monotonic() + 10
                while observer.scalar(blocked_count, {"gate": gate_pid}) < n:
                    assert time.monotonic() < deadline, f"{action} never waited for the task lock"
                    time.sleep(0.01)
        assert futures["cancel"].result(timeout=10) == "CANCELLED"
        assert futures["send"].result(timeout=10) == ("TASK_NOT_ACCEPTABLE" if first == "cancel" else True)

    with kernel.db.uow(write=False) as repo:
        call = repo.one("call_attempts", {"attempt_id": attempt})
    view = service.status(*scope)
    if first == "cancel":
        assert (call["state"], call["actual_amount"], call["actual_tokens"]) == ("FAILED", 0, 0)
        assert (view["reserved_amount"], view["reserved_tokens"]) == ("0.000000", 0)
    else:
        assert (call["state"], call["actual_amount"], call["actual_tokens"]) == ("SENT", None, None)
        assert (view["reserved_amount"], view["reserved_tokens"]) == ("1.000000", 100)


def test_task_and_shared_money_and_token_limits_are_independent(kernel_env):
    kernel = kernel_env["kernel"]
    fixture = load_fixture(kernel)
    owner, wr = fixture["principal"], fixture["world_ref"]
    budget = kernel.tasks.create_budget(wr["work_id"], "1.30")
    service = SharedBudgetService(kernel)
    scope = (owner, wr["work_id"], wr["branch_id"], budget)
    service.set_limit(*scope, 60_000)
    task_a, task_b, task_c = [
        create_bound_task(kernel, owner, wr, point(1), token_budget=limit)
        for limit in (20_000, 20_000, 50_000)
    ]

    def reserve(task, key, amount, tokens):
        return kernel.tasks.reserve_call(task["task_id"], task["generation"], task["worker_id"],
                                         budget_id=budget, amount=amount, token_amount=tokens,
                                         call_key=key, request_hash="b" * 64)

    reserve(task_a, "first", ".60", 10_000)
    baseline = service.status(*scope)
    for key, amount, tokens in (("task-money", ".41", 1), ("task-tokens", ".01", 10_001)):
        with pytest.raises(DomainError) as denied:
            reserve(task_a, key, amount, tokens)
        assert denied.value.code == "BUDGET_EXCEEDED"
        assert service.status(*scope) == baseline
    reserve(task_b, "second", ".50", 11_000)
    baseline = service.status(*scope)
    for key, amount, tokens in (("shared-money", ".21", 1), ("shared-tokens", ".01", 39_001)):
        with pytest.raises(DomainError) as denied:
            reserve(task_c, key, amount, tokens)
        assert denied.value.code == "BUDGET_EXCEEDED"
        assert service.status(*scope) == baseline
    reserve(task_c, "exact-remaining", ".20", 39_000)
    final = service.status(*scope)
    assert (final["reserved_amount"], final["reserved_tokens"], final["available_tokens"]) == ("1.300000", 60_000, 0)


def test_provision_next_preserves_unknowns_and_is_idempotent(kernel_env):
    kernel = kernel_env["kernel"]
    fixture = load_fixture(kernel)
    owner, wr = fixture["principal"], fixture["world_ref"]
    budget = kernel.tasks.create_budget(wr["work_id"], ".40")
    service = SharedBudgetService(kernel)
    scope = owner, wr["work_id"], wr["branch_id"], budget
    service.set_limit(*scope, 40)
    job = create_bound_task(kernel, owner, wr, point(1))
    attempt = _reserve_cancellable(kernel, job, budget, "original", 40, ".40")
    kernel.tasks.mark_call_sent(attempt, worker_id=job["worker_id"])
    kernel.tasks.settle_call(attempt, state="UNKNOWN")
    result = service.provision_next(*scope, ".25", 50)
    assert result["reserved_tokens"] == 40 and result["available_tokens"] == 50
    assert result["reserved_amount"] == "0.400000" and result["limit_amount"] == "0.650000"
    assert service.provision_next(*scope, ".25", 50) == result
    with kernel.db.uow(write=False) as repo:
        assert repo.one("call_attempts", {"attempt_id": attempt})["state"] == "UNKNOWN"
