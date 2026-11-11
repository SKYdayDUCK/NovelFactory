"""Finite, event-driven task barriers on the kernel's PostgreSQL task queue.

All entry points are internal. The caller binds work/branch to its server-side
identity and supplies an authorization check that locks the current grant in
the same transaction. A dependency is satisfied by the current predecessor's
durable task acceptance. No model or external provider is called here.
"""
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert

from novel_factory.domain.errors import DomainError


def _guard(condition, code, message):
    if not condition:
        raise DomainError(code, message)


# The optional grant expiry/policy columns were added after workflow schema
# 0004. An absent column on an older database has the legacy NULL meaning.
_CURRENT_GRANT = """
    g.active AND g.work_id = t.work_id AND g.principal_id::text = t.principal_id AND g.role_id = t.role_id
      AND (g.branch_ids = '["*"]'::jsonb OR g.branch_ids @> jsonb_build_array(t.branch_id))
      AND work.policy_version = i.spec->>'policy_version'
      AND coalesce((to_jsonb(g)->>'expires_at')::timestamptz, 'infinity') > clock_timestamp()
      AND coalesce(to_jsonb(g)->>'bound_policy_version', work.policy_version) = work.policy_version
      AND i.spec->>'capability_grant_id' = t.grant_id::text AND i.spec->>'role_id' = t.role_id
      AND i.spec->'world_ref'->>'work_id' = t.work_id::text
      AND i.spec->'world_ref'->>'branch_id' = t.branch_id::text
"""

_TASK_GRANT = text("""
    SELECT g.epoch FROM nf_tasks t
    JOIN nf_task_inputs i USING (task_id, generation)
    JOIN nf_works work ON work.work_id = t.work_id
    JOIN nf_grants g ON g.grant_id = t.grant_id
    WHERE t.task_id = :task AND """ + _CURRENT_GRANT + """
    FOR SHARE OF g, work
""")


# Match the exact producer event, including its current generation. Reading
# delivered rows is intentional: another consumer can ACK before we register.
_WAKEABLE = text("""
    SELECT t.task_id, t.generation, evidence.message_ids
    FROM nf_tasks t
    JOIN nf_workflow_waits w USING (task_id, generation)
    JOIN nf_task_inputs i USING (task_id, generation)
    JOIN nf_works work ON work.work_id = t.work_id
    JOIN nf_grants g ON g.grant_id = t.grant_id
    CROSS JOIN LATERAL (
        SELECT count(*) AS required, count(o.outbox_id) AS satisfied,
               array_agg(o.outbox_id) FILTER (WHERE o.outbox_id IS NOT NULL) AS message_ids
        FROM nf_workflow_edges e
        JOIN nf_tasks p ON p.task_id = e.predecessor_id
        LEFT JOIN nf_outbox o ON p.state = 'ACCEPTED'
          AND p.work_id = t.work_id AND p.branch_id = t.branch_id
          AND o.dedupe_key = concat_ws(':', 'task', p.task_id, p.generation, 'ACCEPTED')
          AND o.topic = 'task.changed' AND o.aggregate_type = 'task' AND o.aggregate_id = p.task_id::text
          AND o.payload = jsonb_build_object('task_id',p.task_id,'generation',p.generation,'state','ACCEPTED')
        WHERE (e.task_id,e.generation) = (t.task_id,t.generation)
    ) evidence
    WHERE t.work_id = :work AND t.branch_id = :branch AND t.state = 'BLOCKED'
      AND w.woken_at IS NULL AND t.deadline > clock_timestamp()
      AND g.epoch = w.grant_epoch AND """ + _CURRENT_GRANT + """
      AND NOT EXISTS (SELECT 1 FROM nf_call_attempts c WHERE c.task_id=t.task_id
                      AND c.state IN ('PREPARED','SENT','UNKNOWN'))
      AND evidence.satisfied > 0 AND (w.mode = 'OR' OR evidence.satisfied = evidence.required)
    ORDER BY w.created_at, t.task_id
    LIMIT :limit FOR UPDATE OF t SKIP LOCKED FOR SHARE OF g, work SKIP LOCKED
""")


class WorkflowService:
    def __init__(self, tasks):
        self.tasks = tasks
        self.tables = tasks.tables

    @staticmethod
    def _authorize(check):
        _guard(callable(check), "VIEW_DENIED", "Workflow operation requires a bound authorization check")
        return check

    def _counts(self, conn, work_id, branch_id):
        t = self.tables["tasks"]
        scope = (t.c.work_id == work_id, t.c.branch_id == branch_id)
        counts = {"READY": 0, "WAITING": 0, "RUNNING": 0}
        for state, count in conn.execute(select(t.c.state, func.count()).where(*scope).group_by(t.c.state)):
            counts["WAITING" if state == "BLOCKED" else state] = count
        return {"counts": counts, "ready_count": counts["READY"]}

    def status(self, work_id, branch_id, *, authorize_in_tx):
        with self.tasks._tx(self._authorize(authorize_in_tx)) as conn:
            return self._counts(conn, work_id, branch_id)

    def wait(self, task_id, generation, dependencies, *, work_id, branch_id, mode,
             authorize_in_tx, worker_id=None):
        """Install one immutable AND/OR group for a generation; stop the lease.

        Create tasks with prerequisites BLOCKED to avoid exposing a READY task
        before installing its edges. A RUNNING task may suspend only its own
        live lease. Recovery evaluates sources that completed before this call.
        """
        _guard(type(generation) is int and generation >= 0 and mode in ("AND", "OR")
               and isinstance(dependencies, (tuple, list)) and 0 < len(dependencies) <= 128,
               "INVALID_ARGUMENT", "Invalid generation, wait mode or prerequisite count")
        try:
            target = str(UUID(str(task_id)))
            preds = sorted({str(UUID(str(value))) for value in dependencies})
        except (TypeError, ValueError) as exc:
            raise DomainError("INVALID_ARGUMENT", "Expected task UUIDs") from exc
        _guard(len(preds) == len(dependencies), "INVALID_ARGUMENT", "Repeated workflow prerequisite")
        w, e, t = (self.tables[k] for k in ("workflow_waits", "workflow_edges", "tasks"))
        with self.tasks._tx(self._authorize(authorize_in_tx)) as conn:
            # Serialize graph edits in *this branch*. Concurrent opposing inserts
            # otherwise pass both reachability tests under READ COMMITTED.
            conn.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:scope,0))"),
                         {"scope": f"nf-workflow:{UUID(str(branch_id))}"})
            task = self.tasks._task(conn, target)
            _guard((task["work_id"], task["branch_id"]) == (str(work_id), str(branch_id)),
                   "VIEW_DENIED", "Workflow task unavailable")
            _guard(task["generation"] == generation, "STALE_GENERATION", "Workflow generation changed")
            epoch = conn.scalar(_TASK_GRANT, {"task": target})
            _guard(epoch is not None, "VIEW_DENIED", "Workflow task grant unavailable")
            group_key = (w.c.task_id == target, w.c.generation == generation)
            prior = conn.execute(select(w.c.mode, w.c.grant_epoch).where(*group_key)).one_or_none()
            if prior is not None:
                _guard(prior.grant_epoch == epoch, "VIEW_DENIED", "Workflow grant changed")
                previous = list(conn.scalars(select(e.c.predecessor_id).where(
                    e.c.task_id == target, e.c.generation == generation).order_by(e.c.predecessor_id)))
                _guard(prior.mode == mode and previous == preds, "IDEMPOTENCY_CONFLICT", "Wait specification changed")
                return {"task_id": target, "generation": generation, "state": task["state"]}
            if task["state"] == "RUNNING":
                _guard(bool(worker_id), "LEASE_OWNER_MISMATCH", "A live worker identity is required")
                self.tasks.guard_task(conn, target, generation, worker_id, require_context=False)
            else:
                _guard(task["state"] == "BLOCKED", "INVALID_TASK_TRANSITION", "Task cannot install a wait")
            _guard(task["deadline"] > self.tasks._now(conn), "DEADLINE_EXCEEDED", "Task deadline has passed")
            calls = self.tables["call_attempts"]
            unresolved = conn.scalar(select(calls.c.attempt_id).where(calls.c.task_id == target,
                calls.c.state.in_(("PREPARED", "SENT", "UNKNOWN"))).limit(1))
            _guard(not unresolved, "CALL_RECONCILIATION_REQUIRED", "Reconcile external calls before suspension")
            found = conn.scalar(select(func.count()).select_from(t).where(t.c.task_id.in_(preds),
                                t.c.work_id == work_id, t.c.branch_id == branch_id))
            _guard(found == len(preds), "VIEW_DENIED", "Workflow prerequisite unavailable")
            cycle = conn.scalar(text("""
                WITH RECURSIVE ancestors(id) AS (
                    SELECT unnest(CAST(:preds AS uuid[]))
                    UNION
                    SELECT e.predecessor_id FROM ancestors a
                    JOIN nf_tasks p ON p.task_id = a.id AND p.state = 'BLOCKED'
                    JOIN nf_workflow_waits w USING (task_id, generation)
                    JOIN nf_workflow_edges e USING (task_id, generation)
                    WHERE w.woken_at IS NULL
                ) SELECT EXISTS (SELECT 1 FROM ancestors WHERE id = :target)
            """), {"preds": preds, "target": target})
            _guard(not cycle, "WORKFLOW_CYCLE", "Workflow prerequisites must form a DAG")
            conn.execute(w.insert().values(task_id=target, generation=generation, mode=mode, grant_epoch=epoch))
            conn.execute(e.insert(), [dict(task_id=target, generation=generation, predecessor_id=p) for p in preds])
            conn.execute(t.update().where(t.c.task_id == target).values(
                state="BLOCKED", lease_owner=None, lease_expiry=None, attempt_id=None,
                updated_at=self.tasks._now(conn)))
            self.tasks._emit(conn, target, generation, "BLOCKED")
            return {"task_id": target, "generation": generation, "state": "BLOCKED"}

    def resume_project(self, work_id, branch_id, *, authorize_in_tx, limit=100):
        """Bounded recovery transaction; repeated invocations can consume new events."""
        _guard(type(limit) is int and 1 <= limit <= 1000, "INVALID_ARGUMENT", "Invalid recovery batch size")
        with self.tasks._tx(self._authorize(authorize_in_tx)) as conn:
            t, w, inbox = (self.tables[k] for k in ("tasks", "workflow_waits", "inbox"))
            expired = conn.execute(select(t).where(
                t.c.work_id == work_id, t.c.branch_id == branch_id,
                t.c.state == "RUNNING", t.c.lease_expiry <= self.tasks._now(conn)
            ).order_by(t.c.task_id).limit(limit).with_for_update(skip_locked=True)).mappings().all()
            for task in expired:
                self.tasks._recover_task(conn, dict(task))
            rows = conn.execute(_WAKEABLE, {"work": work_id, "branch": branch_id, "limit": limit}).mappings().all()
            for row in rows:
                target, generation = str(row["task_id"]), row["generation"]
                consumer = f"workflow:{target}:{generation}"
                conn.execute(insert(inbox).on_conflict_do_nothing(), [
                    {"consumer": consumer, "message_id": str(msg)} for msg in row["message_ids"]])
                conn.execute(w.update().where(w.c.task_id == target, w.c.generation == generation)
                             .values(woken_at=func.clock_timestamp()))
                conn.execute(t.update().where(t.c.task_id == target, t.c.generation == generation)
                             .values(state="READY", updated_at=func.clock_timestamp()))
                self.tasks._emit(conn, target, generation, "READY")
            return {**self._counts(conn, work_id, branch_id), "awakened": len(rows), "recovered": len(expired)}
