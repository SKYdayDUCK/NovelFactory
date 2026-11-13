"""Enable, rebuild or drop a derived progress row for one branch, as a DB operator."""
import argparse
from uuid import UUID

from novel_factory.infrastructure.database import Database
from novel_factory.orchestration.progress import (
    check_progress,
    configure_progress,
    set_progress_maintenance,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "rebuild", "disable", "suspend-maintenance", "resume-maintenance"))
    parser.add_argument("work", type=UUID, nargs="?")
    parser.add_argument("branch", type=UUID, nargs="?")
    args = parser.parse_args()
    global_action = args.action in ("suspend-maintenance", "resume-maintenance")
    if global_action and (args.work or args.branch):
        parser.error("Maintenance is a database-wide operation and takes no branch arguments")
    if not global_action and (args.work is None or args.branch is None):
        parser.error("Branch operations require both work and branch UUIDs")
    db = Database()
    try:
        if global_action:
            with db.uow() as repo:
                set_progress_maintenance(repo.conn, suspended=args.action == "suspend-maintenance")
            return 0
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
