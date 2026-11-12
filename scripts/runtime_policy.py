"""Activate, preview or rebind a model route under an existing project administrator."""
import argparse
import json
import os
from pathlib import Path

from novel_factory.application import model_routing
from novel_factory.application.kernel import Kernel
from novel_factory.application.supervisor import HostJournal
from novel_factory.domain.errors import DomainError
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.objects import ObjectStore
from novel_factory.infrastructure.runtime_providers import load_runtime_profiles


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--access-file", type=Path, required=True)
    actions = parser.add_subparsers(dest="action", required=True)
    activate = actions.add_parser("activate")
    activate.add_argument("--policy", type=Path, required=True)
    activate.add_argument("--expected-revision", type=int, required=True)
    preview = actions.add_parser("preview")
    preview.add_argument("--role", required=True)
    preview.add_argument("--kind", required=True)
    preview.add_argument("--instance")
    preview.add_argument("--task")
    preview.add_argument("--override")
    rebind = actions.add_parser("rebind")
    rebind.add_argument("--task", required=True)
    rebind.add_argument("--generation", required=True, type=int)
    rebind.add_argument("--key", required=True)
    args = parser.parse_args()
    access = json.loads(args.access_file.read_text())
    db = Database()
    try:
        kernel = Kernel(db, ObjectStore(os.environ.get("NF_OBJECT_ROOT", "var/objects")))
        owner = kernel.authenticate(access["token"])
        with db.uow(write=False) as repo:
            kernel.authorize(repo, owner, access["work_id"], "ADMIN", access["branch_id"])
            wr = kernel.head(repo, access["work_id"], access["branch_id"])
        adapters = load_runtime_profiles(os.environ["NF_RUNTIME_PROFILES"], HostJournal(kernel))
        if args.action == "activate":
            result = model_routing.activate(kernel, owner, wr, json.loads(args.policy.read_text()), adapters,
                                            expected_revision=args.expected_revision)
        elif args.action == "rebind":
            result = model_routing.rebind(kernel, owner, wr, args.task, args.generation, adapters, args.key)
        else:
            actor = {"role_id": args.role, "task_kind": args.kind, "instance_id": args.instance,
                     "task_id": args.task}
            result = model_routing.preview(kernel, owner, wr, actor, adapters, override=args.override)
        print(json.dumps(result, indent=2))
    finally:
        db.close()


if __name__ == "__main__":
    try:
        main()
    except DomainError as exc:
        raise SystemExit(exc.code) from None
    except (KeyError, OSError, ValueError):
        raise SystemExit("INVALID_CONFIGURATION") from None
