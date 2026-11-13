"""Authorize and inspect native model editing against a rolling volume's existing budget."""

import argparse
import json
from pathlib import Path

from novel_factory.application import frozen_editing
from novel_factory.application.kernel import require
from novel_factory.application.mcp_runtime import ProjectBinding
from novel_factory.domain.errors import DomainError
from novel_factory.interfaces.bindings import read_private_config, runtime_from_config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("enroll", "status"))
    parser.add_argument("--binding", type=Path, required=True)
    args = parser.parse_args()
    runtime = runtime_from_config(read_private_config(args.binding))
    try:
        require(
            isinstance(runtime.binding, ProjectBinding), "VIEW_DENIED", "Project binding required"
        )
        action = frozen_editing.start if args.command == "enroll" else frozen_editing.status
        return action(
            runtime.kernel, runtime.principal, runtime.binding.work_id, runtime.binding.branch_id
        )
    finally:
        runtime.kernel.db.close()


if __name__ == "__main__":
    try:
        print(json.dumps(main(), ensure_ascii=False))
    except DomainError as exc:
        print(json.dumps({"error": exc.code}))
        raise SystemExit(2) from None
