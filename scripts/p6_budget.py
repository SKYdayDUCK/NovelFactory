"""Check or set a shared Token budget through a work-authorized NF project binding."""

import argparse
import json
import sys
from pathlib import Path
from uuid import UUID

from novel_factory.application.budget_limits import SharedBudgetService
from novel_factory.application.mcp_runtime import ProjectBinding
from novel_factory.domain.errors import DomainError
from novel_factory.interfaces.mcp_stdio import read_private_config, runtime_from_config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binding", type=Path, required=True)
    commands = parser.add_subparsers(dest="mode", required=True)
    show, edit = commands.add_parser("show"), commands.add_parser("set")
    for command in (show, edit):
        command.add_argument("--budget-id", type=UUID, required=True)
    edit.add_argument("--total-tokens", type=int, required=True)
    edit.add_argument("--expected-total-tokens", type=int)
    args = parser.parse_args()
    runtime = runtime_from_config(read_private_config(args.binding))
    try:
        if not isinstance(runtime.binding, ProjectBinding):
            parser.error("A work-authorized project binding is required")
        scope = (
            runtime.principal,
            runtime.binding.work_id,
            runtime.binding.branch_id,
            str(args.budget_id),
        )
        service = SharedBudgetService(runtime.kernel)
        result = (
            service.status(*scope)
            if args.mode == "show"
            else service.set_limit(
                *scope, args.total_tokens, expected_total_tokens=args.expected_total_tokens
            )
        )
        print(json.dumps(result))
    finally:
        runtime.kernel.db.close()


if __name__ == "__main__":
    try:
        main()
    except DomainError as error:
        sys.exit(error.code)
    except (ValueError, OSError):
        sys.exit("INVALID_CONFIGURATION")
    except Exception:
        sys.exit("SERVICE_UNAVAILABLE")
