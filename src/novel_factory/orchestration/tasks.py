"""Short-transaction task leases, call accounting and durable local delivery.

This is an internal application service. The public application gateway must
resolve the current trusted principal/grant and authorize its work/task scope;
callers cannot acquire authorization merely by choosing principal_id.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from typing import Callable, Mapping
from uuid import uuid4

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert

from novel_factory.contracts.registry import validate_contract
from novel_factory.domain.errors import DomainError

MAINTENANCE_GATE = 721943001


def mutation_gate(conn) -> None:
    conn.execute(text("SELECT pg_advisory_xact_lock_shared(:key)"), {"key": MAINTENANCE_GATE})


def _canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _hash(value) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _error(code, message, *, retryable=False):
    return DomainError(code, message, retryable=retryable)


def _money(value) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise _error("INVALID_ARGUMENT", "Invalid monetary amount") from exc
    if not amount.is_finite() or amount < 0 or amount != amount.quantize(Decimal("0.000001")):
        raise _error("INVALID_ARGUMENT", "Amount must be nonnegative with at most six decimal places")
    return amount


def enqueue_outbox(conn, tables, topic, aggregate_type, aggregate_id, payload, dedupe_key):
    """Called inside the authoritative mutation transaction, never afterward."""
    t = tables["outbox"]
    value = {"outbox_id": str(uuid4()), "topic": topic, "aggregate_type": aggregate_type,
             "aggregate_id": str(aggregate_id), "payload": payload, "dedupe_key": dedupe_key}
    result = conn.execute(insert(t).values(**value).on_conflict_do_nothing(index_elements=[t.c.dedupe_key]).returning(t.c.outbox_id)).scalar_one_or_none()
    if result:
        return result
    existing = conn.execute(select(t).where(t.c.dedupe_key == dedupe_key)).mappings().one()
    if any(existing[k] != value[k] for k in ("topic", "aggregate_type", "aggregate_id", "payload")):
        raise _error("IDEMPOTENCY_CONFLICT", "Outbox identity already has different content")
    return existing["outbox_id"]


class TaskService:
    def __init__(self, engine, tables: Mapping, object_store=None):
        self.engine, self.tables, self.objects = engine, tables, object_store

    @contextmanager
    def _tx(self, authorize_in_tx=None):
        with self.engine.begin() as conn:
            mutation_gate(conn)
            if authorize_in_tx is not None:
                authorize_in_tx(conn)
            yield conn

    @staticmethod
    def _now(conn):
        return conn.scalar(select(func.clock_timestamp()))

    def _task(self, conn, task_id, *, lock=True):
        q = select(self.tables["tasks"]).where(self.tables["tasks"].c.task_id == str(task_id))
        if lock:
            q = q.with_for_update()
        row = conn.execute(q).mappings().one_or_none()
        if row is None:
            raise _error("NOT_FOUND", "Task is unavailable")
        return dict(row)

    def _spec(self, conn, task):
        t = self.tables["task_inputs"]
        return conn.execute(select(t.c.spec).where(t.c.task_id == task["task_id"], t.c.generation == task["generation"])).scalar_one()

    def _save_input(self, conn, task_id, generation, spec, state):
        snapshot = {**spec, "generation": generation, "state": state}
        conn.execute(insert(self.tables["task_inputs"]).values(task_id=task_id, generation=generation, spec=snapshot, input_hash=_hash(snapshot)))

    def _command_prior(self, conn, principal_id, key, content):
        if not key or len(key) > 256:
            raise _error("INVALID_ARGUMENT", "Invalid idempotency key")
        lock = int.from_bytes(hashlib.sha256((str(principal_id) + "\0" + key).encode()).digest()[:8], "big", signed=True)
        conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock})
        table = self.tables["task_commands"]
        row = conn.execute(select(table).where(table.c.principal_id == str(principal_id), table.c.idempotency_key == key)).mappings().one_or_none()
        if row and row["command_hash"] != _hash(content):
            raise _error("IDEMPOTENCY_CONFLICT", "Idempotency key has different content")
        return dict(row["receipt"]) if row else None

    def _command_record(self, conn, principal_id, key, content, receipt):
        conn.execute(insert(self.tables["task_commands"]).values(principal_id=str(principal_id), idempotency_key=key, command_hash=_hash(content), receipt=receipt))

    def _emit(self, conn, task_id, generation, state):
        return enqueue_outbox(conn, self.tables, "task.changed", "task", task_id,
                              {"task_id": task_id, "generation": generation, "state": state},
                              f"task:{task_id}:{generation}:{state}")

    def create(self, spec: dict, *, principal_id: str, idempotency_key: str, authorize_in_tx=None):
        validate_contract("TaskSpec", spec, version="2.0")
        if spec["generation"] != 0 or spec["state"] not in {"READY", "BLOCKED"}:
            raise _error("INVALID_ARGUMENT", "New tasks start in generation zero, READY or BLOCKED")
        deadline = datetime.fromisoformat(spec["wall_deadline"].replace("Z", "+00:00"))
        content = {"operation": "create", "spec": spec}
        with self._tx(authorize_in_tx) as conn:
            prior = self._command_prior(conn, principal_id, idempotency_key, content)
            if prior:
                return prior
            if deadline <= self._now(conn):
                raise _error("DEADLINE_EXCEEDED", "Task deadline has passed")
            t = self.tables["tasks"]
            if conn.scalar(select(t.c.task_id).where(t.c.task_id == spec["task_id"])):
                raise _error("ALREADY_EXISTS", "Task identity is already assigned")
            wr = spec["world_ref"]
            conn.execute(insert(t).values(task_id=spec["task_id"], work_id=wr["work_id"], branch_id=wr["branch_id"], principal_id=str(principal_id), grant_id=spec["capability_grant_id"], role_id=spec["role_id"], task_kind=spec["task_kind"], state=spec["state"], generation=0, context_bundle_id=spec["context_bundle_id"], dependency_fingerprint=spec["dependency_fingerprint"], deadline=deadline))
            self._save_input(conn, spec["task_id"], 0, spec, spec["state"])
            conn.execute(insert(self.tables["task_fairness"]).values(work_id=wr["work_id"], task_kind=spec["task_kind"]).on_conflict_do_nothing())
            receipt = {"task_id": spec["task_id"], "generation": 0, "state": spec["state"]}
            self._command_record(conn, principal_id, idempotency_key, content, receipt)
            self._emit(conn, spec["task_id"], 0, spec["state"])
            return receipt

    def get(self, task_id):
        with self.engine.connect() as conn:
            row = self._task(conn, task_id, lock=False)
            row["world_ref"] = self._spec(conn, row)["world_ref"]
            return row

    def plan_binding_exists(self, task_id, work_id):
        """An immutable plan selection pins its node guards to one task generation."""
        inputs, selections = self.tables["task_inputs"], self.tables["selections"]
        key = func.concat_ws(":", "PLAN_TASK", inputs.c.task_id, inputs.c.generation)
        return (select(1).select_from(inputs.join(selections, selections.c.selection_kind == key))
                .where(inputs.c.task_id == task_id, selections.c.work_id == work_id)
                .correlate_except(inputs, selections).exists())

    def claim(self, worker_id: str, *, work_id=None, task_kinds=None, principal_id=None, grant_id=None, task_id=None, lease_seconds=30, authorize_in_tx=None):
        with self._tx(authorize_in_tx) as conn:
            return self.claim_in_transaction(conn, worker_id, work_id=work_id, task_kinds=task_kinds,
                principal_id=principal_id, grant_id=grant_id, task_id=task_id, lease_seconds=lease_seconds)

    def claim_in_transaction(self, conn, worker_id, *, work_id=None, task_kinds=None, principal_id=None,
                             grant_id=None, task_id=None, lease_seconds=30):
        if not worker_id or not 0 < lease_seconds <= 3600:
            raise _error("INVALID_ARGUMENT", "Invalid worker lease")
        t, fair = self.tables["tasks"], self.tables["task_fairness"]
        now = self._now(conn)
        # A short queue-only scheduler lock produces persistent round-robin
        # shares. It never participates in world acceptance or model waits.
        conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": MAINTENANCE_GATE + 1})
        conditions = [t.c.state == "READY", t.c.deadline > now,
                      or_(t.c.generation == 0, ~self.plan_binding_exists(t.c.task_id, t.c.work_id))]
        if work_id:
            conditions.append(t.c.work_id == str(work_id))
        if task_kinds:
            conditions.append(t.c.task_kind.in_(task_kinds))
        if principal_id:
            conditions.append(t.c.principal_id == str(principal_id))
        if grant_id:
            conditions.append(t.c.grant_id == str(grant_id))
        if task_id:
            conditions.append(t.c.task_id == str(task_id))
        q = select(t).join(fair, and_(t.c.work_id == fair.c.work_id, t.c.task_kind == fair.c.task_kind)).where(*conditions).order_by(fair.c.last_served_at, t.c.created_at, t.c.task_id).limit(1).with_for_update(skip_locked=True, of=t)
        row = conn.execute(q).mappings().one_or_none()
        if row is None:
            return None
        task = dict(row)
        spec = {**self._spec(conn, task), "context_bundle_id": None, "dependency_fingerprint": None}
        if (task["role_id"], task["task_kind"]) == ("volume_director", "volume_review"):
            from novel_factory.orchestration.workflow import review_acceptance
            spec["world_ref"] = review_acceptance(conn, task, spec)["world_ref"]
        generation = task["generation"] + 1
        attempt_id = str(uuid4())
        updates = {"state": "RUNNING", "generation": generation, "attempt_id": attempt_id,
                   "lease_owner": worker_id, "lease_expiry": now + timedelta(seconds=lease_seconds), "updated_at": now, "current_result_id": None, "context_bundle_id": None, "dependency_fingerprint": None}
        conn.execute(t.update().where(t.c.task_id == task["task_id"]).values(**updates))
        self._save_input(conn, task["task_id"], generation, spec, "RUNNING")
        conn.execute(insert(self.tables["task_executions"]).values(attempt_id=attempt_id, task_id=task["task_id"], generation=generation, worker_id=worker_id))
        conn.execute(fair.update().where(fair.c.work_id == task["work_id"], fair.c.task_kind == task["task_kind"]).values(last_served_at=now, claims=fair.c.claims + 1))
        self._emit(conn, task["task_id"], generation, "RUNNING")
        return {**task, **updates, "world_ref": spec["world_ref"]}

    def guard_task(self, conn, task_id, generation, worker_id=None, *, require_context=True):
        task = self._task(conn, task_id)
        if task["generation"] != generation:
            raise _error("STALE_GENERATION", "Task generation is no longer current")
        if task["state"] not in {"RUNNING", "RESULT_STORED", "VALIDATING", "WAITING_REVIEW"}:
            raise _error("TASK_NOT_ACCEPTABLE", "Task has no current execution qualification")
        now = self._now(conn)
        if task["deadline"] <= now:
            raise _error("DEADLINE_EXCEEDED", "Task deadline has passed")
        if task["state"] == "RUNNING" and (task["lease_expiry"] is None or task["lease_expiry"] <= now):
            raise _error("LEASE_EXPIRED", "Task execution lease has expired")
        if worker_id is not None and task["lease_owner"] != worker_id:
            raise _error("LEASE_OWNER_MISMATCH", "Worker does not own this lease")
        if require_context and (task["context_bundle_id"] is None or task["dependency_fingerprint"] is None):
            raise _error("CONTEXT_REQUIRED", "Current task has no persisted context binding")
        task["world_ref"] = self._spec(conn, task)["world_ref"]
        if require_context:
            self._check_context(conn, task, task["context_bundle_id"], task["dependency_fingerprint"])
        return task

    def _check_context(self, conn, task, context_bundle_id, dependency_fingerprint):
        contexts = self.tables["contexts"]
        context = conn.execute(select(contexts).where(contexts.c.context_bundle_id == str(context_bundle_id))).mappings().one_or_none()
        if not context or any(str(context[k]) != str(task[k]) for k in ("task_id", "generation", "work_id", "principal_id", "grant_id")):
            raise _error("CONTEXT_MISMATCH", "Context is not persisted for this task generation and grant")
        if context["dependency_fingerprint"] != dependency_fingerprint or context["payload"].get("world_ref") != task["world_ref"]:
            raise _error("CONTEXT_MISMATCH", "Context does not bind the immutable task input")

    def bind_context_in_transaction(self, conn, task_id, generation, worker_id, *, context_bundle_id, dependency_fingerprint, world_ref):
        task = self.guard_task(conn, task_id, generation, worker_id, require_context=False)
        if task["world_ref"] != world_ref:
            raise _error("TASK_REBIND_REQUIRED", "Context WorldRef differs from immutable task input")
        if task["context_bundle_id"] not in {None, str(context_bundle_id)} or task["dependency_fingerprint"] not in {None, dependency_fingerprint}:
            raise _error("TASK_REBIND_REQUIRED", "Existing context binding is immutable")
        if not context_bundle_id or not isinstance(dependency_fingerprint, str) or len(dependency_fingerprint) != 64:
            raise _error("INVALID_ARGUMENT", "Invalid context binding")
        self._check_context(conn, task, context_bundle_id, dependency_fingerprint)
        t = self.tables["tasks"]
        conn.execute(t.update().where(t.c.task_id == task_id).values(context_bundle_id=str(context_bundle_id), dependency_fingerprint=dependency_fingerprint, updated_at=self._now(conn)))
        return {"task_id": str(task_id), "generation": generation, "context_bundle_id": str(context_bundle_id)}

    def bind_context(self, task_id, generation, worker_id, *, context_bundle_id, dependency_fingerprint, world_ref):
        # ContextCompiler can use the in_transaction variant so the persisted
        # context and task binding commit atomically.
        with self._tx() as conn:
            return self.bind_context_in_transaction(conn, task_id, generation, worker_id, context_bundle_id=context_bundle_id,
                                                    dependency_fingerprint=dependency_fingerprint, world_ref=world_ref)

    def renew(self, task_id, generation, worker_id, *, lease_seconds=30):
        if not 0 < lease_seconds <= 3600:
            raise _error("INVALID_ARGUMENT", "Invalid lease duration")
        with self._tx() as conn:
            task = self.guard_task(conn, task_id, generation, worker_id, require_context=False)
            if task["state"] != "RUNNING":
                raise _error("TASK_NOT_ACCEPTABLE", "Only RUNNING tasks can renew a lease")
            expiry = min(self._now(conn) + timedelta(seconds=lease_seconds), task["deadline"])
            t = self.tables["tasks"]
            conn.execute(t.update().where(t.c.task_id == task_id).values(lease_expiry=expiry, updated_at=self._now(conn)))
            return expiry

    def _register_object(self, conn, ref, owner_type, owner_id):
        objects = self.tables["objects"]
        conn.execute(insert(objects).values(hash=ref.sha256, size=ref.size).on_conflict_do_nothing(index_elements=[objects.c.hash]))
        refs = self.tables["object_refs"]
        conn.execute(insert(refs).values(owner_type=owner_type, owner_id=str(owner_id), object_hash=ref.sha256).on_conflict_do_nothing())

    def store_result(self, result: dict, *, worker_id: str, authorize_in_tx=None):
        validate_contract("TaskResult", result, version="2.0")
        if self.objects is None:
            raise _error("OBJECT_STORE_REQUIRED", "Task results require durable object storage")
        payload_ref = self.objects.put(_canonical(result))
        rejected = None
        receipt = None
        with self.objects.lock():
            self.objects.verify(payload_ref.sha256)
            artifact_refs = [self.objects.verify(a["sha256"]) for a in result["artifacts"]]
            with self._tx(authorize_in_tx) as conn:
                task = self._task(conn, result["task_id"])
                rt = self.tables["task_results"]
                old = conn.execute(select(rt).where(rt.c.result_id == result["result_id"])).mappings().one_or_none()
                if old:
                    if old["payload_hash"] != payload_ref.sha256:
                        raise _error("IDEMPOTENCY_CONFLICT", "Result identity has different content")
                    return {"task_id": task["task_id"], "generation": result["generation"], "receipt_id": result["result_id"], "status": "ALREADY_RECORDED"}
                try:
                    self.guard_task(conn, task["task_id"], result["generation"], worker_id)
                    if task["state"] != "RUNNING":
                        raise _error("TASK_NOT_ACCEPTABLE", "Current generation already has a result")
                    if task["attempt_id"] != result["attempt_id"]:
                        raise _error("ATTEMPT_MISMATCH", "Result attempt does not belong to this execution")
                    if task["context_bundle_id"] != result["context_bundle_id"] or task["dependency_fingerprint"] != result["dependency_fingerprint"]:
                        raise _error("CONTEXT_MISMATCH", "Result does not bind the current input")
                except DomainError as exc:
                    rejected = exc
                disposition = "LATE" if rejected else "CURRENT"
                self._register_object(conn, payload_ref, "task_result", result["result_id"])
                for ref in artifact_refs:
                    self._register_object(conn, ref, "task_result", result["result_id"])
                conn.execute(insert(rt).values(result_id=result["result_id"], task_id=result["task_id"], generation=result["generation"], attempt_id=result["attempt_id"], payload_hash=payload_ref.sha256, payload=result, disposition=disposition))
                if not rejected:
                    state = "RESULT_STORED" if result["status"] == "SUCCEEDED" else ("BLOCKED" if result["status"] == "NEEDS_INPUT" else "FAILED")
                    t = self.tables["tasks"]
                    conn.execute(t.update().where(t.c.task_id == task["task_id"]).values(state=state, current_result_id=result["result_id"], lease_expiry=None, updated_at=self._now(conn)))
                    self._emit(conn, task["task_id"], task["generation"], state)
                    receipt = {"task_id": task["task_id"], "generation": task["generation"], "receipt_id": result["result_id"], "status": "RESULT_STORED"}
        if rejected:
            # Deliberately raised after commit so late output remains auditable.
            raise rejected
        return receipt

    def cancel(self, task_id, expected_generation, *, principal_id, idempotency_key, reason="cancelled", authorize_in_tx=None):
        content = {"operation": "cancel", "task_id": str(task_id), "expected_generation": expected_generation, "reason": reason}
        with self._tx(authorize_in_tx) as conn:
            prior = self._command_prior(conn, principal_id, idempotency_key, content)
            if prior:
                return prior
            task = self._task(conn, task_id)
            if task["generation"] != expected_generation:
                raise _error("STALE_GENERATION", "Cancellation expected another generation")
            if task["state"] == "ACCEPTED":
                raise _error("TASK_ALREADY_ACCEPTED", "Cancellation cannot erase accepted work")
            if task["state"] != "CANCELLED":
                t = self.tables["tasks"]
                conn.execute(t.update().where(t.c.task_id == task_id).values(state="CANCELLED", lease_expiry=None, updated_at=self._now(conn)))
                self._emit(conn, task_id, expected_generation, "CANCELLED")
            receipt = {"task_id": str(task_id), "generation": expected_generation, "receipt_id": str(uuid4()), "status": "CANCELLED"}
            self._command_record(conn, principal_id, idempotency_key, content, receipt)
            return receipt

    def fail(self, task_id, generation, worker_id, *, retryable=False):
        with self._tx() as conn:
            task = self.guard_task(conn, task_id, generation, worker_id, require_context=False)
            if task["state"] != "RUNNING":
                raise _error("TASK_NOT_ACCEPTABLE", "Only active execution can fail")
            if retryable:
                self._recover_task(conn, task)
            else:
                t = self.tables["tasks"]
                conn.execute(t.update().where(t.c.task_id == task_id).values(state="FAILED", lease_expiry=None, updated_at=self._now(conn)))
                self._emit(conn, task_id, generation, "FAILED")

    def transition_result(self, task_id, generation, new_state):
        transitions = {"RESULT_STORED": {"VALIDATING"}, "VALIDATING": {"ACCEPTED", "WAITING_REVIEW", "FAILED", "STALE"}, "WAITING_REVIEW": {"ACCEPTED", "FAILED", "STALE"}}
        with self._tx() as conn:
            task = self.guard_task(conn, task_id, generation)
            if new_state not in transitions.get(task["state"], set()):
                raise _error("INVALID_TASK_TRANSITION", "Invalid result-validation transition")
            if new_state == "ACCEPTED":
                return self.accept_in_transaction(conn, task_id, generation)
            t = self.tables["tasks"]
            conn.execute(t.update().where(t.c.task_id == task_id).values(state=new_state, updated_at=self._now(conn)))
            self._emit(conn, task_id, generation, new_state)
            return {"task_id": str(task_id), "generation": generation, "state": new_state}

    def accept_in_transaction(self, conn, task_id, generation):
        """Close one task in the transaction accepting its authorized product.

        The caller already holds the maintenance/auth/branch guards in order.
        No separate commit may split a formal world receipt from this terminal
        state. The active generation can be closed once; receipt retries are
        handled by the product service before reaching this guard. Existing
        external-call reservations remain for explicit settlement/reconciliation.
        """
        self.guard_task(conn, task_id, generation)
        t = self.tables["tasks"]
        conn.execute(t.update().where(t.c.task_id == task_id).values(
            state="ACCEPTED", lease_owner=None, lease_expiry=None, updated_at=self._now(conn)))
        self._emit(conn, str(task_id), generation, "ACCEPTED")
        return {"task_id": str(task_id), "generation": generation, "state": "ACCEPTED"}

    def _recover_task(self, conn, task):
        calls = self.tables["call_attempts"]
        conn.execute(calls.update().where(calls.c.task_id == task["task_id"], calls.c.generation == task["generation"], calls.c.state == "SENT").values(state="UNKNOWN", updated_at=self._now(conn)))
        unresolved = conn.scalar(select(func.count()).select_from(calls).where(calls.c.task_id == task["task_id"], calls.c.state.in_(["UNKNOWN", "PREPARED"])))
        # PREPARED reservations also require explicit reconciliation before a
        # new external attempt; they cannot silently disappear on restart.
        if unresolved:
            state = "PAUSED"
        elif task["deadline"] <= self._now(conn):
            state = "FAILED"
        elif conn.scalar(select(self.plan_binding_exists(task["task_id"], task["work_id"]))):
            state = "STALE"
        else:
            state = "READY"
        next_generation = task["generation"] + 1
        spec = self._spec(conn, task)
        spec = {**spec, "context_bundle_id": task["context_bundle_id"], "dependency_fingerprint": task["dependency_fingerprint"]}
        self._save_input(conn, task["task_id"], next_generation, spec, state)
        t = self.tables["tasks"]
        conn.execute(t.update().where(t.c.task_id == task["task_id"]).values(state=state, generation=next_generation, lease_owner=None, lease_expiry=None, attempt_id=None, updated_at=self._now(conn)))
        self._emit(conn, task["task_id"], next_generation, state)
        return {"task_id": task["task_id"], "generation": next_generation, "state": state}

    def recover_expired(self, *, limit=100):
        if not 0 < limit <= 1000:
            raise _error("INVALID_ARGUMENT", "Recovery batch must be bounded")
        with self._tx() as conn:
            t = self.tables["tasks"]
            rows = conn.execute(select(t).where(t.c.state == "RUNNING", t.c.lease_expiry <= self._now(conn)).order_by(t.c.task_id).limit(limit).with_for_update(skip_locked=True)).mappings().all()
            return [self._recover_task(conn, dict(row)) for row in rows]

    def resume(self, task_id, expected_generation):
        with self._tx() as conn:
            task = self._task(conn, task_id)
            if task["state"] == "BLOCKED":
                raise _error("WAIT_CONDITION_REQUIRED", "Blocked tasks require a verified workflow event")
            if task["generation"] != expected_generation or task["state"] != "PAUSED":
                raise _error("INVALID_TASK_TRANSITION", "Task is not awaiting an authorized resume")
            calls = self.tables["call_attempts"]
            if conn.scalar(select(func.count()).select_from(calls).where(calls.c.task_id == task_id, calls.c.state.in_(["PREPARED", "SENT", "UNKNOWN"]))):
                raise _error("CALL_RECONCILIATION_REQUIRED", "Unresolved calls still occupy budget")
            if conn.scalar(select(self.plan_binding_exists(task["task_id"], task["work_id"]))):
                raise _error("TASK_REBIND_REQUIRED", "Provision fresh plan guards for a new task")
            t = self.tables["tasks"]
            conn.execute(t.update().where(t.c.task_id == task_id).values(state="READY", updated_at=self._now(conn)))
            self._emit(conn, task_id, expected_generation, "READY")

    def create_budget(self, work_id, limit_amount, *, currency="USD", budget_id=None):
        amount = _money(limit_amount)
        if currency not in {"USD", "CNY"}:
            raise _error("INVALID_ARGUMENT", "Unsupported currency")
        budget_id = str(budget_id or uuid4())
        with self._tx() as conn:
            conn.execute(insert(self.tables["budgets"]).values(budget_id=budget_id, work_id=str(work_id), currency=currency, limit_amount=amount))
        return budget_id

    def reserve_call(self, task_id, generation, worker_id, *, budget_id, amount, call_key, request_hash, token_amount=0, authorize_in_tx=None):
        reserve = _money(amount)
        if type(token_amount) is not int or token_amount < 0:
            raise _error("INVALID_ARGUMENT", "Token reservation must be a nonnegative integer")
        if len(request_hash) != 64 or not call_key:
            raise _error("INVALID_ARGUMENT", "Call requires request hash and stable identity")
        with self._tx(authorize_in_tx) as conn:
            task = self.guard_task(conn, task_id, generation, worker_id)
            if task["state"] != "RUNNING":
                raise _error("TASK_NOT_ACCEPTABLE", "External calls require a live RUNNING execution")
            budgets, calls = self.tables["budgets"], self.tables["call_attempts"]
            budget = conn.execute(select(budgets).where(budgets.c.budget_id == budget_id).with_for_update()).mappings().one_or_none()
            if not budget or budget["work_id"] != task["work_id"]:
                raise _error("PERMISSION_DENIED", "Budget is not available for this task")
            existing = conn.execute(select(calls).where(calls.c.task_id == task_id, calls.c.generation == generation, calls.c.call_key == call_key)).mappings().one_or_none()
            if existing:
                if existing["request_hash"] != request_hash or existing["reserved_amount"] != reserve or existing["budget_id"] != budget_id or existing["reserved_tokens"] != token_amount:
                    raise _error("IDEMPOTENCY_CONFLICT", "Call identity has different content")
                return dict(existing)
            if budget["spent_amount"] + budget["reserved_amount"] + reserve > budget["limit_amount"]:
                raise _error("BUDGET_EXCEEDED", "Call cannot be reserved within the budget")
            spec = self._spec(conn, task)
            task_reserved = conn.scalar(select(func.coalesce(func.sum(calls.c.reserved_amount), 0)).where(calls.c.task_id == task_id, calls.c.state.in_(["PREPARED", "SENT", "UNKNOWN"])))
            task_spent = conn.scalar(select(func.coalesce(func.sum(calls.c.actual_amount), 0)).where(calls.c.task_id == task_id, calls.c.state.in_(["SUCCEEDED", "FAILED"])))
            if spec["budget"]["currency"] != budget["currency"] or task_reserved + task_spent + reserve > Decimal(spec["budget"]["max_amount"]):
                raise _error("BUDGET_EXCEEDED", "Task monetary budget would be exceeded")
            task_reserved_tokens = conn.scalar(select(func.coalesce(func.sum(calls.c.reserved_tokens), 0)).where(calls.c.task_id == task_id, calls.c.state.in_(["PREPARED", "SENT", "UNKNOWN"])))
            task_spent_tokens = conn.scalar(select(func.coalesce(func.sum(calls.c.actual_tokens), 0)).where(calls.c.task_id == task_id, calls.c.state.in_(["SUCCEEDED", "FAILED"])))
            if task_reserved_tokens + task_spent_tokens + token_amount > spec["budget"]["token_budget"]:
                raise _error("BUDGET_EXCEEDED", "Task token budget would be exceeded")
            attempt_id = str(uuid4())
            conn.execute(budgets.update().where(budgets.c.budget_id == budget_id).values(reserved_amount=budgets.c.reserved_amount + reserve))
            conn.execute(insert(calls).values(attempt_id=attempt_id, task_id=task_id, generation=generation, budget_id=budget_id, call_key=call_key, request_hash=request_hash, state="PREPARED", reserved_amount=reserve, reserved_tokens=token_amount))
            return {"attempt_id": attempt_id, "task_id": str(task_id), "generation": generation, "state": "PREPARED", "reserved_amount": reserve, "reserved_tokens": token_amount}

    def mark_call_sent(self, attempt_id, *, worker_id, authorize_in_tx=None):
        """Claim the single right to dispatch this reservation.

        False means another worker has already taken that right. It never means
        the remote provider did not execute; unknown calls require reconciliation.
        """
        with self._tx(authorize_in_tx) as conn:
            calls = self.tables["call_attempts"]
            call = conn.execute(select(calls).where(calls.c.attempt_id == attempt_id)).mappings().one()
            task = self.guard_task(conn, call["task_id"], call["generation"], worker_id)
            if task["state"] != "RUNNING":
                raise _error("TASK_NOT_ACCEPTABLE", "External dispatch requires a live RUNNING execution")
            call = conn.execute(select(calls).where(calls.c.attempt_id == attempt_id).with_for_update()).mappings().one()
            if call["state"] == "SENT":
                return False
            if call["state"] != "PREPARED":
                raise _error("INVALID_CALL_TRANSITION", "Call is not prepared")
            conn.execute(calls.update().where(calls.c.attempt_id == attempt_id).values(state="SENT", updated_at=self._now(conn)))
        # Only now may a worker release the connection and perform network IO.
        return True

    def settle_call(self, attempt_id, *, state, actual_amount=None, actual_tokens=0, provider_receipt=None):
        if state not in {"SUCCEEDED", "FAILED", "UNKNOWN"}:
            raise _error("INVALID_ARGUMENT", "Invalid settlement state")
        if type(actual_tokens) is not int or actual_tokens < 0:
            raise _error("INVALID_ARGUMENT", "Actual token use must be a nonnegative integer")
        actual = None if state == "UNKNOWN" else _money(actual_amount if actual_amount is not None else 0)
        settled_tokens = None if state == "UNKNOWN" else actual_tokens
        with self._tx() as conn:
            calls, budgets = self.tables["call_attempts"], self.tables["budgets"]
            call = conn.execute(select(calls).where(calls.c.attempt_id == attempt_id)).mappings().one()
            # Same lock order as reservation: task -> budget -> call.
            self._task(conn, call["task_id"])
            conn.execute(select(budgets).where(budgets.c.budget_id == call["budget_id"]).with_for_update()).mappings().one()
            call = conn.execute(select(calls).where(calls.c.attempt_id == attempt_id).with_for_update()).mappings().one()
            if call["state"] in {"SUCCEEDED", "FAILED"}:
                if call["state"] != state or call["actual_amount"] != actual or call["provider_receipt"] != provider_receipt or call["actual_tokens"] != settled_tokens:
                    raise _error("IDEMPOTENCY_CONFLICT", "Settled call has different accounting")
                return dict(call)
            if call["state"] == "PREPARED" and (state != "FAILED" or actual != 0 or settled_tokens != 0):
                raise _error("INVALID_CALL_TRANSITION", "Unsent reservations may only be cancelled with zero usage")
            if state == "UNKNOWN":
                if call["state"] not in {"SENT", "UNKNOWN"}:
                    raise _error("INVALID_CALL_TRANSITION", "Only a sent call can have unknown outcome")
            elif actual > call["reserved_amount"] or actual_tokens > call["reserved_tokens"]:
                raise _error("BUDGET_SETTLEMENT_EXCEEDS_RESERVATION", "Cost exceeds the reserved maximum; reconcile explicitly")
            if state != "UNKNOWN":
                conn.execute(budgets.update().where(budgets.c.budget_id == call["budget_id"]).values(reserved_amount=budgets.c.reserved_amount - call["reserved_amount"], spent_amount=budgets.c.spent_amount + actual))
            conn.execute(calls.update().where(calls.c.attempt_id == attempt_id).values(state=state, actual_amount=actual, actual_tokens=settled_tokens, provider_receipt=provider_receipt, updated_at=self._now(conn)))
            return {"attempt_id": str(attempt_id), "state": state, "actual_amount": actual, "actual_tokens": settled_tokens}


class OutboxService:
    """At-least-once dispatch and transactional inbox for local DB consumers."""
    def __init__(self, engine, tables):
        self.engine, self.tables = engine, tables

    def claim(self, worker_id, *, lease_seconds=30, topic=None):
        if not worker_id or not 0 < lease_seconds <= 3600:
            raise _error("INVALID_ARGUMENT", "Invalid outbox lease")
        with self.engine.begin() as conn:
            mutation_gate(conn)
            now = conn.scalar(select(func.clock_timestamp()))
            t = self.tables["outbox"]
            condition = or_(t.c.state == "PENDING", and_(t.c.state == "DELIVERING", t.c.lease_expiry <= now))
            q = select(t).where(condition)
            if topic:
                q = q.where(t.c.topic == topic)
            row = conn.execute(q.order_by(t.c.created_at, t.c.outbox_id).limit(1).with_for_update(skip_locked=True)).mappings().one_or_none()
            if row is None:
                return None
            updates = {"state": "DELIVERING", "generation": row["generation"] + 1, "lease_owner": worker_id,
                       "lease_expiry": now + timedelta(seconds=lease_seconds), "attempts": row["attempts"] + 1}
            conn.execute(t.update().where(t.c.outbox_id == row["outbox_id"]).values(**updates))
            return {**dict(row), **updates}

    def consume(self, consumer, message_id, handler: Callable):
        """handler(conn, payload) must only mutate this database transaction.

        External HTTP/email/model side effects cannot be made exactly-once by
        this local inbox. They are outside P1 and deliberately unsupported.
        """
        with self.engine.begin() as conn:
            mutation_gate(conn)
            t, inbox = self.tables["outbox"], self.tables["inbox"]
            row = conn.execute(select(t).where(t.c.outbox_id == message_id)).mappings().one_or_none()
            if row is None:
                raise _error("NOT_FOUND", "Outbox message is unavailable")
            inserted = conn.execute(insert(inbox).values(consumer=consumer, message_id=message_id).on_conflict_do_nothing().returning(inbox.c.message_id)).scalar_one_or_none()
            if inserted is None:
                return False
            handler(conn, dict(row["payload"]))
            return True

    def ack(self, message_id, generation, worker_id):
        with self.engine.begin() as conn:
            mutation_gate(conn)
            t = self.tables["outbox"]
            now = conn.scalar(select(func.clock_timestamp()))
            row = conn.execute(select(t).where(t.c.outbox_id == message_id).with_for_update()).mappings().one_or_none()
            if row and row["state"] == "DELIVERED" and row["generation"] == generation and row["lease_owner"] == worker_id:
                return
            if not row or row["state"] != "DELIVERING" or row["generation"] != generation or row["lease_owner"] != worker_id or row["lease_expiry"] <= now:
                raise _error("LEASE_EXPIRED", "Outbox delivery qualification is stale")
            conn.execute(t.update().where(t.c.outbox_id == message_id).values(state="DELIVERED", delivered_at=now, lease_expiry=None))
