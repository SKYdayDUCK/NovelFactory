"""Preview or explicitly replace one lost review; never resend its old Attempt."""

import argparse
import json
import os
from pathlib import Path

from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.kernel import Kernel
from novel_factory.application.lost_review import LostReviewRequest, replace_lost_review
from novel_factory.application.production_pipeline import production_material_reader
from novel_factory.application.supervisor import HostJournal
from novel_factory.domain.errors import DomainError
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.objects import ObjectStore
from novel_factory.infrastructure.private_files import read_private_bytes
from novel_factory.infrastructure.runtime_providers import load_runtime_profiles


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--access", type=Path, required=True, help="Owner-private supervisor capability file")
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--generation", type=int, required=True)
    parser.add_argument("--board-sha256", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--acknowledge-unknown-cost", action="store_true")
    args = parser.parse_args()
    access = json.loads(read_private_bytes(args.access, 100_000))
    db = Database()
    try:
        kernel = Kernel(db, ObjectStore(os.environ["NF_OBJECT_ROOT"]))
        principal = kernel.authenticate(access["token"])
        gateway = AgentGateway(kernel, load_runtime_profiles(os.environ["NF_RUNTIME_PROFILES"], HostJournal(kernel)),
                               material_reader=production_material_reader(kernel))
        request = LostReviewRequest(access["work_id"], access["branch_id"], args.attempt_id,
                                    args.generation, args.board_sha256, args.acknowledge_unknown_cost)
        print(json.dumps(replace_lost_review(kernel, gateway, principal, request, apply=args.apply)))
        return 0
    except DomainError as error:
        print(json.dumps({"status": "BLOCKED", "reason": error.code}))
        return 3
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
