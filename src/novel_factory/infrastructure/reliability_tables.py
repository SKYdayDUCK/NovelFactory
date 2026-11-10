"""PostgreSQL task, budget and reliable-delivery tables in shared metadata."""
from sqlalchemy import (
    BigInteger, CheckConstraint, Column, DateTime, ForeignKey, ForeignKeyConstraint, Index, Integer,
    MetaData, Numeric, String, Table, Text, UniqueConstraint, text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID


def define_reliability_tables(metadata: MetaData) -> dict[str, Table]:
    uid = lambda: UUID(as_uuid=False)
    now = text("clock_timestamp()")
    tasks = Table(
        "nf_tasks", metadata,
        Column("task_id", uid(), primary_key=True),
        Column("work_id", uid(), nullable=False),
        Column("branch_id", uid(), nullable=False),
        Column("principal_id", Text, nullable=False),
        Column("grant_id", uid(), nullable=False),
        Column("task_kind", Text, nullable=False),
        Column("role_id", Text, nullable=False),
        Column("state", String(24), nullable=False),
        Column("generation", Integer, nullable=False, server_default="0"),
        Column("context_bundle_id", uid(), nullable=True),
        Column("dependency_fingerprint", String(64), nullable=True),
        Column("lease_owner", Text),
        Column("lease_expiry", DateTime(timezone=True)),
        Column("deadline", DateTime(timezone=True), nullable=False),
        Column("current_result_id", uid()),
        Column("attempt_id", uid()),
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=now),
        Column("updated_at", DateTime(timezone=True), nullable=False, server_default=now),
        ForeignKeyConstraint(["work_id", "branch_id"], ["nf_branches.work_id", "nf_branches.branch_id"]),
        ForeignKeyConstraint(["grant_id"], ["nf_grants.grant_id"]),
        CheckConstraint("generation >= 0", name="ck_task_generation"),
        CheckConstraint("state IN ('BLOCKED','READY','RUNNING','RESULT_STORED','VALIDATING','WAITING_REVIEW','ACCEPTED','STALE','PAUSED','CANCELLED','FAILED')", name="ck_task_state"),
        CheckConstraint("state != 'RUNNING' OR (lease_owner IS NOT NULL AND lease_expiry IS NOT NULL)", name="ck_running_lease"),
        CheckConstraint("state NOT IN ('RESULT_STORED','VALIDATING','WAITING_REVIEW','ACCEPTED') OR (context_bundle_id IS NOT NULL AND dependency_fingerprint IS NOT NULL)", name="ck_task_bound_context"),
    )
    Index("ix_tasks_ready", tasks.c.state, tasks.c.work_id, tasks.c.task_kind, tasks.c.created_at)
    inputs = Table(
        "nf_task_inputs", metadata,
        Column("task_id", uid(), ForeignKey("nf_tasks.task_id"), primary_key=True),
        Column("generation", Integer, primary_key=True),
        Column("spec", JSONB, nullable=False),
        Column("input_hash", String(64), nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=now),
    )
    results = Table(
        "nf_task_results", metadata,
        Column("result_id", uid(), primary_key=True),
        Column("task_id", uid(), ForeignKey("nf_tasks.task_id"), nullable=False),
        Column("generation", Integer, nullable=False),
        Column("attempt_id", uid(), nullable=False),
        Column("payload_hash", String(64), ForeignKey("nf_objects.hash"), nullable=False),
        Column("payload", JSONB, nullable=False),
        Column("disposition", String(24), nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=now),
        CheckConstraint("disposition IN ('CURRENT','LATE','REJECTED')", name="ck_result_disposition"),
    )
    executions = Table(
        "nf_task_executions", metadata,
        Column("attempt_id", uid(), primary_key=True),
        Column("task_id", uid(), ForeignKey("nf_tasks.task_id"), nullable=False),
        Column("generation", Integer, nullable=False),
        Column("worker_id", Text, nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=now),
        UniqueConstraint("task_id", "generation", name="uq_execution_generation"),
    )
    commands = Table(
        "nf_task_commands", metadata,
        Column("principal_id", Text, primary_key=True),
        Column("idempotency_key", Text, primary_key=True),
        Column("command_hash", String(64), nullable=False),
        Column("receipt", JSONB, nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=now),
    )
    fairness = Table(
        "nf_task_fairness", metadata,
        Column("work_id", uid(), primary_key=True),
        Column("task_kind", Text, primary_key=True),
        Column("last_served_at", DateTime(timezone=True), nullable=False, server_default=text("'1970-01-01T00:00:00Z'")),
        Column("claims", BigInteger, nullable=False, server_default="0"),
    )
    budgets = Table(
        "nf_budgets", metadata,
        Column("budget_id", uid(), primary_key=True),
        Column("work_id", uid(), nullable=False),
        Column("currency", String(3), nullable=False),
        Column("limit_amount", Numeric(20, 6), nullable=False),
        Column("reserved_amount", Numeric(20, 6), nullable=False, server_default="0"),
        Column("spent_amount", Numeric(20, 6), nullable=False, server_default="0"),
        CheckConstraint("limit_amount >= 0 AND reserved_amount >= 0 AND spent_amount >= 0 AND reserved_amount + spent_amount <= limit_amount", name="ck_budget_balance"),
        CheckConstraint("currency IN ('USD','CNY')", name="ck_budget_currency"),
    )
    calls = Table(
        "nf_call_attempts", metadata,
        Column("attempt_id", uid(), primary_key=True),
        Column("task_id", uid(), ForeignKey("nf_tasks.task_id"), nullable=False),
        Column("generation", Integer, nullable=False),
        Column("budget_id", uid(), ForeignKey("nf_budgets.budget_id"), nullable=False),
        Column("call_key", Text, nullable=False),
        Column("request_hash", String(64), nullable=False),
        Column("state", String(16), nullable=False),
        Column("reserved_amount", Numeric(20, 6), nullable=False),
        Column("actual_amount", Numeric(20, 6)),
        Column("reserved_tokens", BigInteger, nullable=False, server_default="0"),
        Column("actual_tokens", BigInteger),
        Column("provider_receipt", Text),
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=now),
        Column("updated_at", DateTime(timezone=True), nullable=False, server_default=now),
        UniqueConstraint("task_id", "generation", "call_key", name="uq_call_identity"),
        CheckConstraint("state IN ('PREPARED','SENT','SUCCEEDED','FAILED','UNKNOWN')", name="ck_call_state"),
        CheckConstraint("reserved_amount >= 0 AND (actual_amount IS NULL OR actual_amount >= 0)", name="ck_call_amount"),
        CheckConstraint("reserved_tokens >= 0 AND (actual_tokens IS NULL OR actual_tokens >= 0)", name="ck_call_tokens"),
    )
    outbox = Table(
        "nf_outbox", metadata,
        Column("outbox_id", uid(), primary_key=True),
        Column("topic", Text, nullable=False),
        Column("aggregate_type", Text, nullable=False),
        Column("aggregate_id", Text, nullable=False),
        Column("payload", JSONB, nullable=False),
        Column("dedupe_key", Text, nullable=False, unique=True),
        Column("state", String(16), nullable=False, server_default="PENDING"),
        Column("generation", Integer, nullable=False, server_default="0"),
        Column("lease_owner", Text),
        Column("lease_expiry", DateTime(timezone=True)),
        Column("attempts", Integer, nullable=False, server_default="0"),
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=now),
        Column("delivered_at", DateTime(timezone=True)),
        Column("last_error", Text),
        CheckConstraint("state IN ('PENDING','DELIVERING','DELIVERED')", name="ck_outbox_state"),
    )
    Index("ix_outbox_pending", outbox.c.state, outbox.c.created_at)
    inbox = Table(
        "nf_inbox", metadata,
        Column("consumer", Text, primary_key=True),
        Column("message_id", uid(), primary_key=True),
        Column("processed_at", DateTime(timezone=True), nullable=False, server_default=now),
    )
    return {"tasks": tasks, "task_inputs": inputs, "task_results": results,
            "task_executions": executions, "task_commands": commands, "task_fairness": fairness,
            "budgets": budgets, "call_attempts": calls, "outbox": outbox, "inbox": inbox}
