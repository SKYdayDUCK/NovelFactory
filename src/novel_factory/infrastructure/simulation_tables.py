"""Immutable interaction candidates. World status lives in versioned revisions."""
from sqlalchemy import (
    Column,
    DateTime,
    ForeignKeyConstraint,
    Integer,
    String,
    Table,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID


def define_simulation_tables(metadata):
    uid = lambda name, **kw: Column(name, UUID(as_uuid=False), **kw)
    js = lambda name, **kw: Column(name, JSONB, **kw)
    stamp = lambda: Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now())
    interactions = Table(
        "nf_simulation_interactions", metadata,
        uid("interaction_id", primary_key=True), uid("work_id", nullable=False),
        uid("branch_id", nullable=False), uid("base_revision_id", nullable=False),
        Column("closure_hash", String(64), nullable=False),
        js("artifact_ref", nullable=False), js("payload", nullable=False),
        uid("created_by", nullable=False), stamp(),
        ForeignKeyConstraint(["work_id", "branch_id", "base_revision_id"],
                             ["nf_revisions.work_id", "nf_revisions.branch_id", "nf_revisions.world_revision_id"]),
        UniqueConstraint("work_id", "branch_id", "base_revision_id", "closure_hash"),
    )
    intents = Table(
        "nf_simulation_intents", metadata,
        uid("interaction_id", primary_key=True), uid("actor_id", primary_key=True),
        uid("task_id", nullable=False), Column("generation", Integer, nullable=False),
        uid("grant_id", nullable=False), uid("principal_id", nullable=False),
        js("artifact_ref", nullable=False), js("payload", nullable=False),
        Column("payload_hash", String(64), nullable=False), stamp(),
        ForeignKeyConstraint(["interaction_id"], ["nf_simulation_interactions.interaction_id"]),
        ForeignKeyConstraint(["grant_id"], ["nf_grants.grant_id"]),
        ForeignKeyConstraint(["task_id", "generation"], ["nf_task_inputs.task_id", "nf_task_inputs.generation"]),
    )
    outcomes = Table(
        "nf_simulation_outcomes", metadata,
        uid("interaction_id", primary_key=True), js("artifact_ref", nullable=False),
        js("payload", nullable=False), Column("request_hash", String(64), nullable=False),
        uid("selected_by", nullable=False), stamp(),
        ForeignKeyConstraint(["interaction_id"], ["nf_simulation_interactions.interaction_id"]),
    )
    return {"simulation_interactions": interactions, "simulation_intents": intents,
            "simulation_outcomes": outcomes}
