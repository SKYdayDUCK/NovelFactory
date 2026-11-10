"""Create an isolated acceptance fixture through the real kernel.

The generated Bearer token is written only to a private local file. This script
is an author/developer bootstrap; world initialization is not exposed to Agents.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from novel_factory.application.kernel import Kernel
from novel_factory.fixtures import load_fixture, create_bound_task, seed_sword_history, point
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.objects import ObjectStore


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", choices=["r1_sword", "white_bridge", "legacy_sword"], default="r1_sword")
    parser.add_argument("--history", action="store_true", help="Accept the sword fixture history via tasks/certificates/commits")
    parser.add_argument("--output", type=Path, required=True, help="New private local credential file; do not archive it")
    args = parser.parse_args()
    if args.output.exists():
        parser.error("credential output already exists; choose a new private path")
    if args.history and args.fixture == "white_bridge":
        parser.error("White Bridge full crossing simulation belongs to P2")
    database = Database()
    try:
        kernel = Kernel(database, ObjectStore(os.environ.get("NF_OBJECT_ROOT", "var/objects")))
        fixture = load_fixture(kernel, args.fixture)
        if args.history:
            fixture = seed_sword_history(kernel, fixture)
        at = point(25 if args.history else 1)
        task = create_bound_task(kernel, fixture["principal"], fixture["world_ref"], at,
                                 [fixture["ids"]["sword"]], ["owner", "active_holder"])
        query = {"schema_version": "2.1", "task_id": task["task_id"], "generation": task["generation"],
                 "read_contract": {"revision": {"mode": "FIXED", "world_ref": fixture["world_ref"]},
                                   "process_policy": "REQUIRE_EVALUATED", "index_policy": "AUTHORITATIVE", "max_wait_ms": 0},
                 "knowledge_cut": {"plane": "WORLD"}, "subject_time": {"kind": "POINT", "at": at},
                 "query": {"kind": "STATE", "subject_ids": [fixture["ids"]["sword"]], "predicates": ["owner", "active_holder"]},
                 "page_size": 50, "cursor": None}
        payload = {"purpose": "private_local_test_access_do_not_package", "token": fixture["token"],
                   "world_ref": fixture["world_ref"], "task_id": task["task_id"], "generation": task["generation"],
                   "worker_id": task["worker_id"], "query": query, "ids": fixture["ids"]}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        print(json.dumps({"status": "CREATED", "fixture": args.fixture, "world_ref": fixture["world_ref"],
                          "private_access_file": str(args.output.resolve()), "token_printed": False}, indent=2))
    finally:
        database.close()


if __name__ == "__main__":
    main()
