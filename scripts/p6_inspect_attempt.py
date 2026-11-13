"""Inspect private local evidence for one attempt without a supplier call or state transition."""

import argparse
import json
import sys
from pathlib import Path
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from novel_factory.application.kernel import require
from novel_factory.application.mcp_runtime import ProjectBinding
from novel_factory.application.provider_evidence import inspect_wire_receipt
from novel_factory.domain.errors import DomainError
from novel_factory.interfaces.bindings import read_private_config, runtime_from_config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binding", type=Path, required=True)
    parser.add_argument("--attempt", type=UUID, required=True)
    args = parser.parse_args()
    runtime = runtime_from_config(read_private_config(args.binding))
    try:
        scope = runtime.binding
        require(isinstance(scope, ProjectBinding), "VIEW_DENIED", "A project binding is required")
        result = inspect_wire_receipt(
            runtime.kernel, runtime.principal, scope.work_id, scope.branch_id, str(args.attempt)
        )
        print(json.dumps(result, ensure_ascii=False))
    finally:
        runtime.kernel.db.close()


if __name__ == "__main__":
    try:
        main()
    except DomainError as error:
        sys.exit(error.code)
    except (KeyError, ValueError, OSError):
        sys.exit("INVALID_INPUT_OR_OUTPUT")
    except SQLAlchemyError:
        sys.exit("SERVICE_UNAVAILABLE")
