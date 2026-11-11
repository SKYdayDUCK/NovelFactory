"""Show deployment infrastructure incidents and scaling decisions using operator PG credentials."""

import argparse
import json
from datetime import datetime

from sqlalchemy.exc import SQLAlchemyError

from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.diagnostics import inspect


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=100, choices=range(1, 1001), metavar="1..1000")
    parser.add_argument(
        "--component",
        choices=["relay", "model", "world", "compute", "recovery", "log", "statistics"],
    )
    parser.add_argument(
        "--since", type=datetime.fromisoformat, help="ISO-8601 time with UTC offset"
    )
    args = parser.parse_args()
    if args.since and args.since.tzinfo is None:
        parser.error("--since requires a UTC offset")
    db = Database(pool_size=1)
    try:
        print(
            json.dumps(
                inspect(db, args.limit, component=args.component, since=args.since),
                default=str,
                ensure_ascii=False,
            )
        )
    except SQLAlchemyError:
        print('{"error":"DATABASE_QUERY_UNAVAILABLE"}')
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
