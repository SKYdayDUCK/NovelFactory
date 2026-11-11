#!/usr/bin/env python3
"""P3-03 offline evidence CLI. No model, world update or publication route."""
import argparse
from pathlib import Path
import sys

from novel_factory.domain.canonical import canonical_bytes, strict_json_loads
from novel_factory.domain.errors import DomainError
from novel_factory.evaluation import EvaluationStore
from novel_factory.evaluation.example import run_demo


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    demo = commands.add_parser("demo", help="Run explicit engineering fixtures, never a real model experiment")
    demo.add_argument("--output-dir", required=True)
    for name in ("init", "put", "record", "verify", "report", "checkpoint", "blind"):
        sub = commands.add_parser(name)
        sub.add_argument("--store", required=True)
        if name == "init":
            sub.add_argument("--plan", required=True)
        elif name == "put":
            sub.add_argument("--file", required=True)
            sub.add_argument("--media-type", default="application/json",
                choices=["application/json", "text/plain; charset=utf-8", "application/octet-stream"])
        elif name == "record":
            sub.add_argument("--event", required=True)
        elif name == "verify":
            sub.add_argument("--expected-head")
        elif name == "blind":
            sub.add_argument("--reviewer-dir", required=True)
            sub.add_argument("--coordinator-dir", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "demo":
            result = run_demo(args.output_dir)
        else:
            store = EvaluationStore(args.store)
            if args.command == "put":
                result = store.put(Path(args.file).read_bytes(), args.media_type)
            elif args.command == "init":
                value = store.initialize(strict_json_loads(Path(args.plan).read_bytes()))
                result = {key: value[key] for key in ("plan_sha256", "head_sha256", "event_count")}
            elif args.command == "record":
                result = store.append(strict_json_loads(Path(args.event).read_bytes()))
            elif args.command == "verify":
                value = store.snapshot(expected_head=args.expected_head)
                result = {"integrity_status": "LOCAL_HASH_CHAIN_AND_OBJECTS_VERIFIED",
                    "history_completeness": "MATCHES_SUPPLIED_CHECKPOINT" if args.expected_head else "REQUIRES_EXTERNAL_CHECKPOINT",
                    **{key: value[key] for key in ("plan_sha256", "head_sha256", "event_count")}}
            elif args.command == "report":
                result = store.report()
            elif args.command == "checkpoint":
                result = store.checkpoint()
            else:
                result = store.blind(args.reviewer_dir, args.coordinator_dir)
        print(canonical_bytes(result).decode())
        return 0
    except DomainError as exc:
        print(canonical_bytes({"status": "BLOCKED", "code": exc.code, "reason": exc.safe_message,
                               "literary_inference_status": "BLOCKED_LITERARY_INFERENCE"}).decode(), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
