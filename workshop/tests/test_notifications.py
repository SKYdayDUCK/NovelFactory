import os
from uuid import uuid4

import pytest
from agent_workshop.admin import enroll
from agent_workshop.auth import Session, authenticate, connect
from agent_workshop.messaging import mailbox, send
from agent_workshop.notifications import notify_batch, receive_batch, replay
from agent_workshop.organization import bootstrap


@pytest.mark.skipif(
    not os.environ.get("AW_REDIS_TEST_URL"), reason="Requires an isolated Redis Streams endpoint"
)
def test_recover_pending_ack_and_rebuild_lost_stream():
    from redis import Redis

    dsn = os.environ["AW_DATABASE_URL"]
    redis = Redis.from_url(os.environ["AW_REDIS_TEST_URL"], decode_responses=True)
    org, stream, group = uuid4().hex, f"aw-test-{uuid4().hex}", uuid4().hex
    with connect(dsn) as conn:
        conn.execute("INSERT INTO aw_installations VALUES(%s,%s,true)", (org, "nf-test"))
        bootstrap(conn, org)
        director = authenticate(conn, enroll(conn, org, org, "volume", 1, 1))
        for key in ("1", "2"):
            send(
                conn,
                director,
                key,
                "actor-1",
                "ASSIGN",
                "organization",
                1,
                60,
            )
    # A new subscriber initially sees the full recoverable outbox, including pre-existing mail.
    replay(dsn)
    while notify_batch(dsn, redis, stream):
        pass
    redis.xgroup_create(stream, group, id="0")
    redis.xreadgroup(group, "stopped-before-sql", {stream: ">"}, count=2)
    assert receive_batch(dsn, redis, stream, group, "new-worker", limit=5000, idle_ms=0) >= 2
    redis.delete(stream)
    replay(dsn)
    while notify_batch(dsn, redis, stream):
        pass
    assert receive_batch(dsn, redis, stream, group, "new-worker", limit=5000, idle_ms=0) == 0
    with connect(dsn) as conn:
        assert len(mailbox(conn, Session(org, "actor-1", 1, 1, "i"))["items"]) == 2
    redis.delete(stream)
