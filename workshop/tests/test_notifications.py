import json
import os
import shutil
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from secrets import token_hex
from tempfile import TemporaryDirectory
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
    with connect(dsn) as conn:
        own_events = {str(row["event_id"]) for row in conn.execute(
            "SELECT event_id FROM aw_outbox WHERE org_id=%s", (org,)
        )}
    assert len(own_events) == 2
    pending = redis.xreadgroup(group, "interrupted", {stream: ">"}, count=len(entries))[0][1]
    outstanding = [fields["event_id"] for _, fields in pending if fields["event_id"] in own_events]
    assert set(outstanding) == own_events and len(outstanding) == 2
    assert redis.xpending(stream, group)["pending"] == len(entries)
    # First notification committed to SQL just before process loss. The second
    # was delivered by Redis but had no PostgreSQL transaction before the loss.
    with connect(dsn) as conn:
        assert accept_notification(conn, group, outstanding[0])
    accepted = receive_batch(dsn, redis, stream, group, "new-worker", limit=5000, idle_ms=0)
    with connect(dsn) as conn:
        # Other organizations may have revoked or expired events in the full replay.
        # Only the first of our two eligible hints had a committed receipt before this worker.
        receipts = conn.execute("SELECT event_id FROM aw_inbox WHERE subscriber=%s", (group,)).fetchall()
        assert accepted == len(receipts) - 1 and accepted >= 1
        assert {str(row["event_id"]) for row in receipts} >= own_events
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


@pytest.fixture
def private_redis():
    """Only terminate processes or containers started by this test."""
    sidecar = os.environ.get("AW_REDIS_TEST_CONTAINER")
    if sidecar:
        # CI supplies an image via its configured sidecar. Leave that service running.
        image = subprocess.check_output(
            ["docker", "inspect", "--format", "{{.Image}}", sidecar], text=True
        ).strip()
        container = subprocess.check_output(
            [
                "docker",
                "run",
                "--detach",
                "--publish",
                "127.0.0.1::6379",
                image,
                "redis-server",
                "--save",
                "",
                "--appendonly",
                "no",
            ],
            text=True,
        ).strip()
        try:
            port = subprocess.check_output(
                ["docker", "port", container, "6379/tcp"], text=True
            ).strip()

            @contextmanager
            def outage():
                subprocess.run(["docker", "kill", container], check=True, capture_output=True)
                try:
                    yield
                finally:
                    subprocess.run(["docker", "start", container], check=True, capture_output=True)

            yield f"redis://{port}/0", outage, "docker"
        finally:
            subprocess.run(["docker", "rm", "--force", container], check=True, capture_output=True)
        return

    binary = shutil.which("redis-server")
    if not binary:
        pytest.skip("Requires redis-server or a disposable Redis test container")
    from redis import Redis
    from redis.exceptions import ConnectionError

    with TemporaryDirectory() as directory:
        with socket.socket() as port_probe:
            port_probe.bind(("127.0.0.1", 0))
            port = port_probe.getsockname()[1]
        # An independently generated password and exact PID check prevent a raced port from
        # admitting this test to another service. Only Popen handles are signaled.
        password = token_hex(32)
        endpoint = f"redis://:{password}@127.0.0.1:{port}/0"
        config = Path(directory) / "redis.conf"
        config.write_text(
            f"bind 127.0.0.1\nport {port}\nrequirepass {password}\n"
            f'dir "{directory}"\nsave ""\nappendonly no\n'
        )
        config.chmod(0o600)

        def start():
            process = subprocess.Popen([binary, str(config)], stdout=subprocess.DEVNULL)
            try:
                with Redis.from_url(endpoint, socket_connect_timeout=0.2) as client:
                    until = time.monotonic() + 10
                    while process.poll() is None and time.monotonic() < until:
                        try:
                            assert client.info("server")["process_id"] == process.pid
                            return process
                        except ConnectionError:
                            time.sleep(0.05)
                raise RuntimeError("Private Redis child did not start")
            except BaseException:
                process.terminate()
                process.wait(timeout=10)
                raise

        process = start()

        @contextmanager
        def outage():
            nonlocal process
            process.kill()
            process.wait(timeout=10)
            try:
                yield
            finally:
                process = start()

        try:
            yield endpoint, outage, "native"
        finally:
            process.terminate()
            process.wait(timeout=10)


def test_postgres_mail_during_redis_outage_and_new_worker_processes(private_redis, record_property):
    from redis import Redis
    from redis.backoff import NoBackoff
    from redis.exceptions import ConnectionError, TimeoutError
    from redis.retry import Retry

    dsn = os.environ["AW_DATABASE_URL"]
    endpoint, outage, mode = private_redis
    redis = Redis.from_url(
        endpoint,
        decode_responses=True,
        socket_connect_timeout=1,
        socket_timeout=1,
        retry=Retry(NoBackoff(), 0),
    )

    def wait_online():
        until = time.monotonic() + 10
        while True:
            try:
                return redis.info("server")
            except (ConnectionError, TimeoutError):
                if time.monotonic() >= until:
                    raise
                time.sleep(0.05)

    stream, group, org = f"aw-restart-{uuid4().hex}", uuid4().hex, uuid4().hex
    before = wait_online()
    with connect(dsn) as conn:
        conn.execute("INSERT INTO aw_installations VALUES(%s,%s,true)", (org, "nf-outage"))
        bootstrap(conn, org, installation_id=org)
        director_token = enroll(conn, org, org, "volume", 1, 1)
        actor_token = enroll(conn, org, org, "actor-1", 1, 1)

        director = authenticate(conn, director_token)
        prior = send(conn, director, "before-outage", "actor-1", "ASSIGN", "organization", 1, 3600)
        earlier_event = str(
            conn.execute(
                "SELECT event_id FROM aw_outbox WHERE org_id=%s AND message_id=%s",
                (org, prior["message_id"]),
            ).fetchone()["event_id"]
        )
    while notify_batch(dsn, redis, stream):
        pass
    assert earlier_event in {fields["event_id"] for _, fields in redis.xrange(stream)}

    with outage():
        with connect(dsn) as conn:
            director = authenticate(conn, director_token)
            message = send(
                conn, director, "during-outage", "actor-1", "ASSIGN", "organization", 1, 3600
            )
        with pytest.raises((ConnectionError, TimeoutError)):
            notify_batch(dsn, redis, stream)
        with connect(dsn) as conn:
            actor = authenticate(conn, actor_token)
            assert {m["message_id"] for m in mailbox(conn, actor)["items"]} == {
                message["message_id"],
                prior["message_id"],
            }
            assert ack(conn, actor, message["message_id"]) == ack(
                conn, actor, message["message_id"]
            )
            row = conn.execute(
                "SELECT event_id,last_notified FROM aw_outbox WHERE org_id=%s AND message_id=%s",
                (org, message["message_id"]),
            ).fetchone()
            assert row["last_notified"] is None
            event = str(row["event_id"])
    after = wait_online()
    assert before["run_id"] != after["run_id"]
    assert redis.dbsize() == 0  # The previously published entry disappeared with the process.
    assert replay(dsn) >= 1

    def worker(action, name):
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "agent_workshop.notifications",
                action,
                "--stream",
                stream,
                "--subscriber",
                group,
                "--worker",
                name,
            ],
            check=True,
            capture_output=True,
            text=True,
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
            accepted = conn.execute(
                """SELECT event_id FROM aw_inbox JOIN aw_outbox USING(event_id)
                WHERE subscriber=%s AND org_id=%s""",
                (group, org),
            ).fetchall()
            assert sorted(str(r["event_id"]) for r in accepted) == sorted([earlier_event, event])
            assert (
                conn.execute(
                    "SELECT count(*) AS n FROM aw_deliveries WHERE org_id=%s AND acknowledged_at IS NOT NULL",
                    (org,),
                ).fetchone()["n"]
                == 1
            )
        record_property(
            "physical_redis_outage",
            json.dumps(
                {
                    "mode": mode,
                    "version": after["redis_version"],
                    "old_run_id": before["run_id"],
                    "new_run_id": after["run_id"],
                    "old_pid": before["process_id"],
                    "new_pid": after["process_id"],
                    "prior_event": earlier_event,
                    "outage_event": event,
                    "accepted_once_each": len(accepted),
                    "acknowledged": 1,
                    "redis_pending": 0,
                },
                sort_keys=True,
            ),
        )
    finally:
        redis.delete(stream)
