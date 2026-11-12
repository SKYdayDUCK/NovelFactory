"""Operator incidents survive recovery, remain scoped, and never enqueue more events."""

import json
import os
import subprocess
import sys

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import text
from sqlalchemy.exc import TimeoutError as PoolTimeout

from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.diagnostics import inspect
from novel_factory.infrastructure.eventhub import EventHub

from .test_p55_eventhub import RedisProcess, publish

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("NF_TEST_REDIS_URL"), reason="requires real Redis subprocess"
    ),
]


def test_operator_can_query_failure_recovery_and_pending_reclaim(kernel_env, tmp_path):
    server = RedisProcess(tmp_path)
    db = kernel_env["db"]
    hub = EventHub(db, server.client)
    try:
        eid = publish(db)
        server.stop()
        for _ in range(2):
            with db.uow() as repo:
                repo.conn.execute(
                    text("UPDATE nf_bus_publications SET retry_at=now() WHERE event_id=:id"),
                    {"id": eid},
                )
            with pytest.raises(RedisConnectionError):
                hub.relay_once()
        failed = inspect(db, 30)["observations"]
        assert {row["code"] for row in failed} == {"PUBLISH_FAILED", "REDIS_UNAVAILABLE"}
        assert all(row["samples"] == 1 and row["recovered_at"] is None for row in failed)
        server.start()
        hub.diagnostics.run("relay", server.client.ping, ping_only=True)
        by_code = {r["code"]: r for r in inspect(db, 30)["observations"]}
        assert (
            by_code["REDIS_UNAVAILABLE"]["recovered_at"]
            and by_code["PUBLISH_FAILED"]["recovered_at"] is None
        )
        with db.uow() as repo:
            repo.conn.execute(
                text("UPDATE nf_bus_publications SET retry_at=now() WHERE event_id=:id"),
                {"id": eid},
            )
        assert hub.relay_once() == 1
        hub.streams.messages("observability", "statistics", "departed-worker", 1, 30000)
        assert (
            hub.consume_once(
                "statistics", "replacement", lambda conn, event: None, reclaim_idle_ms=0
            )
            == 1
        )
        result = subprocess.run(
            [sys.executable, "scripts/p55_diagnostics.py", "--limit", "20"],
            check=True,
            capture_output=True,
            text=True,
            env={**os.environ, "NF_DATABASE_URL": kernel_env["url"]},
        )
        rows = {r["code"]: r for r in json.loads(result.stdout)["observations"]}
        assert rows["PUBLISH_FAILED"]["recovered_at"]
        assert (
            rows["PENDING_RECLAIMED"]["component"] == "statistics"
            and rows["PENDING_RECLAIMED"]["samples"] == 1
        )
        assert "departed-worker" not in result.stdout and "redis://" not in result.stdout
        with db.uow(write=False) as repo:
            assert repo.conn.scalar(text("SELECT count(*) FROM nf_outbox")) == 1
    finally:
        server.stop()
        server.client.close()


def test_real_pool_timeout_journal_then_db_recovery(kernel_env, capfd, tmp_path):
    server = RedisProcess(tmp_path)
    db = Database(kernel_env["url"], pool_size=1, pool_timeout=0.02)
    hub = EventHub(db, server.client)
    try:
        with db.engine.connect(), pytest.raises(PoolTimeout):
            hub.relay_once()
        err = capfd.readouterr().err
        assert "DATABASE_TIMEOUT" in err and "postgresql" not in err
        hub.relay_once()
        rows = [r for r in inspect(db, 30)["observations"] if r["code"] == "DATABASE_TIMEOUT"]
        assert len(rows) == 1 and rows[0]["recovered_at"] >= rows[0]["last_seen"]
    finally:
        db.close()
        server.stop()
        server.client.close()


def test_transient_handler_sql_timeout_keeps_pending_budget(kernel_env, tmp_path):
    from sqlalchemy.exc import OperationalError

    db = kernel_env["db"]
    server = RedisProcess(tmp_path)
    hub = EventHub(db, server.client, max_failures=1)
    try:
        eid = publish(db)
        hub.relay_once()

        def busy(conn, event):
            conn.execute(text("SET LOCAL lock_timeout='20ms'"))
            conn.execute(text("SELECT pg_advisory_xact_lock(622500222)"))

        with db.engine.begin() as blocker:
            blocker.execute(text("SELECT pg_advisory_xact_lock(622500222)"))
            with pytest.raises(OperationalError):
                hub.consume_once("statistics", "timeout", busy)
        with db.uow(write=False) as repo:
            assert repo.conn.execute(
                text("SELECT status,attempts FROM nf_bus_subscriptions WHERE event_id=:id"),
                {"id": eid},
            ).one() == ("PENDING", 0)
        assert hub.consume_once("statistics", "resumed", busy, reclaim_idle_ms=0) == 1
        rows = inspect(db, 30, component="statistics")["observations"]
        assert any(row["code"] == "DATABASE_TIMEOUT" and row["recovered_at"] for row in rows)
    finally:
        server.stop()
        server.client.close()


def test_disconnected_database_reports_sanitized_service_log():
    import socket

    with socket.socket() as guard:
        # Reserve a local port without listening so a real libpq connection fails.
        guard.bind(("127.0.0.1", 0))
        port = guard.getsockname()[1]
        env = {
            **os.environ,
            "NF_DATABASE_URL": f"postgresql+psycopg://fixture:private-sentinel@127.0.0.1:{port}/fixture",
            "NF_REDIS_URL": os.environ["NF_TEST_REDIS_URL"],
        }
        cmd = subprocess.run(
            [sys.executable, "scripts/run_eventhub.py", "relay", "--once"],
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
    assert cmd.returncode == 1 and "DATABASE_UNAVAILABLE" in cmd.stderr
    assert "private-sentinel" not in cmd.stderr + cmd.stdout
    assert "postgresql" not in cmd.stderr + cmd.stdout and "Traceback" not in cmd.stderr
