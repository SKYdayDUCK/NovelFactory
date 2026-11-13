"""Export content-free NovelFactory runtime evidence for one authorized work/branch."""

import argparse
import json
import sys
from pathlib import Path

from novel_factory.application.mcp_runtime import ProjectBinding
from novel_factory.application.runtime_evidence import export_runtime_evidence
from novel_factory.domain.errors import DomainError
from novel_factory.infrastructure.native_files import private_export
from novel_factory.interfaces.mcp_stdio import read_private_config, runtime_from_config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--binding", type=Path, required=True, help="private branch administrator project binding"
    )
    parser.add_argument(
        "--output", type=Path, required=True, help="new JSON Lines file in an existing directory"
    )
    args = parser.parse_args()
    runtime = runtime_from_config(read_private_config(args.binding))
    try:
        scope = runtime.binding
        if not isinstance(scope, ProjectBinding):
            raise DomainError("VIEW_DENIED", "A project binding is required")
        with private_export(args.output) as pending:
            result = export_runtime_evidence(
                runtime.kernel, runtime.principal, scope.work_id, scope.branch_id, pending
            )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    finally:
        runtime.kernel.db.close()


if __name__ == "__main__":
    try:
        main()
    except DomainError as error:
        sys.exit(error.code)
    except FileExistsError:
        sys.exit("OUTPUT_EXISTS")
    except (OSError, ValueError):
        sys.exit("INVALID_INPUT_OR_OUTPUT")
    except Exception:  # noqa: BLE001 -- database exceptions can contain private connection data.
        sys.exit("SERVICE_UNAVAILABLE")
