"""CLI for the P1 logical backup service; restore only to an empty isolated DB."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from novel_factory.infrastructure.database import Database, tables
from novel_factory.infrastructure.objects import ObjectStore
from novel_factory.infrastructure.recovery import BackupService


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["create", "verify", "restore"])
    parser.add_argument("path", type=Path, help="New backup directory or existing backup to verify/restore")
    parser.add_argument("--restore-object-root", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.operation == "restore" and (not args.restore_object_root or not os.environ.get("NF_RESTORE_DATABASE_URL")):
        parser.error("restore requires NF_RESTORE_DATABASE_URL and --restore-object-root; target must be empty")
    database = Database()
    try:
        service = BackupService(database.engine, ObjectStore(os.environ.get("NF_OBJECT_ROOT", "var/objects")))
        if args.operation == "create":
            result = service.create(args.path)
        elif args.operation == "verify":
            result = service.verify(args.path)
        else:
            result = service.restore(args.path, os.environ["NF_RESTORE_DATABASE_URL"],
                                      args.restore_object_root, tables=tables)
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({"operation": args.operation, "status": "PASS", "backup_id": result.get("backup_id"),
                          "objects": len(result.get("objects", {})) if "objects" in result else result.get("object_count"),
                          "tables_verified": result.get("tables_verified"),
                          "snapshot_equal_before_fencing": result.get("snapshot_equal_before_fencing")}, indent=2))
    finally:
        database.close()


if __name__ == "__main__":
    main()
