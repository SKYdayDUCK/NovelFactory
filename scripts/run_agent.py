"""Dispatch one prepared, leased runtime task through its authorized model route.

The private access file contains a Bearer token; it is never printed. Prepare
and compile the fixed task through the application services first. This command
does not create grants, enlarge budgets, approve content, or accept world facts.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.kernel import Kernel
from novel_factory.domain.errors import DomainError
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.model_gateway import OpenAICompatibleAdapter
from novel_factory.infrastructure.objects import ObjectStore


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--access-file", type=Path, required=True, help="Private JSON file containing token")
    parser.add_argument("--request", type=Path, required=True, help="Exact AgentDispatchRequest JSON")
    parser.add_argument("--adapter-id", default="external", help="Controller-configured route fixed in the task input")
    args = parser.parse_args()
    database = None
    try:
        adapter = OpenAICompatibleAdapter.from_environment()
        database = Database()
        kernel = Kernel(database, ObjectStore(os.environ.get("NF_OBJECT_ROOT", "var/objects")))
        access = json.loads(args.access_file.read_text(encoding="utf-8"))
        principal = kernel.authenticate(access["token"])
        request = json.loads(args.request.read_text(encoding="utf-8"))
        result = AgentGateway(kernel, {args.adapter_id: adapter}).execute(principal, request)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "RESULT_STORED" else 2
    except DomainError as error:
        print(json.dumps({"status": "BLOCKED", "reason": error.code}, ensure_ascii=False))
        return 2
    finally:
        if database is not None:
            database.close()


if __name__ == "__main__":
    raise SystemExit(main())
