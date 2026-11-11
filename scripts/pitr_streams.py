"""Cross-check a disposable Redis against the named recovery point in pitr_drill."""

import json
import shutil
import subprocess
import time

from redis import Redis
from redis.exceptions import ConnectionError
from sqlalchemy import text

from novel_factory.application.audit_log import LogService
from novel_factory.application.queries import WorldQueryService
from novel_factory.application.statistics import CURRENT_VERSION, StatisticsService
from novel_factory.fixtures import create_bound_task, load_fixture, point, run_command
from novel_factory.infrastructure.eventhub import EventHub


def stage_semantic_conflict(kernel):
    """Read on one workflow, accept an earlier effective change on another."""
    world = load_fixture(kernel, "white_bridge")
    owner, base = world["principal"], world["world_ref"]
    guard = world["ids"]["guard1"]
    task = create_bound_task(kernel, owner, base, point(20), [guard], ["on_duty"])
    WorldQueryService(kernel).query(owner, {
        "schema_version": "2.1", "task_id": task["task_id"], "generation": task["generation"],
        "read_contract": {"revision": {"mode": "FIXED", "world_ref": base},
                          "process_policy": "ALLOW_PARTIAL", "index_policy": "AUTHORITATIVE",
                          "max_wait_ms": 0},
        "knowledge_cut": {"plane": "WORLD"}, "subject_time": {"kind": "POINT", "at": point(20)},
        "query": {"kind": "STATE", "subject_ids": [guard], "predicates": ["on_duty"]},
        "page_size": 32, "cursor": None,
    })
    changed = run_command(kernel, owner, base, "SET_RELATION", {
        "subject_id": guard, "predicate": "on_duty",
        "object_value": {"kind": "BOOLEAN", "value": False}, "at": point(10),
    })
    return (owner, base["work_id"], base["branch_id"]), task, changed


class StreamsDrill:
    def __init__(self, root, port):
        binary = shutil.which("redis-server")
        if not binary:
            raise RuntimeError("redis-server is required for --with-redis")
        self.process = subprocess.Popen(
            [
                binary,
                "--bind",
                "127.0.0.1",
                "--port",
                str(port),
                "--dir",
                str(root),
                "--save",
                "",
                "--appendonly",
                "no",
                "--logfile",
                str(root / "redis.log"),
            ],
            start_new_session=True,
        )
        self.client = Redis(host="127.0.0.1", port=port, socket_timeout=2, decode_responses=True)
        try:
            for _ in range(100):
                if self.process.poll() is not None:
                    raise RuntimeError("Isolated Redis exited during startup")
                try:
                    if self.client.ping():
                        return
                except ConnectionError:
                    time.sleep(0.05)
            raise RuntimeError("Isolated Redis did not start")
        except BaseException:
            self.close()
            raise

    def close(self):
        self.client.close()
        self.process.terminate()
        self.process.wait(timeout=10)

    @staticmethod
    def drain(operation):
        for _ in range(100):
            if operation() == 0:
                return
        raise RuntimeError("Bounded fixture did not drain")

    def publish(self, db):
        self.drain(EventHub(db, self.client).relay_once)

    def acknowledge_target(self, db):
        hub = EventHub(db, self.client)
        self.drain(lambda: hub.consume_once("log", "pitr-before", LogService.handle))

    def create_semantic_case(self, kernel):
        """Create decisions after the base backup so only WAL can bring them back."""
        from novel_factory.application.conflict_resolution import ConflictResolutionService

        scope, task, changed = stage_semantic_conflict(kernel)
        resolver = ConflictResolutionService(kernel)
        for _ in range(8):
            resolver.scan_one(*scope)
            resolver.resolve_one(*scope)
            with kernel.db.uow(write=False) as repo:
                case_id = repo.conn.scalar(text("""
                    SELECT c.case_id FROM nf_conflict_cases c JOIN nf_conflict_resolutions r USING(case_id)
                    WHERE c.task_id=:task AND c.generation=:gen AND c.world_revision_id=:changed
                """), {"task": task["task_id"], "gen": task["generation"],
                       "changed": changed["world_ref"]["world_revision_id"]})
            if case_id is not None:
                break
        else:
            raise RuntimeError("Semantic recovery fixture did not reach a resolution")
        self.publish(kernel.db)
        hub = EventHub(kernel.db, self.client)
        self.drain(lambda: hub.consume_once("statistics", "pitr-stats", StatisticsService.handle))
        return self.semantic_witness(kernel.db, case_id)

    @staticmethod
    def semantic_witness(db, case_id):
        """Read immutable, scoped receipts and their actual incremental metric contributions."""
        with db.uow(write=False) as repo:
            result = repo.conn.execute(text("""
                SELECT c.case_id::text, r.receipt_id::text, c.conflict_type, r.outcome,
                       (SELECT array_agg(o.observation_id::text ORDER BY o.observation_id)
                        FROM nf_semantic_reads o WHERE o.task_id=c.task_id AND o.generation=c.generation)
                           AS observation_ids,
                       c.world_revision_id::text AS change_revision,
                       (SELECT jsonb_object_agg(a.event_type,m.metrics) FROM nf_audit_events a
                        JOIN nf_metric_contributions m USING(event_id)
                        WHERE a.evidence_id=c.case_id AND m.metric_version=:v
                        AND a.event_type IN ('semantic.conflict.detected','semantic.conflict.resolved'))
                           AS contributions
                FROM nf_conflict_cases c JOIN nf_conflict_resolutions r USING(case_id)
                JOIN nf_semantic_changes ch USING(world_revision_id)
                WHERE c.case_id=:id
            """), {"id": case_id, "v": CURRENT_VERSION}).mappings().one()
        contributions = result["contributions"] or {}
        if not result["observation_ids"] or set(contributions) != {
                "semantic.conflict.detected", "semantic.conflict.resolved"}:
            raise RuntimeError("Incomplete semantic read, resolution, or metric witnesses")
        return dict(result)

    def verify(self, db, target, later):
        hub = EventHub(db, self.client)
        # Redis has acknowledged pre-target events after the PG restore point.
        # It also has records for a later world revision excluded by WAL replay.
        entries = self.client.xrange("nf:events:domain:v1")
        later_ids = {
            rid
            for rid, fields in entries
            if (json.loads(fields["envelope"]).get("world_ref") or {}).get("world_revision_id")
            == later
        }
        if not later_ids:
            raise RuntimeError("PITR drill did not publish the excluded revision")
        with db.uow(write=False) as repo:
            target_id = repo.conn.scalar(
                text("SELECT outbox_id FROM nf_outbox WHERE dedupe_key=:key"),
                {"key": "world:" + target},
            )
            pending = repo.conn.scalar(
                text("""SELECT status FROM nf_bus_subscriptions
                WHERE consumer='log' AND event_id=:id"""),
                {"id": target_id},
            )
            if pending != "PENDING":
                raise RuntimeError("Restored subscriber was not behind Redis")
        hub.repair_once(1000)
        self.drain(hub.relay_once)
        self.drain(
            lambda: hub.consume_once("log", "pitr-after", LogService.handle, reclaim_idle_ms=0)
        )
        with db.uow(write=False) as repo:
            quarantined = set(
                repo.conn.scalars(
                    text("""SELECT redis_id FROM nf_bus_quarantine
                WHERE stream_key='domain' AND consumer='log' AND error_code='SOURCE_MISMATCH'""")
                )
            )
            receipt = repo.conn.scalar(
                text("""SELECT count(*) FROM nf_inbox
                WHERE message_id=:id AND consumer='p55:log'"""),
                {"id": target_id},
            )
            audit = repo.conn.scalar(
                text("SELECT count(*) FROM nf_audit_events WHERE event_id=:id"), {"id": target_id}
            )
            rejected = repo.conn.scalar(
                text("SELECT count(*) FROM nf_revisions WHERE world_revision_id=:id"), {"id": later}
            )
        if receipt != 1 or audit != 1 or not later_ids <= quarantined or rejected:
            raise RuntimeError("Redis/PG recovery reconciliation failed")
        return {
            "redis_version": self.client.info("server")["redis_version"],
            "target_subscriber_receipts": receipt,
            "target_audit_rows": audit,
            "excluded_redis_entries_quarantined": len(later_ids),
            "excluded_world_revisions": rejected,
        }
