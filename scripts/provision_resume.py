"""Provision one private, revocable operator binding using an existing administrator."""
import argparse
import json
import os
from datetime import UTC, datetime, timedelta
from getpass import getpass
from pathlib import Path

from novel_factory.application.kernel import Kernel
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.objects import ObjectStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-id", required=True)
    parser.add_argument("--branch-id", required=True)
    parser.add_argument("--object-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--hours", type=int, required=True)
    args = parser.parse_args()
    if args.hours <= 0:
        parser.error("hours must be positive")
    kernel = Kernel(Database(), ObjectStore(args.object_root))
    admin = kernel.authenticate(getpass("Existing project administrator token: "))
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as out:
            binding = {"work_id": args.work_id, "branch_id": args.branch_id}
            granted = kernel.issue_grant(admin, args.work_id, role_id="resume_operator",
                       capabilities=["PROJECT_RESUME"], branch_ids=[args.branch_id],
                       expires_at=datetime.now(UTC) + timedelta(hours=args.hours))
            json.dump({"binding": binding, "token": granted["token"],
                       "database_url": os.environ["NF_DATABASE_URL"],
                       "object_root": str(args.object_root.resolve())}, out)
            out.flush()
            os.fsync(out.fileno())
        print("Resume grant", granted["principal"].grant_id)
    except BaseException:
        if "granted" in locals():
            kernel.revoke_grant(admin, granted["principal"].grant_id)
        args.output.unlink(missing_ok=True)
        raise
    finally:
        kernel.db.close()


if __name__ == "__main__":
    main()
