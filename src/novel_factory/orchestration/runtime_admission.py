"""Transient executor capacity. NF call attempts remain the sole spending ledger."""
import json
from decimal import Decimal
from uuid import UUID, uuid5

from sqlalchemy import text

from novel_factory.application.kernel import require
from novel_factory.domain.canonical import fingerprint

# Separate from the task queue gate. All senders take this lock *after* their own
# task lock; the enqueue selector only observes permits and never takes this lock.
SEND_GATE = 5800231


def refund_definite_unsent(kernel, binding, audit_ref):
    """Refund one trusted HTTP pre-send rejection, never an UNKNOWN/returned call.

    The immutable audit is written first, so a crash can replay this transaction.
    Two fresh generations are permitted; a third failure retains a finite hold.
    """
    with kernel.db.uow() as repo:
        task = repo.one("tasks", {"task_id": binding["task"]["task_id"]}, lock="update")
        audit = kernel.artifact(repo, audit_ref, task["work_id"], kind="AGENT_CALL_EVIDENCE")["payload"]
        attempt = audit["attempt_id"]
        call = repo.one("call_attempts", {"attempt_id": attempt})
        require(call and audit_ref["artifact_revision_id"] == str(uuid5(UUID(attempt), "audit:revision"))
                and audit["task_id"] == call["task_id"] == task["task_id"]
                and audit["generation"] == call["generation"]
                and audit["reason"] == "MODEL_TLS_CONFIGURATION_INVALID"
                and audit["outcome"] == "REJECTED" and audit["accounting_state"] == "FAILED"
                and audit["response_ref"] is audit["output_ref"] is audit["provider_receipt"] is None
                and audit["usage"] == {"input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0,
                    "actual_amount": "0", "currency": binding["spec"]["budget"]["currency"], "cost_status": "NOT_SENT"}
                and audit["model_configuration"] == binding["config"]
                and audit["model_configuration"].get("adapter_kind") in {"deepseek", "openai-compatible", "OPENAI_COMPATIBLE_HTTP"},
                "INVALID_EVIDENCE", "Only an exact trusted HTTP pre-send rejection may release a reservation")
        prompt = kernel.artifact(repo, audit["prompt_ref"], task["work_id"], kind="AGENT_PROMPT")["payload"]
        require(audit["request_sha256"] == call["request_hash"] == fingerprint(prompt)
                and prompt["input_ref"] == audit["input_ref"] and prompt["model_configuration"] == binding["config"]
                and prompt["budget_id"] == call["budget_id"] and Decimal(prompt["reserve_amount"]) == call["reserved_amount"]
                and prompt["token_reservation"] == call["reserved_tokens"]
                and not repo.one("host_turns", {"attempt_id": attempt}),
                "INVALID_EVIDENCE", "Unsent evidence differs from its frozen request or contains a Host execution")
        prior = repo.conn.scalar(text("SELECT EXISTS(SELECT FROM nf_runtime_unsent WHERE attempt_id=:id AND generation=:gen)"),
                                 {"id": attempt, "gen": call["generation"]})
        if prior:
            require(call["state"] == "FAILED" and call["actual_amount"] == 0 and call["actual_tokens"] == 0
                    and call["provider_receipt"] is None, "INVALID_EVIDENCE", "Unsent refund marker and ledger differ")
            return {"task_id": task["task_id"], "generation": task["generation"], "state": task["state"], "refunded_now": False}
        require(call["state"] == "SENT" and call["provider_receipt"] is call["actual_amount"] is call["actual_tokens"] is None,
                "CALL_RECONCILIATION_REQUIRED", "An UNKNOWN, returned or settled call cannot be downgraded to unsent")
        kernel.tasks._settle_locked_task_call(repo.conn, call, "FAILED", Decimal(0), 0, None)
        repo.conn.execute(text("INSERT INTO nf_runtime_unsent(attempt_id,generation) VALUES (:id,:gen)"),
                          {"id": attempt, "gen": call["generation"]})
        repo.conn.execute(text("UPDATE nf_runtime_permits SET released=true WHERE attempt_id=:id"), {"id": attempt})
        count = repo.conn.scalar(text("""
            SELECT count(*) FROM nf_runtime_unsent u JOIN nf_call_attempts c USING(attempt_id)
            WHERE c.task_id=:task AND EXISTS(SELECT FROM nf_artifacts a WHERE a.work_id=:work
                AND a.kind='AGENT_CALL_EVIDENCE' AND a.payload->>'attempt_id'=c.attempt_id::text
                AND a.payload->>'reason'='MODEL_TLS_CONFIGURATION_INVALID'
                AND a.payload->'usage'->>'cost_status'='NOT_SENT')
        """), {"task": task["task_id"], "work": task["work_id"]})
        state = {"task_id": task["task_id"], "generation": task["generation"], "state": task["state"]}
        if task["state"] == "RUNNING" and task["generation"] == call["generation"]:
            if count <= 2:
                state = kernel.tasks._recover_task(repo.conn, task, reason="MODEL_TLS_CONFIGURATION_INVALID")
            else:
                repo.change("tasks", {"task_id": task["task_id"]}, {"state": "PAUSED", "lease_owner": None, "lease_expiry": None,
                                                                  "updated_at": repo.now()})
                kernel.tasks._emit(repo.conn, task["task_id"], task["generation"], "PAUSED")
                state.update(state="PAUSED", reason="UNSENT_RETRY_EXHAUSTED")
        repo.conn.execute(text("SELECT pg_notify('nf_supervisor_wake','')"))
        return {**state, "refunded_now": True}


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
