"""PostgreSQL schema and short-lived Unit of Work.

There is no SQLite compatibility layer. Immutable records are also protected by
database triggers installed by migration 0002. All application writers take the
shared maintenance gate; backups take the exclusive gate without serializing
ordinary writers against one another.
"""
from __future__ import annotations

from contextlib import contextmanager
import os
from typing import Iterator

from sqlalchemy import (MetaData, Table, Column, Text, String, Integer, BigInteger,
                        Boolean, DateTime, ForeignKeyConstraint, UniqueConstraint,
                        CheckConstraint, Index, create_engine, text, func)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.engine import Connection

MAINTENANCE_LOCK = 721943001
metadata = MetaData(naming_convention={"ix": "ix_%(column_0_label)s", "uq": "uq_%(table_name)s_%(column_0_name)s",
                                     "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s", "pk": "pk_%(table_name)s"})
U = UUID(as_uuid=False)
def uid(name, **kw): return Column(name, U, **kw)
def js(name, **kw): return Column(name, JSONB, **kw)
def ts(name="created_at", **kw): return Column(name, DateTime(timezone=True), server_default=func.now(), nullable=False, **kw)

tables = {}
def define(key, *args):
    table = Table("nf_" + key, metadata, *args)
    tables[key] = table
    return table

define("objects", Column("hash", String(64), primary_key=True), Column("size", BigInteger, nullable=False), ts(),
       CheckConstraint("size >= 0", name="object_nonnegative"))
define("object_refs", Column("owner_type", Text, primary_key=True), Column("owner_id", Text, primary_key=True),
       Column("object_hash", String(64), primary_key=True),
       ForeignKeyConstraint(["object_hash"], ["nf_objects.hash"]))
define("works", uid("work_id", primary_key=True), Column("name", Text, nullable=False),
       uid("canon_branch_id"), Column("canon_epoch", BigInteger, nullable=False, server_default="0"),
       Column("policy_version", Text, nullable=False), js("policy_ref", nullable=False), js("rules_ref", nullable=False),
       js("interpretation_ref", nullable=False), Column("interpretation_epoch", BigInteger, nullable=False, server_default="0"),
       ts())
define("genesis", uid("work_id", primary_key=True), js("payload", nullable=False), Column("payload_hash", String(64), nullable=False),
       ForeignKeyConstraint(["work_id"], ["nf_works.work_id"]))
define("branches", uid("branch_id", primary_key=True), uid("work_id", nullable=False), Column("kind", Text, nullable=False),
       uid("head_revision_id"), js("fork_ref"), Column("history_epoch", BigInteger, nullable=False, server_default="0"),
       Column("repair_ready", Boolean, nullable=False, server_default="false"), ts(),
       ForeignKeyConstraint(["work_id"], ["nf_works.work_id"]), UniqueConstraint("work_id", "branch_id"),
       CheckConstraint("kind IN ('CANON','CANDIDATE','SIMULATION','REPAIR')", name="branch_kind"))
define("revisions", uid("world_revision_id", primary_key=True), uid("work_id", nullable=False), uid("branch_id", nullable=False),
       Column("branch_seq", BigInteger, nullable=False), uid("parent_revision_id"),
       js("selection", nullable=False), js("frontiers", nullable=False), js("scope_versions", nullable=False),
       js("snapshot", nullable=False), js("interpretation_ref", nullable=False),
       Column("manifest_hash", String(64), nullable=False), Column("state_hash", String(64), nullable=False),
       js("provenance", nullable=False), ts(),
       ForeignKeyConstraint(["work_id", "branch_id"], ["nf_branches.work_id", "nf_branches.branch_id"]),
       UniqueConstraint("branch_id", "branch_seq"), UniqueConstraint("work_id", "branch_id", "world_revision_id"),
       ForeignKeyConstraint(["work_id", "branch_id", "parent_revision_id"],
                            ["nf_revisions.work_id", "nf_revisions.branch_id", "nf_revisions.world_revision_id"], deferrable=True, initially="DEFERRED"),
       CheckConstraint("branch_seq >= 0", name="revision_seq"))
tables["branches"].append_constraint(ForeignKeyConstraint(
    ["work_id", "branch_id", "head_revision_id"], ["nf_revisions.work_id", "nf_revisions.branch_id", "nf_revisions.world_revision_id"],
    name="fk_branch_head_identity", deferrable=True, initially="DEFERRED", use_alter=True))
tables["works"].append_constraint(ForeignKeyConstraint(["work_id", "canon_branch_id"], ["nf_branches.work_id", "nf_branches.branch_id"],
    name="fk_work_canon_identity", deferrable=True, initially="DEFERRED", use_alter=True))
define("events", uid("event_version_id", primary_key=True), uid("work_id", nullable=False), uid("occurrence_id", nullable=False),
       uid("effect_group_id", nullable=False), uid("representation_id", nullable=False), js("payload", nullable=False),
       Column("payload_hash", String(64), nullable=False), Column("object_hash", String(64), nullable=False), ts(),
       ForeignKeyConstraint(["work_id"], ["nf_works.work_id"]), ForeignKeyConstraint(["object_hash"], ["nf_objects.hash"]),
       UniqueConstraint("work_id", "event_version_id", "occurrence_id", "effect_group_id"))
define("revision_events", uid("world_revision_id", primary_key=True), uid("occurrence_id", primary_key=True),
       uid("work_id", nullable=False), uid("branch_id", nullable=False), uid("event_version_id", nullable=False), uid("effect_group_id", nullable=False),
       ForeignKeyConstraint(["work_id", "branch_id", "world_revision_id"], ["nf_revisions.work_id", "nf_revisions.branch_id", "nf_revisions.world_revision_id"]),
       ForeignKeyConstraint(["work_id", "event_version_id", "occurrence_id", "effect_group_id"],
                            ["nf_events.work_id", "nf_events.event_version_id", "nf_events.occurrence_id", "nf_events.effect_group_id"]),
       UniqueConstraint("world_revision_id", "effect_group_id"))
define("commits", uid("world_revision_id", primary_key=True), js("operations", nullable=False), js("request", nullable=False),
       Column("request_hash", String(64), nullable=False),
       ForeignKeyConstraint(["world_revision_id"], ["nf_revisions.world_revision_id"]))
define("grants", uid("grant_id", primary_key=True), uid("principal_id", nullable=False), uid("work_id", nullable=False),
       Column("token_hash", String(64), nullable=False, unique=True), Column("role_id", Text, nullable=False), uid("actor_id"),
       js("branch_ids", nullable=False), js("capabilities", nullable=False), js("planes", nullable=False), js("max_story_time", nullable=False),
       Column("active", Boolean, nullable=False, server_default="true"), Column("epoch", BigInteger, nullable=False, server_default="0"),
       ForeignKeyConstraint(["work_id"], ["nf_works.work_id"]), ts())
define("artifacts", uid("artifact_revision_id", primary_key=True), uid("artifact_id", nullable=False), uid("work_id", nullable=False),
       Column("sha256", String(64), nullable=False), Column("kind", Text, nullable=False), js("payload"), uid("created_by", nullable=False), ts(),
       ForeignKeyConstraint(["sha256"], ["nf_objects.hash"]), ForeignKeyConstraint(["work_id"], ["nf_works.work_id"]),
       UniqueConstraint("work_id", "artifact_revision_id"))
define("contexts", uid("context_bundle_id", primary_key=True), uid("work_id", nullable=False), uid("task_id", nullable=False),
       Column("generation", BigInteger, nullable=False), uid("principal_id", nullable=False), uid("grant_id", nullable=False),
       js("payload", nullable=False), js("dependency_manifest", nullable=False), js("artifact_ref", nullable=False),
       js("read_cut", nullable=False), Column("dependency_fingerprint", String(64), nullable=False),
       ForeignKeyConstraint(["work_id"], ["nf_works.work_id"]), ForeignKeyConstraint(["grant_id"], ["nf_grants.grant_id"]),
       UniqueConstraint("task_id", "generation"), ts())
define("proposals", uid("proposal_id", primary_key=True), uid("work_id", nullable=False), uid("branch_id", nullable=False),
       uid("principal_id", nullable=False), uid("task_id", nullable=False), Column("generation", BigInteger, nullable=False),
       uid("context_bundle_id", nullable=False), js("artifact_ref", nullable=False), js("payload", nullable=False),
       Column("template_id", Text, nullable=False), js("template_args", nullable=False), js("footprint", nullable=False),
       js("closure", nullable=False),
       ForeignKeyConstraint(["context_bundle_id"], ["nf_contexts.context_bundle_id"]),
       ForeignKeyConstraint(["work_id", "branch_id"], ["nf_branches.work_id", "nf_branches.branch_id"]), ts())
define("approvals", uid("approval_id", primary_key=True), uid("work_id", nullable=False), uid("proposal_id", nullable=False),
       uid("grant_id", nullable=False), js("payload", nullable=False), Column("applicability", Text, nullable=False),
       Column("active", Boolean, nullable=False, server_default="true"), Column("epoch", BigInteger, nullable=False, server_default="0"),
       Column("expires_at", DateTime(timezone=True), nullable=False),
       ForeignKeyConstraint(["proposal_id"], ["nf_proposals.proposal_id"]), ForeignKeyConstraint(["grant_id"], ["nf_grants.grant_id"]))
define("certificates", uid("certificate_id", primary_key=True), uid("work_id", nullable=False), uid("proposal_id", nullable=False),
       js("artifact_ref", nullable=False), js("payload", nullable=False), js("validation_inputs", nullable=False),
       ForeignKeyConstraint(["proposal_id"], ["nf_proposals.proposal_id"]), ts())
define("scopes", uid("work_id", primary_key=True), uid("branch_id", primary_key=True), Column("scope_key", Text, primary_key=True),
       Column("version", BigInteger, nullable=False), Column("tombstone", Boolean, nullable=False, server_default="false"),
       ForeignKeyConstraint(["work_id", "branch_id"], ["nf_branches.work_id", "nf_branches.branch_id"]),
       CheckConstraint("version >= 0", name="scope_monotonic_nonnegative"))
define("receipts", uid("work_id", primary_key=True), uid("principal_id", primary_key=True), Column("command_type", Text, primary_key=True),
       Column("idempotency_key", Text, primary_key=True), Column("request_hash", String(64), nullable=False),
       uid("branch_id", nullable=False), uid("proposal_id"), js("payload", nullable=False), ts(),
       ForeignKeyConstraint(["work_id", "branch_id"], ["nf_branches.work_id", "nf_branches.branch_id"]),
       UniqueConstraint("branch_id", "proposal_id", "command_type"))
define("invalidations", uid("invalidation_id", primary_key=True), uid("work_id", nullable=False), uid("branch_id", nullable=False),
       uid("world_revision_id", nullable=False), Column("scope_key", Text, nullable=False), Column("version", BigInteger, nullable=False),
       ForeignKeyConstraint(["world_revision_id"], ["nf_revisions.world_revision_id"]), ts())
define("dependencies", uid("dependency_id", primary_key=True), uid("work_id", nullable=False), uid("branch_id", nullable=False),
       Column("owner_type", Text, nullable=False), Column("owner_id", Text, nullable=False), Column("scope_key", Text, nullable=False),
       Column("observed_version", BigInteger, nullable=False), Column("dependency_kind", Text, nullable=False), ts(),
       ForeignKeyConstraint(["work_id", "branch_id"], ["nf_branches.work_id", "nf_branches.branch_id"]))
Index("ix_dependencies_scope", tables["dependencies"].c.work_id, tables["dependencies"].c.branch_id, tables["dependencies"].c.scope_key)
define("selections", uid("work_id", primary_key=True), Column("selection_kind", Text, primary_key=True), js("artifact_ref", nullable=False),
       Column("epoch", BigInteger, nullable=False), ForeignKeyConstraint(["work_id"], ["nf_works.work_id"]))
define("index_watermarks", uid("branch_id", primary_key=True), Column("index_name", Text, primary_key=True),
       uid("world_revision_id", nullable=False), Column("branch_seq", BigInteger, nullable=False), Column("generation", BigInteger, nullable=False),
       ForeignKeyConstraint(["branch_id"], ["nf_branches.branch_id"]))
define("promotions", uid("promotion_id", primary_key=True), uid("work_id", nullable=False),
       js("old_world_ref", nullable=False), js("new_world_ref", nullable=False), js("report_ref", nullable=False), ts(),
       ForeignKeyConstraint(["work_id"], ["nf_works.work_id"]))

# Task tables use this exact metadata registry; no parallel authority/schema.
from novel_factory.infrastructure.reliability_tables import define_reliability_tables
tables.update(define_reliability_tables(metadata))
from novel_factory.infrastructure.workflow_tables import define_workflow_tables
tables.update(define_workflow_tables(metadata))
from novel_factory.infrastructure.simulation_tables import define_simulation_tables
tables.update(define_simulation_tables(metadata))

IMMUTABLE_TABLES = ("genesis", "revisions", "events", "revision_events", "commits", "artifacts", "contexts",
                    "proposals", "certificates", "receipts", "invalidations", "promotions",
                    "task_inputs", "task_executions", "task_results", "task_commands",
                    "simulation_interactions", "simulation_intents", "simulation_outcomes")

def mutation_gate(conn: Connection):
    conn.execute(text("SELECT pg_advisory_xact_lock_shared(:key)"), {"key": MAINTENANCE_LOCK})

define("supervisor_jobs", uid("task_id", primary_key=True), js("config", nullable=False), ts(),
       ForeignKeyConstraint(["task_id"], ["nf_tasks.task_id"]))
define("host_turns", uid("attempt_id", primary_key=True), Column("thread_id", Text, nullable=False, unique=True),
       Column("turn_id", Text, unique=True), Column("phase", Text, nullable=False), ts("updated_at"),
       ForeignKeyConstraint(["attempt_id"], ["nf_call_attempts.attempt_id"]),
       CheckConstraint("phase IN ('THREAD_STARTED','TURN_STARTED','COMPLETED')"),
       CheckConstraint("(turn_id IS NULL) = (phase = 'THREAD_STARTED')"))


class Database:
    def __init__(self, url: str | None = None, *, pool_size: int = 5, max_overflow: int = 0, pool_timeout: float = 2):
        url = url or os.environ.get("NF_DATABASE_URL")
        if not url or not url.startswith("postgresql"):
            raise ValueError("NF_DATABASE_URL must identify a PostgreSQL database")
        if pool_size < 1 or max_overflow < 0 or pool_size + max_overflow > 20:
            raise ValueError("Per-process database budget must be between 1 and 20")
        self.engine = create_engine(url, pool_size=pool_size, max_overflow=max_overflow, pool_timeout=pool_timeout,
            pool_pre_ping=True, connect_args={"options": "-c lock_timeout=3000 -c statement_timeout=15000 -c idle_in_transaction_session_timeout=10000"})

    @contextmanager
    def uow(self, *, write: bool = True, repeatable_read: bool = False):
        from novel_factory.infrastructure.repository import KernelRepository
        with self.engine.connect() as conn:
            if repeatable_read:
                conn = conn.execution_options(isolation_level="REPEATABLE READ")
            with conn.begin():
                if write:
                    mutation_gate(conn)
                yield KernelRepository(conn, tables)

    def close(self):
        self.engine.dispose()
