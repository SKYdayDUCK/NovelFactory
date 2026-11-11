"""Statically bound Redis intake and scoped drains of existing P5 services."""
from dataclasses import dataclass
from types import MappingProxyType
from uuid import uuid4

from sqlalchemy import text

from novel_factory.orchestration import dispatch
from novel_factory.orchestration.tasks import MAINTENANCE_GATE


@dataclass(frozen=True)
class Processor:
    name: str
    resource: str
    supported_work: str
    output: str
    permission: str
    max_workers: int
    timeout_seconds: int
    recovery: str
    input_schema: str = "EventEnvelope.v1"


PROCESSORS = MappingProxyType({row.name: row for row in (
    Processor("model", "model", "enrolled READY task", "AgentCallEvidence", "ADMIN + current task grant", 16, 900,
              "P5 lease and call ledger; quarantine UNKNOWN"),
    Processor("world", "database", "world and editorial handoffs", "Kernel or editorial receipt", "ADMIN + enrolled grants", 4, 15,
              "rescan stored candidates and receipts"),
    Processor("compute", "cpu", "world.accepted", "DerivedWorldIndex", "ADMIN on target branch", 8, 120,
              "rebuild from immutable WorldRef and keyed inbox"),
    Processor("log", "observability", "durable domain evidence", "AuditRecord", "trusted audit writer", 4, 15,
              "atomic subscriber receipt and log"),
    Processor("statistics", "observability", "persisted audit record", "MetricProjection", "trusted statistics writer", 4, 15,
              "atomic subscriber receipt and projection"),
    Processor("recovery", "database", "workflow and lease reconciliation", "WorkflowStatus", "ADMIN on target branch", 2, 15,
              "bounded P5 resume; preserve uncertain calls"),
)})

SCOPED = ("model", "world", "compute", "recovery")
_SIGNAL = text("""
    INSERT INTO nf_processor_hints(branch_id,processor,occurrence)
    SELECT b.branch_id,:processor,CAST(:event AS uuid)
    FROM nf_redis_branches b WHERE b.work_id=:work AND b.branch_id=:branch
    ON CONFLICT(branch_id,processor) DO UPDATE
    SET occurrence=EXCLUDED.occurrence, updated_at=statement_timestamp()
""")


def handler_for(group):
    """This allowlist is selected by process configuration, never by the envelope."""
    if group == "log":
        from novel_factory.application.audit_log import LogService
        return LogService.handle
    if group == "statistics":
        from novel_factory.application.statistics import StatisticsService
        return StatisticsService.handle
    if group not in SCOPED:
        raise ValueError("Unknown processor group")

    def signal(conn, envelope):
        if envelope.work_id and envelope.branch_id:
            conn.execute(_SIGNAL, {"work": envelope.work_id, "branch": envelope.branch_id,
                                   "processor": group, "event": envelope.event_id})
    return signal


class TaskDispatcher:
    def __init__(self, supervisor, principal, work, branch):
        self.supervisor, self.principal = supervisor, principal
        self.kernel, self.work, self.branch = supervisor.kernel, work, branch

    def _authorize(self, repo):
        self.kernel.authorize(repo, self.principal, self.work, "ADMIN", self.branch)

    def switch(self, transport):
        if transport not in {"redis", "postgres"}:
            raise ValueError("Unknown transport")
        scope = {"work": self.work, "branch": self.branch}
        with self.kernel.db.uow() as repo:
            self._authorize(repo)
            repo.conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": MAINTENANCE_GATE + 1})
            if transport == "redis":
                repo.conn.execute(text("""
                    INSERT INTO nf_redis_branches(work_id,branch_id) VALUES (:work,:branch)
                    ON CONFLICT DO NOTHING"""), scope)
                for processor in SCOPED:
                    repo.conn.execute(_SIGNAL, {**scope, "event": str(uuid4()), "processor": processor})
            else:
                repo.conn.execute(text("DELETE FROM nf_redis_branches WHERE work_id=:work AND branch_id=:branch"), scope)
            repo.conn.execute(text("DELETE FROM nf_dispatch_waiters WHERE branch_id=:branch"), scope)
            repo.conn.exec_driver_sql("NOTIFY nf_supervisor_wake")
        return {"status": "CONFIGURED", "transport": transport}

    def run_once(self, processor, *, executor=None, reconcile=False):
        if processor not in SCOPED:
            raise ValueError("Unknown project processor")
        params = {"branch": self.branch, "processor": processor}
        with self.kernel.db.uow(write=False) as repo:
            self._authorize(repo)
            if not dispatch.redis_enabled(repo.conn, self.work, self.branch):
                return {"status": "IDLE"}
            occurrence = repo.conn.scalar(text("""SELECT occurrence FROM nf_processor_hints
                WHERE branch_id=:branch AND processor=:processor"""), params)
        if not occurrence and not reconcile:
            return {"status": "IDLE"}
        args = (self.principal, self.work, self.branch)
        if processor == "model":
            result = self.supervisor.admit_model(*args, executor=executor, transport="redis")
        elif processor == "world":
            result = self.supervisor.advance_world(*args) or {"status": "IDLE"}
        elif processor == "compute":
            from novel_factory.application.derived_worker import DerivedWorldWorker
            receipt = DerivedWorldWorker(self.kernel).rebuild_one(*args)
            result = {"status": "INDEXED", "message_id": receipt["message_id"]} if receipt else {"status": "IDLE"}
        else:
            summary = self.supervisor.recover_project(*args)
            result = {"status": "IDLE", **summary}
        if occurrence and isinstance(result, dict) and result["status"] == "IDLE":
            # Compare the occurrence, not the clock. A new concurrent arrival survives this clear.
            with self.kernel.db.uow() as repo:
                repo.conn.execute(text("""DELETE FROM nf_processor_hints WHERE branch_id=:branch
                    AND processor=:processor AND occurrence=:occurrence"""), {**params, "occurrence": occurrence})
        return result
