"""Durable wait specifications for existing nf_tasks, scoped to one generation."""
from sqlalchemy import (BigInteger, CheckConstraint, Column, DateTime, ForeignKey, ForeignKeyConstraint,
                        Integer, MetaData, String, Table, text)
from sqlalchemy.dialects.postgresql import UUID


def define_workflow_tables(metadata: MetaData) -> dict[str, Table]:
    uid = UUID(as_uuid=False)
    waits = Table(
        "nf_workflow_waits", metadata,
        Column("task_id", uid, primary_key=True),
        Column("generation", Integer, primary_key=True),
        Column("mode", String(3), nullable=False),
        Column("grant_epoch", BigInteger, nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("clock_timestamp()")),
        Column("woken_at", DateTime(timezone=True)),
        ForeignKeyConstraint(["task_id", "generation"], ["nf_task_inputs.task_id", "nf_task_inputs.generation"]),
        CheckConstraint("mode IN ('AND','OR')", name="ck_workflow_wait_mode"),
    )
    edges = Table(
        "nf_workflow_edges", metadata,
        Column("task_id", uid, primary_key=True),
        Column("generation", Integer, primary_key=True),
        Column("predecessor_id", uid, ForeignKey("nf_tasks.task_id"), primary_key=True),
        ForeignKeyConstraint(["task_id", "generation"], ["nf_workflow_waits.task_id", "nf_workflow_waits.generation"]),
        CheckConstraint("task_id <> predecessor_id", name="ck_workflow_edge_distinct"),
    )
    limits = Table(
        "nf_dispatch_limits", metadata,
        Column("scope", String, primary_key=True), Column("subject", String, primary_key=True),
        Column("slots", Integer, nullable=False),
        CheckConstraint("slots >= 0", name="dispatch_slots_nonnegative"),
        CheckConstraint("scope IN ('GLOBAL','WORK','ROLE')", name="dispatch_scope"),
        CheckConstraint("subject <> '' AND (scope <> 'GLOBAL' OR subject = '*')", name="dispatch_subject"),
    )
    waiters = Table(
        "nf_dispatch_waiters", metadata,
        Column("branch_id", uid, ForeignKey("nf_branches.branch_id"), primary_key=True),
        Column("requested_at", DateTime(timezone=True), nullable=False),
        Column("valid_until", DateTime(timezone=True), nullable=False),
    )
    slots = Table(
        "nf_dispatch_slots", metadata,
        Column("attempt_id", uid, ForeignKey("nf_task_executions.attempt_id"), primary_key=True),
        Column("task_id", uid, ForeignKey("nf_supervisor_jobs.task_id"), nullable=False),
        Column("expires_at", DateTime(timezone=True), nullable=False),
    )
    return {"workflow_waits": waits, "workflow_edges": edges, "dispatch_limits": limits,
            "dispatch_waiters": waiters, "dispatch_slots": slots}
