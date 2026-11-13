"""Publish a signed rolling final volume through the native release service."""

import argparse
import json
import sys
from pathlib import Path

from novel_factory.application.kernel import require
from novel_factory.application.mcp_runtime import ProjectBinding
from novel_factory.application.release import ReleaseService
from novel_factory.application.rolling_publication import final_review_request
from novel_factory.domain.canonical import fingerprint
from novel_factory.domain.errors import DomainError
from novel_factory.interfaces.bindings import read_private_config, runtime_from_config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binding", required=True, type=Path)
    args = parser.parse_args()
    runtime = runtime_from_config(read_private_config(args.binding))
    try:
        scope = runtime.binding
        require(isinstance(scope, ProjectBinding), "VIEW_DENIED", "A project binding is required")
        request = final_review_request(
            runtime.kernel, runtime.principal, scope.work_id, scope.branch_id
        )
        receipt = ReleaseService(runtime.kernel).freeze(
            runtime.principal, request, "p6-final:" + fingerprint(request)
        )
        print(json.dumps({k: receipt[k] for k in ("release_ref", "manifest_ref")}))
    finally:
        runtime.kernel.db.close()


if __name__ == "__main__":
    try:
        main()
    except DomainError as exc:
        sys.exit(exc.code)
    except (ValueError, OSError):
        sys.exit("INVALID_INPUT")
    except Exception:  # noqa: BLE001 -- connection diagnostics may contain project credentials.
        sys.exit("SERVICE_UNAVAILABLE")
