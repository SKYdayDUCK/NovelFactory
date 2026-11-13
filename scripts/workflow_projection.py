"""Enable, rebuild or drop a derived progress row for one branch, as a DB operator."""
import argparse
from uuid import UUID

from novel_factory.infrastructure.database import Database
from novel_factory.orchestration.progress import configure_progress


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("rebuild", "disable"))
    parser.add_argument("work", type=UUID)
    parser.add_argument("branch", type=UUID)
    args = parser.parse_args()
    db = Database()
    try:
        with db.uow() as repo:
            configure_progress(repo.conn, args.work, args.branch, enabled=args.action == "rebuild")
    finally:
        db.close()


if __name__ == "__main__":
    main()
