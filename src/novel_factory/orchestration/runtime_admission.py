"""Transient executor capacity. NF call attempts remain the sole spending ledger."""
import json

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
