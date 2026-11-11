"""Cross-check a disposable Redis against the named recovery point in pitr_drill."""

import json
import shutil
import subprocess
import time

from redis import Redis
from redis.exceptions import ConnectionError
from sqlalchemy import text

from novel_factory.application.audit_log import LogService
from novel_factory.infrastructure.eventhub import EventHub


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
