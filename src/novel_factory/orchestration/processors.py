"""Statically bound Redis intake and scoped drains of existing P5 services."""
from dataclasses import dataclass
from types import MappingProxyType
from uuid import uuid4

from sqlalchemy import text

from novel_factory.infrastructure.event_streams import DEFAULT_HANDLER_FAILURE_LIMIT
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
    business_idempotency: str
    input_schema: str = "EventEnvelope.v1"
    handler_failure_limit: int = DEFAULT_HANDLER_FAILURE_LIMIT

    @property
    def consumer_group(self):
        return self.name


PROCESSORS = MappingProxyType({row.name: row for row in (
    Processor("model", "model", "enrolled READY task", "AgentCallEvidence", "ADMIN + current task grant", 16, 900,
              "P5 lease and call ledger; quarantine UNKNOWN",
              "task ID, generation, lease and model call ledger"),
    Processor("world", "database", "world and editorial handoffs", "Kernel or editorial receipt", "ADMIN + enrolled grants", 4, 15,
              "rescan stored candidates and receipts",
              "Kernel commit and editorial handoff receipts"),
    Processor("compute", "cpu", "world.accepted", "DerivedWorldIndex", "ADMIN on target branch", 8, 120,
              "rebuild from immutable WorldRef and keyed inbox",
              "immutable WorldRef, projection watermarks and index Inbox"),
    Processor("log", "observability", "durable domain evidence", "AuditRecord", "trusted audit writer", 4, 15,
              "atomic subscriber receipt and log",
              "subscriber/event ID and immutable audit evidence"),
    Processor("statistics", "observability", "persisted audit record", "MetricProjection", "trusted statistics writer", 4, 15,
              "atomic subscriber receipt and projection",
              "event ID, metric version and per-event contribution"),
    Processor("recovery", "database", "workflow and lease reconciliation", "WorkflowStatus", "ADMIN on target branch", 2, 15,
              "bounded P5 resume; preserve uncertain calls",
              "task generation, dependency receipts and call ledger"),
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
        if group == "world" and envelope.event_type == "world.accepted":
            from novel_factory.application.conflict_resolution import scan_event
            from novel_factory.infrastructure.database import tables
            from novel_factory.infrastructure.repository import KernelRepository
            scan_event(KernelRepository(conn, tables), envelope.event_id)
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
            worker = DerivedWorldWorker(self.kernel)
            if executor is not None:
                lease = worker.reserve_index(*args)
                result = executor.submit(process_reserved_index, *args, lease) if lease else {"status": "IDLE"}
            else:
                receipt = worker.rebuild_one(*args)
                result = {"status": "INDEXED", "message_id": receipt["message_id"]} if receipt else {"status": "IDLE"}
        else:
            summary = self.supervisor.recover_project(*args)
            result = {"status": "IDLE", **summary}
        busy = not isinstance(result, dict) or result["status"] != "IDLE"
        if reconcile and not occurrence and busy:
            with self.kernel.db.uow() as repo:
                repo.conn.execute(_SIGNAL, {**params, "work": self.work, "event": str(uuid4())})
        if occurrence and not busy:
            # Compare the occurrence, not the clock. A new concurrent arrival survives this clear.
            with self.kernel.db.uow() as repo:
                # Capacity/fairness may temporarily hide READY or unindexed work.
                # A completion notification must be enough to resume the drain.
                if processor == "model" and repo.conn.scalar(text("""SELECT EXISTS (SELECT
                    FROM nf_tasks t JOIN nf_supervisor_jobs j USING(task_id)
                    WHERE t.branch_id=:branch AND t.state='READY')"""), params):
                    return result
                if processor == "compute" and repo.conn.scalar(text("""SELECT EXISTS (SELECT
                    FROM nf_outbox o WHERE o.topic='world.accepted' AND o.payload->'world_ref'->>'branch_id'=:branch
                    AND NOT EXISTS(SELECT FROM nf_inbox i WHERE i.consumer=:consumer AND i.message_id=o.outbox_id))"""),
                    {**params, "consumer": "derived-world:nf-derived-world-1"}):
                    return result
                repo.conn.execute(text("""DELETE FROM nf_processor_hints WHERE branch_id=:branch
                    AND processor=:processor AND occurrence=:occurrence"""), {**params, "occurrence": occurrence})
        return result


# Spawn workers build their own SQLAlchemy engine. Never pickle a live connection.
_index_db = None
_index_kernel = None
_index_meter = None


def initialize_index_worker(database_url, object_root, meter=None):
    import atexit

    from novel_factory.application.kernel import Kernel
    from novel_factory.infrastructure.database import Database
    from novel_factory.infrastructure.objects import ObjectStore

    global _index_db, _index_kernel, _index_meter
    _index_meter = meter
    _index_db = Database(database_url, pool_size=1)
    _index_kernel = Kernel(_index_db, ObjectStore(object_root))
    atexit.register(_index_db.close)


def process_reserved_index(principal, work, branch, lease):
    from novel_factory.application.derived_worker import DerivedWorldWorker

    process = DerivedWorldWorker(_index_kernel).process_reserved
    if _index_meter is None:
        receipt = process(principal, work, branch, lease)
    else:
        receipt = _index_meter.measure(process, principal, work, branch, lease)
    return {"status": "INDEXED", "message_id": receipt["message_id"]}
