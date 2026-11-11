"""Consume one versioned EventHub group with bounded concurrency."""
import argparse
import json
import os
import socket
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from threading import local
from uuid import uuid4

from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy.exc import OperationalError, TimeoutError as PoolTimeout

from novel_factory.infrastructure.capacity import AdaptiveController
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.eventhub import EventHub
from novel_factory.orchestration.processors import PROCESSORS, handler_for


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group", choices=tuple(PROCESSORS), required=True)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--batch", type=int, default=16)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--adaptive", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.batch <= 128 or not 1 <= args.workers <= PROCESSORS[args.group].max_workers:
        parser.error("Invalid batch or worker count")
    db = Database(pool_size=args.workers + int(args.adaptive))
    client = Redis.from_url(os.environ["NF_REDIS_URL"], decode_responses=True, max_connections=args.workers + 1,
                            socket_connect_timeout=3, socket_timeout=5)
    local_worker = local()
    handler = handler_for(args.group)

    def consume():
        if not hasattr(local_worker, "hub"):
            local_worker.hub = EventHub(db, client)
            local_worker.name = socket.gethostname()[:60] + "-" + uuid4().hex
        return local_worker.hub.consume_once(args.group, local_worker.name, handler, limit=args.batch)

    controller = AdaptiveController(db, args.group, args.workers, "observability", redis=client) if args.adaptive else None
    try:
        if args.once:
            try:
                print(json.dumps({"group": args.group, "processed": consume()}))
            except (RedisError, OperationalError, PoolTimeout):
                return 1
            return
        pending = {}
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            while True:
                target = controller.limit() if controller else args.workers
                while len(pending) < target:
                    pending[pool.submit(consume)] = time.monotonic()
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
                processed = 0
                for future in done:
                    start = pending.pop(future)
                    try:
                        count = future.result()
                    except (RedisError, OperationalError, PoolTimeout):
                        if controller:
                            controller.monitor.completed(time.monotonic() - start, False)
                        continue
                    processed += count
                    if controller and count:
                        controller.monitor.completed(time.monotonic() - start)
                if not processed:
                    time.sleep(.25)
    finally:
        client.close()
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
