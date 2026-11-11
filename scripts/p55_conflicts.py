"""List recent scoped conflict cases or submit an independent review decision."""
import argparse
import json
from pathlib import Path

from novel_factory.application.audit_log import LogService
from novel_factory.application.conflict_resolution import ConflictResolutionService
from novel_factory.application.mcp_runtime import ProjectBinding
from novel_factory.interfaces.mcp_stdio import read_private_config, runtime_from_config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binding", required=True, type=Path)
    commands = parser.add_subparsers(dest="operation", required=True)
    commands.add_parser("list").add_argument("--limit", type=int, default=30)
    commands.add_parser("decide").add_argument("--request", required=True, type=Path)
    args = parser.parse_args()
    runtime = runtime_from_config(read_private_config(args.binding))
    try:
        binding = runtime.binding
        if not isinstance(binding, ProjectBinding):
            parser.error("A work and branch binding is required")
        if args.operation == "list":
            result = LogService.recent_conflicts(runtime.kernel, runtime.principal,
                binding.work_id, binding.branch_id, limit=args.limit)
        else:
            request = json.loads(args.request.read_text(encoding="utf-8"))
            if not isinstance(request, dict) or not {
                "expected_head", "case_id", "decision", "evidence_ref"
            } <= request.keys() <= {"expected_head", "case_id", "decision", "evidence_ref", "interaction"}:
                parser.error("Review request fields do not match the decision contract")
            head = request["expected_head"]
            if not isinstance(head, dict) or (head.get("work_id"), head.get("branch_id")) != (
                binding.work_id, binding.branch_id):
                parser.error("Decision is outside the configured binding")
            result = ConflictResolutionService(runtime.kernel).decide_review(runtime.principal, **request)
        print(json.dumps(result, ensure_ascii=False, default=str, indent=2))
    finally:
        runtime.kernel.db.close()


if __name__ == "__main__":
    main()
