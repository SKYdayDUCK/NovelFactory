"""Bounded Redis Streams hint relay. PostgreSQL mailboxes remain independently readable."""

import argparse
import os
from itertools import batched
from uuid import UUID

from .auth import connect
from .messaging import accept_notification


def notify_batch(dsn, redis, stream, limit=100, *, grouped=False):
    with connect(dsn) as conn:
        rows = conn.execute(
            """SELECT event_id FROM aw_outbox WHERE last_notified IS NULL
                               ORDER BY event_id LIMIT %s""",
            (limit,),
        ).fetchall()
    if grouped:
        for chunk in batched(rows, 32):
            ids = [row["event_id"] for row in chunk]
            with redis.pipeline(transaction=False) as pipe:
                for event in ids:
                    pipe.xadd(stream, {"event_id": str(event)})
                pipe.execute()
            # A lost reply leaves these rows eligible for replay. The subscriber's
            # transactional inbox absorbs repeated hints from any publisher.
            with connect(dsn) as conn:
                conn.execute("UPDATE aw_outbox SET last_notified=clock_timestamp() "
                             "WHERE event_id=ANY(%s) AND last_notified IS NULL", (ids,))
        return len(rows)
    for row in rows:
        event = str(row["event_id"])
        redis.xadd(stream, {"event_id": event})
        with connect(dsn) as conn:
            conn.execute(
                "UPDATE aw_outbox SET last_notified=clock_timestamp() WHERE event_id=%s", (event,)
            )
    return len(rows)


def replay(dsn):
    """Operator recovery after stream loss. Duplicate hints are safe for existing subscribers."""
    with connect(dsn) as conn:
        return conn.execute(
            "UPDATE aw_outbox SET last_notified=NULL WHERE last_notified IS NOT NULL"
        ).rowcount


def receive_batch(dsn, redis, stream, subscriber, worker, limit=100, idle_ms=30000, *, grouped=False):
    from redis.exceptions import ResponseError

    try:
        redis.xgroup_create(stream, subscriber, id="0", mkstream=True)
    except ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise
    _, claimed, *_ = redis.xautoclaim(stream, subscriber, worker, idle_ms, "0-0", count=limit)
    items = list(claimed)
    if len(items) < limit:
        groups = redis.xreadgroup(subscriber, worker, {stream: ">"}, count=limit - len(items))
        items.extend(pair for _, entries in groups for pair in entries)
    accepted = 0
    for chunk in batched(items, 32 if grouped else 1):
        events = []
        for _, fields in chunk:
            try:
                events.append(UUID(fields.get("event_id", "")))
            except (TypeError, ValueError, AttributeError):
                continue
        if events:
            with connect(dsn) as conn:
                for event in events:
                    accepted += int(accept_notification(conn, subscriber, event))
        # ACK only after the entire chunk's inbox transaction committed.
        redis.xack(stream, subscriber, *(redis_id for redis_id, _ in chunk))
    return accepted


def main():
    from redis import Redis

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["publish", "receive", "replay"])
    parser.add_argument("--stream", default="agent-workshop:notifications:v1")
    parser.add_argument("--subscriber", default="workshop-inbox")
    parser.add_argument("--worker", default="workshop-worker")
    parser.add_argument("--grouped", action="store_true",
                        help="Share SQL commits and Redis round-trips across up to 32 ready notifications")
    args = parser.parse_args()
    dsn = os.environ["AW_DATABASE_URL"]
    if args.action == "replay":
        print(replay(dsn))
        return
    redis = Redis.from_url(os.environ["AW_REDIS_URL"], decode_responses=True, socket_timeout=5)
    if args.action == "publish":
        print(notify_batch(dsn, redis, args.stream, grouped=args.grouped))
    else:
        print(receive_batch(dsn, redis, args.stream, args.subscriber, args.worker, grouped=args.grouped))


if __name__ == "__main__":
    main()
