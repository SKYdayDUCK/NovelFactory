"""Preview or apply an authorized, independent reading under a new model policy."""
import argparse
import json
import os
from pathlib import Path

from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.kernel import Kernel
from novel_factory.application.production_pipeline import production_material_reader
from novel_factory.application.reading_policy_recovery import (
    ReadingPolicyRecovery,
    replace_stopped_reading,
)
from novel_factory.application.supervisor import HostJournal
from novel_factory.domain.errors import DomainError
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.objects import ObjectStore
from novel_factory.infrastructure.private_files import read_private_bytes
from novel_factory.infrastructure.runtime_providers import load_runtime_profiles


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--access-file", type=Path, required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--generation", type=int, required=True)
    parser.add_argument("--board-sha256", required=True)
    parser.add_argument("--creative", default="deepseek_flash_writer")
    parser.add_argument("--review", default="codex_planner_reviewer")
    parser.add_argument("--tokens", type=int, default=500000)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    access = json.loads(read_private_bytes(args.access_file, 100000))
    wr = access.get("world_ref", access)
    db = Database()
    try:
        kernel = Kernel(db, ObjectStore(os.environ["NF_OBJECT_ROOT"]))
        gateway = AgentGateway(kernel, load_runtime_profiles(os.environ["NF_RUNTIME_PROFILES"], HostJournal(kernel)),
                               material_reader=production_material_reader(kernel))
        request = ReadingPolicyRecovery(wr["work_id"], wr["branch_id"], args.task, args.generation,
                                         args.board_sha256, args.creative, args.review, args.tokens)
        result = replace_stopped_reading(kernel, gateway, kernel.authenticate(access["token"]), request, apply=args.apply)
        print(json.dumps(result))
    finally:
        db.close()


if __name__ == "__main__":
    try:
        main()
    except DomainError as error:
        raise SystemExit(error.code) from None
    except (OSError, ValueError, KeyError):
        raise SystemExit("INVALID_DEPLOYMENT_CONFIGURATION") from None
