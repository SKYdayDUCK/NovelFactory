"""Export scoped transport overlap, model usage and settled amounts without calling any provider."""

import argparse
import json
from pathlib import Path
from uuid import UUID

from novel_factory.application.mcp_runtime import ProjectBinding
from novel_factory.application.provider_evidence import inspect
from novel_factory.domain.errors import DomainError
from novel_factory.interfaces.mcp_stdio import read_private_config, runtime_from_config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binding", type=Path, required=True)
    parser.add_argument("--attempt", type=UUID, action="append", required=True)
    args = parser.parse_args()
    runtime = runtime_from_config(read_private_config(args.binding))
    try:
        scope = runtime.binding
        if not isinstance(scope, ProjectBinding):
            parser.error("A project binding is required")
        result = inspect(
            runtime.kernel,
            runtime.principal,
            scope.work_id,
            scope.branch_id,
            [str(item) for item in args.attempt],
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        runtime.kernel.db.close()


if __name__ == "__main__":
    try:
        main()
    except DomainError as exc:
        raise SystemExit(exc.code) from None
