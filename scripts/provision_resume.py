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
from novel_factory.infrastructure.private_files import create_private_file, read_private_bytes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-id", required=True)
    parser.add_argument("--branch-id", required=True)
    parser.add_argument("--object-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--hours", type=int, required=True)
    parser.add_argument("--admin-binding", type=Path, help="Read the existing private control binding for this exact project")
    args = parser.parse_args()
    if args.hours <= 0:
        parser.error("hours must be positive")
    kernel = Kernel(Database(), ObjectStore(args.object_root))
    binding = {"work_id": args.work_id, "branch_id": args.branch_id}
    try:
        if args.admin_binding:
            config = json.loads(read_private_bytes(args.admin_binding, 16384))
            if (config["binding"] != binding or Path(config["object_root"]).resolve() != kernel.objects.root
                    or config["database_url"] != kernel.db.engine.url.render_as_string(hide_password=False)):
                parser.error("Administrator binding must match the database, object root and project scope")
            token = config["token"]
        else:
            token = getpass("Existing project administrator token: ")
        admin = kernel.authenticate(token)
        fd = create_private_file(args.output)
    except BaseException:
        kernel.db.close()
        raise
    try:
        with os.fdopen(fd, "w") as out:
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
