"""Run a bounded trusted local derived-index worker; no external side effects."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from novel_factory.application.derived_worker import DerivedWorldWorker
from novel_factory.application.kernel import Kernel
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.objects import ObjectStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--objects", required=True, type=Path)
    parser.add_argument("--worker-id", default="derived-local")
    parser.add_argument("--max-messages", type=int, default=100)
    args = parser.parse_args()
    if not 1 <= args.max_messages <= 10000:
        parser.error("max-messages must be between 1 and 10000")
    db = Database()
    worker = DerivedWorldWorker(Kernel(db, ObjectStore(args.objects)))
    try:
        for _ in range(args.max_messages):
            result = worker.run_once(args.worker_id)
            if result is None:
                break
            print(json.dumps(result, ensure_ascii=False), flush=True)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
