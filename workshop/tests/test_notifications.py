import os
import subprocess
import sys
import time
from uuid import uuid4

import pytest
from agent_workshop.admin import enroll
from agent_workshop.auth import authenticate, connect
from agent_workshop.messaging import accept_notification, ack, mailbox, send
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
        bootstrap(conn, org, installation_id=org)
        director = authenticate(conn, enroll(conn, org, org, "volume", 1, 1))
        actor_token = enroll(conn, org, org, "actor-1", 1, 1)
        assert director.org_member
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
    entries = redis.xrange(stream)
    pending = redis.xreadgroup(group, "interrupted", {stream: ">"}, count=2)[0][1]
    assert len(pending) == 2 and redis.xpending(stream, group)["pending"] == 2
    # First notification committed to SQL just before process loss. The second
    # was delivered by Redis but had no PostgreSQL transaction before the loss.
    with connect(dsn) as conn:
        assert accept_notification(conn, group, pending[0][1]["event_id"])
    assert receive_batch(dsn, redis, stream, group, "new-worker", limit=5000, idle_ms=0) == len(entries) - 1
    assert redis.xpending(stream, group)["pending"] == 0
    redis.delete(stream)
    replay(dsn)
    while notify_batch(dsn, redis, stream):
        pass
    assert receive_batch(dsn, redis, stream, group, "new-worker", limit=5000, idle_ms=0) == 0
    with connect(dsn) as conn:
        actor = authenticate(conn, actor_token)
        assert actor.org_member and len(mailbox(conn, actor)["items"]) == 2
        accepted = conn.execute("""SELECT count(*) AS n FROM aw_inbox i JOIN aw_outbox o USING(event_id)
                                   WHERE i.subscriber=%s AND o.org_id=%s""", (group, org)).fetchone()
        assert accepted["n"] == 2
    redis.delete(stream)


@pytest.mark.skipif(
    not os.environ.get("AW_REDIS_TEST_CONTAINER"),
    reason="Requires a disposable Redis sidecar for a real stop and restart",
)
def test_postgres_mail_during_redis_outage_and_new_worker_processes():
    from redis import Redis
    from redis.backoff import NoBackoff
    from redis.exceptions import ConnectionError, TimeoutError
    from redis.retry import Retry

    dsn = os.environ["AW_DATABASE_URL"]
    endpoint = os.environ["AW_REDIS_TEST_URL"]
    container = os.environ["AW_REDIS_TEST_CONTAINER"]
    redis = Redis.from_url(endpoint, decode_responses=True, socket_connect_timeout=1,
                           socket_timeout=1, retry=Retry(NoBackoff(), 0))
    stream, group, org = f"aw-restart-{uuid4().hex}", uuid4().hex, uuid4().hex
    assert redis.ping()
    with connect(dsn) as conn:
        conn.execute("INSERT INTO aw_installations VALUES(%s,%s,true)", (org, "nf-outage"))
        bootstrap(conn, org, installation_id=org)
        director_token = enroll(conn, org, org, "volume", 1, 1)
        actor_token = enroll(conn, org, org, "actor-1", 1, 1)

    subprocess.run(["docker", "stop", "-t", "2", container], check=True, capture_output=True)
    try:
        with connect(dsn) as conn:
            director = authenticate(conn, director_token)
            message = send(conn, director, "during-outage", "actor-1", "ASSIGN", "organization", 1, 3600)
        with pytest.raises((ConnectionError, TimeoutError)):
            notify_batch(dsn, redis, stream)
        with connect(dsn) as conn:
            actor = authenticate(conn, actor_token)
            assert mailbox(conn, actor)["items"][0]["message_id"] == message["message_id"]
            assert ack(conn, actor, message["message_id"]) == ack(conn, actor, message["message_id"])
            row = conn.execute("SELECT event_id,last_notified FROM aw_outbox WHERE org_id=%s", (org,)).fetchone()
            assert row["last_notified"] is None
            event = str(row["event_id"])
    finally:
        subprocess.run(["docker", "start", container], check=True, capture_output=True)
    until = time.monotonic() + 30
    while True:
        try:
            if redis.ping():
                break
        except (ConnectionError, TimeoutError):
            if time.monotonic() >= until:
                raise
            time.sleep(0.2)

    def worker(action, name):
        result = subprocess.run(
            [sys.executable, "-m", "agent_workshop.notifications", action, "--stream", stream,
             "--subscriber", group, "--worker", name], check=True, capture_output=True, text=True,
            env={**os.environ, "AW_REDIS_URL": endpoint},
        )
        return int(result.stdout.strip())

    try:
        while worker("publish", "restarted-publisher"):
            pass
        while worker("receive", "restarted-consumer"):
            pass
        redis.xadd(stream, {"event_id": event})
        assert worker("receive", "replacement-consumer") == 0
        assert redis.xpending(stream, group)["pending"] == 0
        with connect(dsn) as conn:
            assert conn.execute(
                "SELECT count(*) AS n FROM aw_inbox WHERE subscriber=%s AND event_id=%s",
                (group, event),
            ).fetchone()["n"] == 1
            assert conn.execute(
                "SELECT count(*) AS n FROM aw_deliveries WHERE org_id=%s AND acknowledged_at IS NOT NULL",
                (org,),
            ).fetchone()["n"] == 1
    finally:
        redis.delete(stream)
