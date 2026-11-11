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
    return {"workflow_waits": waits, "workflow_edges": edges}
