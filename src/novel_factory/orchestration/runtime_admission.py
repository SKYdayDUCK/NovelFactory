"""Transient executor capacity. NF call attempts remain the sole spending ledger."""
import json
from uuid import UUID

from sqlalchemy import text

from novel_factory.application.kernel import require


# Separate from the task queue gate. All senders take this lock *after* their own
# task lock; the enqueue selector only observes permits and never takes this lock.
SEND_GATE = 5800231


def scope_for(adapter, payload, work):
    provider = getattr(adapter, "provider_key", None)
    decision = payload.get("routing_decision")
    if not provider and not decision:
        return None
    scope = {"work": str(work), "role": payload["role_id"]}
    if provider:
        scope.update(provider=provider, account=adapter.account_key)
    if decision:
        scope["organization"] = decision["organization"]
    return scope


def take(conn, scope, task, attempt, tokens, timeout_seconds):
    conn.execute(text("SELECT pg_advisory_xact_lock(:gate)"), {"gate": SEND_GATE})
    conn.execute(text("LOCK TABLE nf_runtime_limits IN SHARE MODE"))
    if conn.scalar(text("SELECT EXISTS(SELECT FROM nf_runtime_permits WHERE attempt_id=:attempt)"),
                   {"attempt": attempt}):
        return
    effective = conn.scalar(text("""
        SELECT nf_runtime_scope(CAST(:scope AS jsonb),work_id,branch_id) FROM nf_tasks WHERE task_id=:task
    """), {"scope": json.dumps(scope), "task": task})
    params = {"scope": json.dumps(effective), "task": task, "attempt": attempt, "tokens": tokens,
              "timeout": float(timeout_seconds) + 10}
    ok = conn.scalar(text("SELECT nf_runtime_available(CAST(:scope AS jsonb),:tokens,false)"), params)
    require(ok, "RUNTIME_CAPACITY_LIMIT", "Model runtime quota has no available permit")
    conn.execute(text("""
        INSERT INTO nf_runtime_permits (scope,task_id,attempt_id,tokens,expires_at)
        SELECT CAST(:scope AS jsonb),task_id,:attempt,:tokens,
          greatest(lease_expiry, clock_timestamp()+:timeout * interval '1 second')
        FROM nf_tasks WHERE task_id=:task
    """), params)


def release(db, attempt):
    with db.uow() as repo:
        repo.conn.execute(text("UPDATE nf_runtime_permits SET released=true WHERE attempt_id=:attempt"),
                          {"attempt": attempt})
        repo.conn.execute(text("SELECT pg_notify('nf_supervisor_wake','')"))


def delay_repeated_denial(conn, task, attempt):
    """Called with the task locked, only after the current PREPARED call was refunded."""
    recent = conn.scalar(text("""
        SELECT count(*) FROM (
            SELECT 1 FROM nf_call_attempts c JOIN nf_runtime_unsent u USING(attempt_id)
            WHERE c.task_id=:task AND c.state='FAILED' AND c.actual_amount=0 AND c.actual_tokens=0
              AND u.recorded_at >= statement_timestamp() - interval '2 minutes'
            LIMIT 6
        ) refunded
    """), {"task": task})
    if recent < 2:
        return
    # Stable per-attempt jitter spreads competing projects across a reopening quota.
    jitter = (UUID(str(attempt)).int % 250) / 1000
    seconds = min(30, 2 ** (recent - 1) + jitter)
    conn.execute(text("""
        INSERT INTO nf_quota_retry_deadlines (task_id,generation,attempt_id,retry_not_before)
        SELECT task_id,generation,:attempt,least(deadline, clock_timestamp() + :seconds * interval '1 second')
        FROM nf_tasks WHERE task_id=:task AND state='READY'
        ON CONFLICT (task_id) DO UPDATE SET generation=excluded.generation, attempt_id=excluded.attempt_id,
                                          retry_not_before=excluded.retry_not_before
    """), {"task": task, "attempt": attempt, "seconds": seconds})
