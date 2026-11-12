"""Lose an entire disposable Redis deployment and republish both PG outboxes."""

import json
import os
import subprocess
import time
from contextlib import closing, contextmanager

from agent_workshop.auth import connect
from agent_workshop.notifications import notify_batch, replay
from redis import Redis
from redis.exceptions import ConnectionError as RedisConnectionError

from novel_factory.infrastructure.event_streams import STREAMS
from novel_factory.infrastructure.eventhub import EventHub
from scripts.pitr_drill import _port
from scripts.pitr_streams import StreamsDrill


@contextmanager
def private_redis(directory):
    image = os.environ.get("AW_W3_REDIS_IMAGE")
    if not image:
        with closing(StreamsDrill(directory, _port())) as instance:
            yield instance.client
        return
    container = subprocess.check_output(
        [
            "docker",
            "run",
            "--detach",
            "--rm",
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
        address = subprocess.check_output(["docker", "port", container, "6379/tcp"], text=True)
        port = int(address.strip().rsplit(":", 1)[1])
        with Redis(host="127.0.0.1", port=port, decode_responses=True, socket_timeout=2) as client:
            for _ in range(100):
                try:
                    if client.ping():
                        break
                except RedisConnectionError:
                    time.sleep(0.05)
            else:
                raise RuntimeError("Disposable Redis did not start")
            yield client
    finally:
        subprocess.run(["docker", "stop", "--time", "2", container], check=True)


def rebuild_outboxes(directory, aw_dsn, nf_db, org, message_id, task_id):
    stream = "agent-workshop:notifications:v1"
    with connect(aw_dsn) as conn:
        row = conn.execute(
            "SELECT event_id FROM aw_outbox WHERE org_id=%s AND message_id=%s", (org, message_id)
        ).fetchone()
        aw_event = str(row["event_id"])
    receipts = []
    for recovering in (False, True):
        with private_redis(directory) as redis:
            assert redis.dbsize() == 0
            hub = EventHub(nf_db, redis)
            if recovering:
                assert replay(aw_dsn) > 0
                assert hub.repair_once(limit=1000) > 0
            StreamsDrill.drain(lambda: notify_batch(aw_dsn, redis, stream))
            StreamsDrill.drain(hub.relay_once)
            aw_ids = [fields["event_id"] for _, fields in redis.xrange(stream)]
            nf_events = [
                json.loads(fields["envelope"])
                for key in STREAMS.values()
                for _, fields in redis.xrange(key)
            ]
            task_events = sorted(
                event["event_id"] for event in nf_events if event["task_id"] == task_id
            )
            assert aw_ids.count(aw_event) == 1 and task_events
            info = redis.info("server")
            receipts.append(
                {
                    "run_id": info["run_id"],
                    "redis_version": info["redis_version"],
                    "initial_keys": 0,
                    "aw_event_id": aw_event,
                    "nf_task_events": task_events,
                }
            )
    before, after = receipts
    assert before["run_id"] != after["run_id"]
    assert before["nf_task_events"] == after["nf_task_events"]
    return {"before_loss": before, "after_rebuild": after}
