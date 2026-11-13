"""Enable, rebuild or drop a derived progress row for one branch, as a DB operator."""
import argparse
from uuid import UUID

from novel_factory.infrastructure.database import Database
from novel_factory.orchestration.progress import check_progress, configure_progress


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "rebuild", "disable"))
    parser.add_argument("work", type=UUID)
    parser.add_argument("branch", type=UUID)
    args = parser.parse_args()
    db = Database()
    try:
        if args.action == "check":
            with db.uow(write=False) as repo:
                status = check_progress(repo.conn, args.work, args.branch)
            print(status)
            return int(status == "MISMATCH")
        with db.uow() as repo:
            configure_progress(repo.conn, args.work, args.branch, enabled=args.action == "rebuild")
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
