"""Run the isolated six-scene TEST_FIXTURE production loop on PostgreSQL 17.

Use scripts/with_postgres.py --migrate -- ... for a disposable database. This
command creates one new work, writes no credentials, and never calls a paid
model or publishes externally. A failed run retains its partial process trace.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from novel_factory.application.kernel import Kernel
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.objects import ObjectStore
from novel_factory.orchestration.short_volume import run_short_volume


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execution-kind", choices=["TEST_FIXTURE"], required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--object-root", type=Path,
                        default=Path(os.environ.get("NF_OBJECT_ROOT", "var/short-volume-objects")))
    args = parser.parse_args()
    if args.output.exists() and any(args.output.iterdir()):
        parser.error("output must be new or empty, so earlier evidence is preserved")
    db = Database()
    try:
        result = run_short_volume(Kernel(db, ObjectStore(args.object_root)), output_dir=args.output)
        print(json.dumps({key: result[key] for key in ("title", "execution_kind", "engineering_status",
            "real_model_status", "author_status", "scene_count", "text_codepoints", "model_fixture_call_count",
            "real_model_call_count", "world_events_before_writing", "world_events_after_release")}, ensure_ascii=False, indent=2))
    finally:
        db.close()


if __name__ == "__main__":
    main()
