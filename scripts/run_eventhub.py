"""Operate P5.5 Redis delivery using a privileged internal service connection."""

import argparse
import json
import os
import time

from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy.exc import OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeout

from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.eventhub import EventHub


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["relay", "repair", "status", "trim", "redrive"])
    parser.add_argument("--once", action="store_true", help="run one bounded relay pass")
    parser.add_argument("--limit", type=int, default=64)
    parser.add_argument("--retention-seconds", type=int, default=86400)
    parser.add_argument("--event-id")
    parser.add_argument("--group")
    args = parser.parse_args()
    if args.operation == "redrive" and not (args.group and args.event_id):
        parser.error("redrive requires --event-id and --group")
    client = Redis.from_url(
        os.environ["NF_REDIS_URL"],
        decode_responses=True,
        max_connections=2,
        socket_connect_timeout=2,
        socket_timeout=3,
    )
    db = Database(pool_size=2)
    hub = EventHub(db, client)
    try:
        if args.operation == "status":
            result = hub.status()
        elif args.operation == "trim":
            result = {"trimmed": hub.trim_once(args.retention_seconds, args.limit)}
        elif args.operation == "repair":
            result = {"requeued": hub.repair_once(args.limit)}
        elif args.operation == "redrive":
            result = {"requeued": hub.redrive(args.event_id, args.group)}
        else:
            next_check = 0
            while True:
                try:
                    replay = 0
                    if time.monotonic() >= next_check:
                        hub.diagnostics.run("relay", client.ping, ping_only=True)
                        replay = hub.diagnostics.run(
                            "relay", lambda: hub.repair_once(args.limit), ping_only=True
                        )
                        next_check = time.monotonic() + 5
                    n = hub.relay_once(args.limit)
                    result = {"published": n, "requeued": replay}
                    if args.once:
                        break
                    if not n:
                        time.sleep(0.25)
                except (RedisError, OperationalError, PoolTimeout):
                    if args.once:
                        return 1
                    time.sleep(2)
        print(json.dumps(result, default=str))
        return 0
    except (RedisError, OperationalError, PoolTimeout):
        print('{"error":"SERVICE_UNAVAILABLE"}', flush=True)
        return 1
    finally:
        client.close()
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
