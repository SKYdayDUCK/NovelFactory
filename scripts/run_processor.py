"""Consume one versioned EventHub group. Run multiple bounded service processes as needed."""
import argparse
import json
import logging
import os
import socket
import time
from uuid import uuid4

from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy.exc import OperationalError

from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.eventhub import EventHub
from novel_factory.orchestration.processors import PROCESSORS, handler_for


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group", choices=tuple(PROCESSORS), required=True)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--batch", type=int, default=16)
    args = parser.parse_args()
    if not 1 <= args.batch <= 128:
        parser.error("batch must be between 1 and 128")
    db = Database(pool_size=1)
    client = Redis.from_url(os.environ["NF_REDIS_URL"], decode_responses=True, max_connections=2,
                            socket_connect_timeout=3, socket_timeout=5)
    hub = EventHub(db, client)
    name = f"{socket.gethostname()}-{uuid4().hex}"
    try:
        while True:
            try:
                count = hub.consume_once(args.group, name, handler_for(args.group), limit=args.batch)
            except (RedisError, OperationalError):
                logging.warning("Processor temporarily unavailable", extra={"group": args.group})
                if args.once:
                    raise SystemExit(1) from None
                time.sleep(1)
                continue
            if args.once:
                print(json.dumps({"group": args.group, "processed": count}))
                break
            if not count:
                time.sleep(.25)
    finally:
        client.close()
        db.close()


if __name__ == "__main__":
    main()
