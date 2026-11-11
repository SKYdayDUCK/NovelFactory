"""Inspect a scoped trace or rebuild and verify an authorized historical projection."""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from uuid import UUID

from novel_factory.application.audit_log import LogService
from novel_factory.application.mcp_runtime import ProjectBinding
from novel_factory.application.statistics import CURRENT_VERSION, StatisticsService
from novel_factory.domain.errors import DomainError
from novel_factory.interfaces.mcp_stdio import read_private_config, runtime_from_config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binding", required=True, type=Path)
    parser.add_argument(
        "operation",
        choices=[
            "read",
            "rebuild",
            "verify",
            "current",
            "literature",
            "index-literature",
            "trace",
            "import-legacy",
            "register-experiment",
        ],
    )
    parser.add_argument("--version", choices=[1, 2, 3, 4], type=int)
    parser.add_argument("--source-cut")
    parser.add_argument("--chapter-bindings", type=Path)
    parser.add_argument("--trace-id", type=UUID)
    parser.add_argument("--volume-id", type=UUID)
    parser.add_argument("--actor-id", type=UUID)
    parser.add_argument("--experiment-id", type=UUID)
    parser.add_argument("--role-id")
    parser.add_argument("--model")
    parser.add_argument("--start", type=datetime.fromisoformat)
    parser.add_argument("--end", type=datetime.fromisoformat)
    args = parser.parse_args()
    runtime = runtime_from_config(read_private_config(args.binding))
    try:
        if not isinstance(runtime.binding, ProjectBinding):
            parser.error("a project-scoped statistics operator binding is required")
        bound = [runtime.principal, runtime.binding.work_id, runtime.binding.branch_id]
        service = StatisticsService(runtime.kernel)
        if args.operation == "trace":
            if not args.trace_id:
                parser.error("--trace-id is required")
            result = LogService.trace(runtime.kernel, *bound, args.trace_id)
        elif args.operation in {"literature", "index-literature"}:
            from novel_factory.application.literary_statistics import LiteraryStatisticsService
            from novel_factory.domain.canonical import strict_json_loads

            reports = LiteraryStatisticsService(runtime.kernel)
            if args.operation == "index-literature":
                result = reports.index_history(*bound)
            else:
                bindings = None
                if args.chapter_bindings:
                    with args.chapter_bindings.open("rb") as stream:
                        raw = stream.read(2_000_001)
                    if len(raw) > 2_000_000:
                        parser.error("chapter bindings exceed the size limit")
                    bindings = strict_json_loads(raw)
                result = reports.report(
                    *bound, chapters=bindings, source_cut=args.source_cut, version=args.version or 1
                )
        elif args.operation == "register-experiment":
            if not (args.experiment_id and args.start and args.end):
                parser.error("register-experiment requires --experiment-id, --start, --end")
            result = service.register_experiment(*bound, args.experiment_id, args.start, args.end)
        elif args.operation == "import-legacy":
            result = service.import_legacy(*bound)
        elif args.operation == "current":
            result = service.current(*bound)
        else:
            filters = {
                key: value
                for key in (
                    "trace_id",
                    "volume_id",
                    "actor_id",
                    "experiment_id",
                    "role_id",
                    "model",
                )
                if (value := getattr(args, key)) is not None
            }
            method = {
                "read": service.read,
                "rebuild": service.rebuild,
                "verify": service.verify_statistics,
            }[args.operation]
            result = method(
                *bound,
                version=args.version or CURRENT_VERSION,
                source_cut=args.source_cut,
                filters=filters,
                start=args.start,
                end=args.end,
            )
        print(json.dumps(result, ensure_ascii=False, default=str))
    finally:
        runtime.kernel.db.close()


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, DomainError) as error:
        sys.exit(error.code if isinstance(error, DomainError) else "INVALID_CONFIGURATION")
    except Exception:  # noqa: BLE001 -- drivers can expose credentials in messages.
        sys.exit("SERVICE_UNAVAILABLE")
