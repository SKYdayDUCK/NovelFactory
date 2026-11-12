"""NF-owned installation bindings, finite work allowances and immutable receipts."""

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    String,
    Table,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID


def define_delegation_tables(metadata):
    uid = UUID(as_uuid=False)
    installs = Table(
        "nf_delegation_installations",
        metadata,
        Column("installation_id", uid, primary_key=True),
        Column("nf_instance_id", uid, nullable=False),
        Column("org_id", uid, nullable=False),
        Column("work_id", uid, nullable=False),
        Column("branch_id", uid, nullable=False),
        Column("secret_hash", String(64), nullable=False, unique=True),
        Column("policy_version", String, nullable=False),
        Column("epoch", BigInteger, nullable=False, server_default="1"),
        Column("active", Boolean, nullable=False, server_default="true"),
        Column("outbound_fenced", Boolean, nullable=False, server_default="false"),
        Column("expires_at", DateTime(timezone=True), nullable=False),
        ForeignKeyConstraint(
            ["work_id", "branch_id"], ["nf_branches.work_id", "nf_branches.branch_id"]
        ),
    )
    slots = Table(
        "nf_delegation_slots",
        metadata,
        Column("installation_id", uid, ForeignKey(installs.c.installation_id), primary_key=True),
        Column("scope_ref", String(128), primary_key=True),
        Column("task_template_id", String(128), nullable=False),
        Column("grant_id", uid, ForeignKey("nf_grants.grant_id"), nullable=False, unique=True),
        Column("grant_epoch", BigInteger, nullable=False),
        Column("principal_id", uid, nullable=False),
        Column("actor_binding_ref", uid),
        Column("spec", JSONB, nullable=False),
        Column("job_config", JSONB, nullable=False),
    )
    receipts = Table(
        "nf_delegation_receipts",
        metadata,
        Column("installation_id", uid, primary_key=True),
        Column("operation_id", uid, primary_key=True),
        Column("scope_ref", String(128), nullable=False),
        Column("budget_reservation_ref", uid, nullable=False),
        Column("task_id", uid, ForeignKey("nf_tasks.task_id"), nullable=False, unique=True),
        Column("receipt_ref", uid, nullable=False, unique=True),
        Column("request_hash", String(64), nullable=False),
        Column(
            "created_at",
            DateTime(timezone=True),
            nullable=False,
            server_default=text("clock_timestamp()"),
        ),
        ForeignKeyConstraint(
            ["installation_id", "scope_ref"], [slots.c.installation_id, slots.c.scope_ref]
        ),
        UniqueConstraint("installation_id", "scope_ref"),
        UniqueConstraint("installation_id", "budget_reservation_ref"),
    )
    return {
        "delegation_installations": installs,
        "delegation_slots": slots,
        "delegation_receipts": receipts,
    }
