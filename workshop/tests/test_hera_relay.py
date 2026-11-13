"""Real PostgreSQL/Redis relay after losing a publication acknowledgement."""
import os
from uuid import uuid4

import pytest
from alembic import command
from redis import Redis

from agent_workshop import notifications
from agent_workshop.admin import enroll
from agent_workshop.auth import authenticate, connect
from agent_workshop.messaging import send
from agent_workshop.organization import bootstrap
from test_installation_ownership import isolated_database  # noqa: F401


@pytest.mark.skipif(not os.environ.get("AW_REDIS_TEST_URL"), reason="Requires isolated Redis Streams")
def test_grouped_relay_publication_crash_and_authorized_inbox_replay(isolated_database, monkeypatch):
    dsn, cfg = isolated_database
    command.upgrade(cfg, "head")
    redis = Redis.from_url(os.environ["AW_REDIS_TEST_URL"], decode_responses=True)
    org = uuid4().hex
    stream, group = f"hera:{org}", org
    with connect(dsn) as conn:
        conn.execute("INSERT INTO aw_installations VALUES(%s,%s,true)", (org, "nf-test"))
        bootstrap(conn, org, installation_id=org)
        director = authenticate(conn, enroll(conn, org, org, "volume", 1, 1))
        for idx in range(40):
            send(conn, director, str(idx), "actor-1", "ASSIGN", "organization", 1, 3600)

    opened = 0

    def crashed_after_xadd(uri):
        nonlocal opened
        opened += 1
        if opened == 2:
            raise ConnectionError("lost publisher before its SQL marking transaction")
        return connect(uri)

    with monkeypatch.context() as patch:
        patch.setattr(notifications, "connect", crashed_after_xadd)
        with pytest.raises(ConnectionError, match="lost publisher"):
            notifications.notify_batch(dsn, redis, stream, grouped=True)
    assert redis.xlen(stream) == 32
    assert notifications.notify_batch(dsn, redis, stream, grouped=True) == 40
    assert redis.xlen(stream) == 72
    assert notifications.receive_batch(dsn, redis, stream, group, "one", grouped=True) == 40
    with connect(dsn) as conn:
        assert conn.execute("SELECT count(*) AS n FROM aw_inbox").fetchone()["n"] == 40
        assert conn.execute("SELECT count(*) AS n FROM aw_outbox WHERE last_notified IS NULL").fetchone()["n"] == 0
    assert redis.xpending(stream, group)["pending"] == 0
    assert notifications.replay(dsn) == 40
    assert notifications.notify_batch(dsn, redis, stream, grouped=True) == 40
    assert notifications.receive_batch(dsn, redis, stream, group, "two", grouped=True) == 0
    redis.delete(stream)
