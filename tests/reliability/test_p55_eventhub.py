"""Adversarial transport boundaries with disposable PostgreSQL and a real Redis subprocess."""

import os
import shutil
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest
from redis import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import text

from novel_factory.infrastructure.event_streams import STREAMS
from novel_factory.infrastructure.eventhub import EventHub
from novel_factory.orchestration.tasks import enqueue_outbox

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.recovery,
    pytest.mark.skipif(
        not os.environ.get("NF_TEST_REDIS_URL"), reason="requires Redis integration environment"
    ),
]


class RedisProcess:
    def __init__(self, directory):
        exe = shutil.which("redis-server")
        assert exe, "Redis integration selected; redis-server must be on PATH"
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.command = [
            exe,
            "--bind",
            "127.0.0.1",
            "--port",
            str(self.port),
            "--dir",
            str(directory),
            "--appendonly",
            "yes",
            "--appendfsync",
            "always",
            "--save",
            "",
            "--logfile",
            str(Path(directory) / "redis.log"),
        ]
        self.client = Redis(
            host="127.0.0.1",
            port=self.port,
            decode_responses=True,
            socket_connect_timeout=1,
            socket_timeout=1,
        )
        self.proc = None
        self.start()

    def start(self):
        self.proc = subprocess.Popen(self.command)
        for _ in range(200):
            try:
                if self.client.ping():
                    return
            except RedisConnectionError:
                pass
            assert self.proc.poll() is None
            time.sleep(0.01)
        pytest.fail("Redis startup deadline")

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(timeout=10)


@pytest.fixture
def server(tmp_path):
    service = RedisProcess(tmp_path)
    try:
        yield service
    finally:
        service.stop()
        service.client.close()


def publish(db, topic="audit.recorded", payload=None):
    with db.uow() as repo:
        ident = str(uuid4())
        return str(
            enqueue_outbox(
                repo.conn,
                repo.tables,
                topic,
                "audit",
                ident,
                payload or {"trace_id": ident},
                "test:" + ident,
            )
        )


def value(db, statement, **params):
    with db.uow(write=False) as repo:
        return repo.conn.scalar(text(statement), params)


def receipt(db, eid, group="statistics"):
    return value(
        db,
        "SELECT count(*) FROM nf_inbox WHERE message_id=:id AND consumer=:g",
        id=eid,
        g="p55:" + group,
    )


def handler(conn, event):
    # No unique constraint on this deliberately simple witness; duplicate calls
    # would change the count even when the Inbox uniqueness constraint holds.
    conn.execute(text("INSERT INTO nf_test_bus_effect(id) VALUES(:id)"), {"id": event.event_id})


@pytest.fixture
def hub(kernel_env, server):
    db = kernel_env["db"]
    with db.uow() as repo:
        repo.conn.execute(text("CREATE TABLE IF NOT EXISTS nf_test_bus_effect(id uuid NOT NULL)"))
    return EventHub(db, server.client, max_failures=2)


def test_relay_crash_before_recording_and_worker_crash_before_ack(hub, server):
    db = hub.db
    eid = publish(db)
    # Exit the OS process at the two actual external boundaries. All Redis
    # commands and SQL execute against real servers; only the exit is injected.
    program = """
import os
from redis import Redis
from redis.client import Pipeline
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.eventhub import EventHub
from sqlalchemy import text
db=Database(os.environ["PRIVATE_DB"])
client=Redis.from_url(os.environ["PRIVATE_REDIS"],decode_responses=True)
bus=EventHub(db,client)
if os.environ["CRASH"]=="relay":
    original=Pipeline.execute
    def exit_after_send(pipe):
        original(pipe)
        os._exit(74)
    Pipeline.execute=exit_after_send
    bus.relay_once()
else:
    def exit_before_ack(*args):
        os._exit(75)
    client.xack=exit_before_ack
    bus.consume_once("statistics","worker-who-exits",
        lambda c,e: c.execute(text("INSERT INTO nf_test_bus_effect(id) VALUES(:id)"),{"id":e.event_id}))
"""
    env = {
        **os.environ,
        "PRIVATE_DB": db.engine.url.render_as_string(hide_password=False),
        "PRIVATE_REDIS": f"redis://127.0.0.1:{server.port}/0",
    }
    assert (
        subprocess.run(
            [sys.executable, "-c", program], env={**env, "CRASH": "relay"}, check=False
        ).returncode
        == 74
    )
    with db.uow() as repo:
        repo.conn.execute(
            text("UPDATE nf_bus_publications SET lease_until=now() WHERE event_id=:id"), {"id": eid}
        )
    hub.relay_once()
    rs = server.client
    stream = STREAMS["observability"]
    assert rs.xlen(stream) >= 2
    assert (
        subprocess.run(
            [sys.executable, "-c", program], env={**env, "CRASH": "worker"}, check=False
        ).returncode
        == 75
    )
    # Abrupt Redis death also preserves the pending entry with AOF always.
    server.proc.kill()
    server.proc.wait(timeout=10)
    server.start()
    assert rs.xpending(stream, "statistics")["pending"] >= 1
    hub.consume_once("statistics", "replacement", handler, reclaim_idle_ms=0)
    assert receipt(db, eid) == 1
    assert value(db, "SELECT count(*) FROM nf_test_bus_effect WHERE id=:id", id=eid) == 1
    assert rs.xpending(stream, "statistics")["pending"] == 0


def test_disconnect_commits_durable_work_and_missing_entry_is_replayed(hub, server):
    db = hub.db
    server.stop()
    eid = publish(db)
    with pytest.raises(RedisConnectionError):
        hub.relay_once()
    assert hub.status()["redis"] == "UNAVAILABLE" and not receipt(db, eid)
    with db.uow() as repo:
        repo.conn.execute(
            text("UPDATE nf_bus_publications SET retry_at=now() WHERE event_id=:id"), {"id": eid}
        )
    server.start()
    hub.relay_once()
    server.client.delete(STREAMS["observability"])  # Test instance is private.
    assert hub.repair_once() >= 1
    hub.relay_once()
    hub.consume_once("statistics", "restored", handler)
    assert receipt(db, eid) == 1


def test_repair_previously_acked_redis_position_and_out_of_order(hub, server):
    db, client = hub.db, server.client
    ids = [publish(db) for _ in range(2)]
    hub.relay_once()
    hub.streams.register("observability", "statistics")
    stream = STREAMS["observability"]
    batch = client.xreadgroup("statistics", "ahead", {stream: ">"}, count=100)[0][1]
    for rid, fields in reversed(batch):
        client.xack(stream, "statistics", rid)  # Redis cursor ahead of PG durable receipts.
    assert hub.repair_once() >= 2
    hub.relay_once()
    entries = client.xreadgroup("statistics", "post-restore", {stream: ">"}, count=100)[0][1]
    for rid, fields in reversed(entries):
        assert hub._apply("observability", "statistics", rid, fields, handler)
        client.xack(stream, "statistics", rid)
    assert all(receipt(db, eid) == 1 for eid in ids)


def test_multiple_relays_competing_consumers_and_independent_subscriptions(hub, server):
    db, client = hub.db, server.client
    targets = {publish(db, "world.requested") for _ in range(32)}
    with ThreadPoolExecutor(max_workers=4) as pool:
        for _ in range(4):
            list(pool.map(lambda _: EventHub(db, client).relay_once(8), range(4)))
    assert client.xlen(STREAMS["world"]) == 32

    def worker(i):
        return EventHub(db, client).consume_once("world", str(i), handler, limit=8)

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(worker, range(4))) == 32
    assert all(receipt(db, eid, "world") and not receipt(db, eid, "log") for eid in targets)
    with db.uow() as repo:
        repo.conn.execute(
            text(
                "UPDATE nf_bus_publications SET created_at=now()-interval '1 day' "
                "WHERE stream_key='world'"
            )
        )
    assert not hub.trim_once(1)
    hub.consume_once("log", "audit-writer", lambda conn, env: None, limit=64)
    assert hub.trim_once(1) == 32
    assert all(receipt(db, eid, "log") for eid in targets)
    assert (
        value(
            db,
            "SELECT count(*) FROM nf_test_bus_effect WHERE id=ANY(CAST(:ids AS uuid[]))",
            ids=list(targets),
        )
        == 32
    )


def test_poison_rollback_dead_letter_redrive_and_source_forgery(hub, server):
    db, client = hub.db, server.client
    eid = publish(db)
    hub.relay_once()

    def broken(conn, env):
        handler(conn, env)
        raise ValueError("secret diagnostics")

    for worker in ("first", "retry"):
        hub.consume_once("statistics", worker, broken, reclaim_idle_ms=0)
    assert not receipt(db, eid)
    assert value(db, "SELECT count(*) FROM nf_test_bus_effect WHERE id=:id", id=eid) == 0
    assert value(db, "SELECT status FROM nf_bus_subscriptions WHERE event_id=:id", id=eid) == "DEAD"
    assert hub.redrive(eid, "statistics") == 1
    hub.relay_once()
    stream = STREAMS["observability"]
    message = client.xrange(stream, count=1)[0][1]["envelope"]
    client.xadd(stream, {"envelope": message.replace(eid, str(uuid4()))})
    client.xadd(stream, {"envelope": message.replace("reference_only", "secret diagnostics")})
    client.xadd(stream, {"prompt": "secret diagnostics"})
    hub.consume_once("statistics", "recovered", handler)
    assert receipt(db, eid) == 1
    assert value(db, "SELECT count(*) FROM nf_bus_quarantine WHERE stream_key='observability'") >= 3
    assert value(db, "SELECT count(*) FROM nf_bus_quarantine WHERE error_code LIKE '%secret%'") == 0
