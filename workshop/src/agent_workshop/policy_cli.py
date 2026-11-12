"""Manage Workshop model policies with a manager's private MCP binding."""
import argparse
import json
from pathlib import Path

from .auth import Denied, private_binding
from .server import dispatch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binding", type=Path, required=True)
    commands = parser.add_subparsers(dest="action", required=True)
    publish, get = commands.add_parser("publish"), commands.add_parser("get")
    for command in (publish, get):
        command.add_argument("--work", required=True)
        command.add_argument("--branch", required=True)
    publish.add_argument("--rules", type=Path, required=True)
    publish.add_argument("--expected-revision", type=int, required=True)
    args = parser.parse_args()
    request = {"work_id": args.work, "branch_id": args.branch}
    if args.action == "publish":
        request.update(rules=json.loads(args.rules.read_text()), expected_revision=args.expected_revision)
    print(json.dumps(dispatch(private_binding(args.binding), "aw_model_policy_" + args.action, request),
                     indent=2, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Denied as exc:
        raise SystemExit(str(exc)) from None
