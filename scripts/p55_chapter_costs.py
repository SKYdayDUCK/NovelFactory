"""Pin a scoped chapter/price document or report costs with its exact ArtifactRef."""

import argparse
import json
import sys
from pathlib import Path

from novel_factory.application.cost_reports import ChapterCostService
from novel_factory.application.mcp_runtime import ProjectBinding
from novel_factory.domain.canonical import strict_json_loads
from novel_factory.domain.errors import DomainError
from novel_factory.interfaces.mcp_stdio import read_private_config, runtime_from_config


def document(path):
    with path.open("rb") as stream:
        data = stream.read(2_000_001)
    if len(data) > 2_000_000:
        raise ValueError
    return strict_json_loads(data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binding", type=Path, required=True)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("pin").add_argument("document", type=Path)
    report = sub.add_parser("report")
    report.add_argument("chapter_ref", type=Path)
    report.add_argument("--price-ref", type=Path)
    report.add_argument("--source-cut")
    args = parser.parse_args()
    runtime = runtime_from_config(read_private_config(args.binding))
    try:
        if not isinstance(runtime.binding, ProjectBinding):
            parser.error("a project binding is required")
        service = ChapterCostService(runtime.kernel)
        scope = (runtime.principal, runtime.binding.work_id, runtime.binding.branch_id)
        if args.action == "pin":
            result = service.pin(*scope, document(args.document))
        else:
            result = service.report(
                *scope,
                document(args.chapter_ref),
                document(args.price_ref) if args.price_ref else None,
                source_cut=args.source_cut,
            )
        print(json.dumps(result, ensure_ascii=False, default=str))
    finally:
        runtime.kernel.db.close()


if __name__ == "__main__":
    try:
        main()
    except DomainError as error:
        sys.exit(error.code)
    except (OSError, ValueError):
        sys.exit("INVALID_CONFIGURATION")
    except Exception:  # noqa: BLE001 -- drivers may include private connection attributes.
        sys.exit("SERVICE_UNAVAILABLE")
