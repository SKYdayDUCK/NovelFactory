"""P1-05: real PostgreSQL lease, generation, budget and delivery counterexamples."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
import threading
from uuid import uuid4

import pytest
from sqlalchemy import func, insert, select, text
from sqlalchemy.exc import IntegrityError

from novel_factory.domain.errors import DomainError
from novel_factory.contracts.registry import validate_contract
from novel_factory.orchestration.tasks import OutboxService, enqueue_outbox
from .conftest import task_spec, start_task, bind_task, task_result

pytestmark = pytest.mark.postgres


def test_task_create_idempotency_and_immutable_input(rel_env, rel_scope):
    service = rel_env["service"]
    spec = task_spec(rel_scope)
    first = service.create(spec, principal_id=rel_scope["principal_id"], idempotency_key="stable-create")
    assert first == service.create(spec, principal_id=rel_scope["principal_id"], idempotency_key="stable-create")
    changed = {**spec, "task_kind": "different"}
    with pytest.raises(DomainError) as exc:
        service.create(changed, principal_id=rel_scope["principal_id"], idempotency_key="stable-create")
    assert exc.value.code == "IDEMPOTENCY_CONFLICT"
    t = rel_env["tables"]["task_inputs"]
    with pytest.raises(IntegrityError):
        with rel_env["db"].engine.begin() as conn:
            conn.execute(t.update().where(t.c.task_id == spec["task_id"]).values(input_hash="0" * 64))


@pytest.mark.concurrency
def test_two_connections_cannot_claim_same_task(rel_env, rel_scope):
    service = rel_env["service"]
    spec = task_spec(rel_scope)
    service.create(spec, principal_id=rel_scope["principal_id"], idempotency_key=spec["task_id"])
    barrier = threading.Barrier(2)
    pids = set()
    def claim(worker):
        with rel_env["db"].engine.connect() as conn:
            pids.add(conn.scalar(text("SELECT pg_backend_pid()")))
            barrier.wait(timeout=5)
        return service.claim(worker, task_id=spec["task_id"])
    with ThreadPoolExecutor(max_workers=2) as pool:
        rows = list(pool.map(claim, ["left", "right"]))
    assert len(pids) == 2
    assert sum(row is not None for row in rows) == 1
    assert service.get(spec["task_id"])["generation"] == 1
    assert rel_env["db"].engine.pool.checkedout() == 0


def test_authenticated_claim_filters_before_mutation(rel_env, rel_scope):
    service = rel_env["service"]
    spec = task_spec(rel_scope)
    service.create(spec, principal_id=rel_scope["principal_id"], idempotency_key=spec["task_id"])
    assert service.claim("intruder", task_id=spec["task_id"], principal_id=str(uuid4())) is None
    assert service.get(spec["task_id"])["state"] == "READY"
    assert service.claim("valid", task_id=spec["task_id"], principal_id=rel_scope["principal_id"], grant_id=rel_scope["grant_id"]) is not None


def test_context_requires_persisted_current_binding(rel_env, rel_scope):
    task = start_task(rel_env, rel_scope, bind=False)
    service = rel_env["service"]
    with pytest.raises(DomainError) as exc:
        service.bind_context(task["task_id"], task["generation"], "worker", context_bundle_id=str(uuid4()), dependency_fingerprint="0" * 64, world_ref=task["world_ref"])
    assert exc.value.code == "CONTEXT_MISMATCH"
    wrong = {**task["world_ref"], "world_revision_id": str(uuid4())}
    with pytest.raises(DomainError) as exc:
        service.bind_context(task["task_id"], task["generation"], "worker", context_bundle_id=str(uuid4()), dependency_fingerprint="0" * 64, world_ref=wrong)
    assert exc.value.code == "TASK_REBIND_REQUIRED"
    bind_task(rel_env, task, rel_scope)
    with rel_env["db"].engine.begin() as conn:
        assert service.guard_task(conn, task["task_id"], task["generation"])["world_ref"] == task["world_ref"]


def test_expired_lease_recovery_fences_old_result(rel_env, rel_scope):
    task = start_task(rel_env, rel_scope)
    service = rel_env["service"]
    t = rel_env["tables"]["tasks"]
    with rel_env["db"].engine.begin() as conn:
        conn.execute(t.update().where(t.c.task_id == task["task_id"]).values(lease_expiry=func.clock_timestamp() - text("interval '1 second'")))
    with pytest.raises(DomainError) as exc:
        service.renew(task["task_id"], 1, "worker")
    assert exc.value.code == "LEASE_EXPIRED"
    recovered = service.recover_expired()
    assert any(r["task_id"] == task["task_id"] and r["state"] == "READY" for r in recovered)
    current = service.claim("replacement", task_id=task["task_id"])
    assert current["generation"] > 1 and current["context_bundle_id"] is None
    late = task_result(task)
    with pytest.raises(DomainError) as exc:
        service.store_result(late, worker_id="worker")
    assert exc.value.code == "STALE_GENERATION"
    rt = rel_env["tables"]["task_results"]
    with rel_env["db"].engine.connect() as conn:
        assert conn.scalar(select(rt.c.disposition).where(rt.c.result_id == late["result_id"])) == "LATE"
    assert service.get(task["task_id"])["current_result_id"] is None


@pytest.mark.concurrency
def test_cancel_and_result_race_leaves_no_qualification(rel_env, rel_scope):
    service = rel_env["service"]
    task = start_task(rel_env, rel_scope)
    result = task_result(task)
    barrier = threading.Barrier(2)
    def cancel():
        barrier.wait(timeout=5)
        return service.cancel(task["task_id"], 1, principal_id=rel_scope["principal_id"], idempotency_key="cancel-race")
    def finish():
        barrier.wait(timeout=5)
        try:
            return service.store_result(result, worker_id="worker")
        except DomainError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(cancel), pool.submit(finish)
        assert a.result()["status"] == "CANCELLED"
        b.result()
    assert service.get(task["task_id"])["state"] == "CANCELLED"
    with rel_env["db"].engine.begin() as conn, pytest.raises(DomainError) as exc:
        service.guard_task(conn, task["task_id"], 1)
    assert exc.value.code == "TASK_NOT_ACCEPTABLE"


def test_result_lifecycle_and_response_loss_retry(rel_env, rel_scope):
    task = start_task(rel_env, rel_scope)
    service = rel_env["service"]
    result = task_result(task)
    first = service.store_result(result, worker_id="worker")
    # First receipt may be lost by transport; same immutable result is retrieved.
    again = service.store_result(result, worker_id="worker")
    validate_contract("TaskReceipt", first, version="2.0")
    validate_contract("TaskReceipt", again, version="2.0")
    assert first["receipt_id"] == again["receipt_id"] and again["status"] == "ALREADY_RECORDED"
    with pytest.raises(DomainError):
        service.store_result({**result, "safe_summary": "changed"}, worker_id="worker")
    assert service.transition_result(task["task_id"], 1, "VALIDATING")["state"] == "VALIDATING"
    assert service.transition_result(task["task_id"], 1, "ACCEPTED")["state"] == "ACCEPTED"
    with rel_env["db"].engine.begin() as conn, pytest.raises(DomainError):
        service.guard_task(conn, task["task_id"], 1)


@pytest.mark.parametrize("state", ["RUNNING", "RESULT_STORED", "VALIDATING", "WAITING_REVIEW"])
def test_accept_in_world_transaction_closes_current_task(rel_env, rel_scope, state):
    service = rel_env["service"]
    task = start_task(rel_env, rel_scope)
    if state != "RUNNING":
        service.store_result(task_result(task), worker_id="worker")
    if state in {"VALIDATING", "WAITING_REVIEW"}:
        service.transition_result(task["task_id"], 1, "VALIDATING")
    if state == "WAITING_REVIEW":
        service.transition_result(task["task_id"], 1, "WAITING_REVIEW")
    with rel_env["db"].uow() as repo:
        accepted = service.accept_in_transaction(repo.conn, task["task_id"], 1)
        assert accepted["state"] == "ACCEPTED"
    current = service.get(task["task_id"])
    assert current["state"] == "ACCEPTED"
    assert current["lease_owner"] is None and current["lease_expiry"] is None
    with rel_env["db"].uow() as repo:
        assert len(repo.many("outbox", {"dedupe_key": f'task:{task["task_id"]}:1:ACCEPTED'})) == 1
        with pytest.raises(DomainError) as error:
            service.accept_in_transaction(repo.conn, task["task_id"], 1)
        assert error.value.code == "TASK_NOT_ACCEPTABLE"
    with pytest.raises(DomainError) as cancelled:
        service.cancel(task["task_id"], 1, principal_id=rel_scope["principal_id"], idempotency_key="cancel-after-accept")
    assert cancelled.value.code == "TASK_ALREADY_ACCEPTED"


def test_acceptance_rolls_back_with_outer_transaction_and_preserves_call_accounting(rel_env, rel_scope):
    service = rel_env["service"]
    task = start_task(rel_env, rel_scope)
    budget = service.create_budget(rel_scope["work_id"], "5.00")
    call = service.reserve_call(task["task_id"], 1, "worker", budget_id=budget, amount="1.00", call_key="before-accept", request_hash="1" * 64, token_amount=10)
    service.mark_call_sent(call["attempt_id"], worker_id="worker")
    with pytest.raises(RuntimeError, match="outer transaction failed"):
        with rel_env["db"].uow() as repo:
            service.accept_in_transaction(repo.conn, task["task_id"], 1)
            raise RuntimeError("outer transaction failed")
    assert service.get(task["task_id"])["state"] == "RUNNING"
    with rel_env["db"].uow() as repo:
        assert not repo.many("outbox", {"dedupe_key": f'task:{task["task_id"]}:1:ACCEPTED'})
        service.accept_in_transaction(repo.conn, task["task_id"], 1)
    # Closing a world task cannot erase or implicitly release outstanding
    # provider accounting; settlement of an already sent attempt stays valid.
    settled = service.settle_call(call["attempt_id"], state="SUCCEEDED", actual_amount="0.75", actual_tokens=7, provider_receipt="settled-after-accept")
    assert settled["state"] == "SUCCEEDED"
    assert service.get(task["task_id"])["state"] == "ACCEPTED"


@pytest.mark.concurrency
def test_budget_race_unknown_reservation_and_settlement(rel_env, rel_scope):
    service = rel_env["service"]
    left = start_task(rel_env, rel_scope, worker="left")
    right = start_task(rel_env, rel_scope, worker="right")
    budget = service.create_budget(rel_scope["work_id"], "1.00")
    barrier = threading.Barrier(2)
    def reserve(pair):
        task, worker = pair
        barrier.wait(timeout=5)
        try:
            return service.reserve_call(task["task_id"], 1, worker, budget_id=budget, amount="1.00", call_key="invoke", request_hash="1" * 64)
        except DomainError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        outputs = list(pool.map(reserve, [(left, "left"), (right, "right")]))
    success = [o for o in outputs if isinstance(o, dict)]
    assert len(success) == 1 and outputs.count("BUDGET_EXCEEDED") == 1
    call = success[0]
    worker = "left" if call["task_id"] == left["task_id"] else "right"
    service.mark_call_sent(call["attempt_id"], worker_id=worker)
    assert rel_env["db"].engine.pool.checkedout() == 0  # external wait consumes no DB connection
    service.settle_call(call["attempt_id"], state="UNKNOWN")
    b = rel_env["tables"]["budgets"]
    with rel_env["db"].engine.connect() as conn:
        row = conn.execute(select(b).where(b.c.budget_id == budget)).mappings().one()
        assert row["reserved_amount"] == Decimal("1") and row["spent_amount"] == 0
    service.settle_call(call["attempt_id"], state="SUCCEEDED", actual_amount="0.70", provider_receipt="isolated-receipt")
    service.settle_call(call["attempt_id"], state="SUCCEEDED", actual_amount="0.70", provider_receipt="isolated-receipt")
    with rel_env["db"].engine.connect() as conn:
        row = conn.execute(select(b).where(b.c.budget_id == budget)).mappings().one()
        assert row["reserved_amount"] == 0 and row["spent_amount"] == Decimal("0.70")


def test_unknown_call_recovery_pauses_until_reconciled(rel_env, rel_scope):
    service = rel_env["service"]
    task = start_task(rel_env, rel_scope)
    budget = service.create_budget(rel_scope["work_id"], "2")
    call = service.reserve_call(task["task_id"], 1, "worker", budget_id=budget, amount="1", call_key="unknown", request_hash="2" * 64)
    service.mark_call_sent(call["attempt_id"], worker_id="worker")
    t = rel_env["tables"]["tasks"]
    with rel_env["db"].engine.begin() as conn:
        conn.execute(t.update().where(t.c.task_id == task["task_id"]).values(lease_expiry=func.clock_timestamp() - text("interval '1 second'")))
    service.recover_expired()
    paused = service.get(task["task_id"])
    assert paused["state"] == "PAUSED"
    with pytest.raises(DomainError) as exc:
        service.resume(task["task_id"], paused["generation"])
    assert exc.value.code == "CALL_RECONCILIATION_REQUIRED"
    service.settle_call(call["attempt_id"], state="FAILED", actual_amount="0")
    service.resume(task["task_id"], paused["generation"])
    assert service.get(task["task_id"])["state"] == "READY"


@pytest.mark.concurrency
def test_persistent_scheduler_fairness_across_task_classes(rel_env, rel_scope):
    service = rel_env["service"]
    ids = []
    for kind in ["writing"] * 4 + ["review"] * 4:
        spec = task_spec(rel_scope, kind=kind)
        ids.append(spec["task_id"])
        service.create(spec, principal_id=rel_scope["principal_id"], idempotency_key=spec["task_id"])
    got = [service.claim("fair-worker", work_id=rel_scope["work_id"], task_kinds=["writing", "review"])["task_kind"] for _ in range(8)]
    assert all(set(got[i:i + 2]) == {"writing", "review"} for i in range(0, 8, 2))


def test_outbox_failure_redelivery_and_inbox_atomicity(rel_env, rel_scope):
    tables, engine = rel_env["tables"], rel_env["db"].engine
    budget = rel_env["service"].create_budget(rel_scope["work_id"], "10")
    outbox = OutboxService(engine, tables)
    with engine.begin() as conn:
        message = enqueue_outbox(conn, tables, "test.side_effect", "budget", budget, {"budget_id": budget}, f"once:{uuid4()}")
    dispatch = outbox.claim("dispatch", topic="test.side_effect")
    b = tables["budgets"]
    def fail(conn, payload):
        conn.execute(b.update().where(b.c.budget_id == payload["budget_id"]).values(spent_amount=Decimal("1")))
        raise RuntimeError("injected before transaction commit")
    with pytest.raises(RuntimeError):
        outbox.consume("budget-consumer", message, fail)
    with engine.connect() as conn:
        assert conn.scalar(select(b.c.spent_amount).where(b.c.budget_id == budget)) == 0
        assert conn.scalar(select(func.count()).select_from(tables["inbox"]).where(tables["inbox"].c.message_id == message)) == 0
    def apply(conn, payload):
        conn.execute(b.update().where(b.c.budget_id == payload["budget_id"]).values(spent_amount=b.c.spent_amount + 1))
    assert outbox.consume("budget-consumer", message, apply)
    # Consumer committed but acknowledgment was lost: reconsume is a no-op.
    assert not outbox.consume("budget-consumer", message, apply)
    o = tables["outbox"]
    with engine.begin() as conn:
        conn.execute(o.update().where(o.c.outbox_id == message).values(lease_expiry=func.clock_timestamp() - text("interval '1 second'")))
    replacement = outbox.claim("replacement", topic="test.side_effect")
    assert replacement["generation"] == dispatch["generation"] + 1
    with pytest.raises(DomainError):
        outbox.ack(message, dispatch["generation"], "dispatch")
    assert not outbox.consume("budget-consumer", message, apply)
    outbox.ack(message, replacement["generation"], "replacement")
    with engine.connect() as conn:
        assert conn.scalar(select(b.c.spent_amount).where(b.c.budget_id == budget)) == 1


def test_object_database_reference_constraints(rel_env):
    refs = rel_env["tables"]["object_refs"]
    with pytest.raises(IntegrityError):
        with rel_env["db"].engine.begin() as conn:
            conn.execute(insert(refs).values(owner_type="test", owner_id=str(uuid4()), object_hash="f" * 64))


def test_token_reservations_cannot_exceed_task_budget(rel_env, rel_scope):
    service = rel_env["service"]
    task = start_task(rel_env, rel_scope)
    budget = service.create_budget(rel_scope["work_id"], "5")
    call = service.reserve_call(task["task_id"], 1, "worker", budget_id=budget, amount="1", call_key="token-max", request_hash="3" * 64, token_amount=12000)
    with pytest.raises(DomainError) as exc:
        service.reserve_call(task["task_id"], 1, "worker", budget_id=budget, amount="0", call_key="token-over", request_hash="3" * 64, token_amount=1)
    assert exc.value.code == "BUDGET_EXCEEDED"
    with pytest.raises(DomainError) as exc:
        service.settle_call(call["attempt_id"], state="SUCCEEDED", actual_amount="0.50", actual_tokens=1000)
    assert exc.value.code == "INVALID_CALL_TRANSITION"
    service.mark_call_sent(call["attempt_id"], worker_id="worker")
    service.settle_call(call["attempt_id"], state="UNKNOWN")
    with pytest.raises(DomainError):
        service.reserve_call(task["task_id"], 1, "worker", budget_id=budget, amount="0", call_key="token-over", request_hash="3" * 64, token_amount=1)
    service.settle_call(call["attempt_id"], state="SUCCEEDED", actual_amount="0.50", actual_tokens=11000)
    next_call = service.reserve_call(task["task_id"], 1, "worker", budget_id=budget, amount="1", call_key="token-remainder", request_hash="3" * 64, token_amount=1000)
    assert next_call["reserved_tokens"] == 1000


def test_create_deadline_precedes_identity_conflict_but_not_exact_replay(rel_env, rel_scope):
    service = rel_env["service"]
    spec = task_spec(rel_scope)
    principal = rel_scope["principal_id"]
    first = service.create(spec, principal_id=principal, idempotency_key="deadline-order")
    for next_spec, expected in (
        (spec, "ALREADY_EXISTS"),
        ({**spec, "wall_deadline": "2000-01-01T00:00:00+00:00"}, "DEADLINE_EXCEEDED"),
    ):
        with pytest.raises(DomainError) as error:
            service.create(next_spec, principal_id=principal, idempotency_key="new-key")
        assert error.value.code == expected
    assert service.create(spec, principal_id=principal, idempotency_key="deadline-order") == first
