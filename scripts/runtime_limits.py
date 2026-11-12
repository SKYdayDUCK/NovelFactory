"""Set installation-wide capacity as a trusted database operator, outside role sandboxes."""
import argparse
from sqlalchemy import text

from novel_factory.infrastructure.database import Database


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    cap = actions.add_parser("limit")
    cap.add_argument("scope", choices=("organization", "work", "role", "account", "provider"))
    cap.add_argument("subject")
    cap.add_argument("--slots", type=int, required=True)
    cap.add_argument("--rpm", type=int)
    cap.add_argument("--tpm", type=int)
    weight = actions.add_parser("weight")
    weight.add_argument("work")
    weight.add_argument("weight", type=int, choices=range(1, 101))
    org = actions.add_parser("org-weight")
    org.add_argument("organization")
    org.add_argument("weight", type=int, choices=range(1, 101))
    args = parser.parse_args()
    db = Database()
    try:
        with db.uow() as repo:
            if args.action == "limit":
                if min(v for v in (args.slots, args.rpm, args.tpm) if v is not None) < 0:
                    parser.error("Limits must be nonnegative")
                repo.conn.execute(text("""
                    INSERT INTO nf_runtime_limits(scope,subject,slots,rpm,tpm)
                    VALUES (:scope,:subject,:slots,:rpm,:tpm) ON CONFLICT(scope,subject)
                    DO UPDATE SET slots=EXCLUDED.slots, rpm=EXCLUDED.rpm, tpm=EXCLUDED.tpm
                """), vars(args))
            elif args.action == "org-weight":
                repo.conn.execute(text("""
                    INSERT INTO nf_runtime_org_share(organization,weight) VALUES (:organization,:weight)
                    ON CONFLICT(organization) DO UPDATE SET weight=EXCLUDED.weight
                """), vars(args))
            else:
                repo.conn.execute(text("""
                    INSERT INTO nf_runtime_work_share(work_id,weight) VALUES (:work,:weight)
                    ON CONFLICT(work_id) DO UPDATE SET weight=EXCLUDED.weight
                """), vars(args))
    finally:
        db.close()


if __name__ == "__main__":
    main()
