"""Invoke a bound NovelFactory tool once. Supply a JSON argument object on stdin."""
import argparse
import json
import sys

from novel_factory.application.bound_tools import invoke_bound_tool
from novel_factory.domain.errors import DomainError
from .bindings import read_private_config, runtime_from_config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binding", required=True, help="Private server-provisioned binding file")
    parser.add_argument("tool", help="Tool name available to this binding")
    args = parser.parse_args()
    try:
        config = read_private_config(args.binding)
        runtime = runtime_from_config(config)
        try:
            raw = sys.stdin.buffer.read(1_048_577)
            if len(raw) > 1_048_576:
                raise DomainError("PAYLOAD_TOO_LARGE", "Argument size limit exceeded")
            try:
                arguments = json.loads(raw)
            except (ValueError, UnicodeError, RecursionError):
                raise DomainError("INVALID_ARGUMENT", "Invalid JSON arguments") from None
            result = invoke_bound_tool(runtime, args.tool, arguments)
            print(json.dumps(result, allow_nan=False))
        finally:
            runtime.kernel.db.close()
    except DomainError as exc:
        print(json.dumps({"error": {"code": exc.code}}))
        return 1
    except Exception:
        print(json.dumps({"error": {"code": "SERVICE_UNAVAILABLE"}}))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
