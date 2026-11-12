"""Save six path-scoped fixtures. Whitelist fields; never observe or store headers."""
import functools
import json
import os
from pathlib import Path
import re
from uuid import UUID

import httpx
from novel_factory.application.continuity import ContinuityService


def record(key, param, value, body, result, kind):
    assert str(UUID(value)) == value
    target = Path(os.environ["DOC_PATH_EXAMPLES"])
    rows = json.loads(target.read_text()) if target.exists() else {}
    if key in rows:
        return
    node = os.environ["PYTEST_CURRENT_TEST"].split(" (", 1)[0]
    rows[key] = {"kind": kind, "test": node, "pathBindings": {param: value},
                 "request": body, "response": result}
    target.write_text(json.dumps(rows, indent=2, ensure_ascii=False, sort_keys=True) + "\n")


def pytest_configure(config):
    original = httpx.Client.send

    @functools.wraps(original)
    def send(self, request, *args, **kwargs):
        reply = original(self, request, *args, **kwargs)
        match = re.fullmatch(r"/api/v1/(works|tasks)/([a-f0-9-]+)(/continuity/(?:readings|reviews|publish|inspect))?", request.url.path)
        if match and reply.status_code == 200:
            resource, value, suffix = match.groups()
            param = "work_id" if resource == "works" else "task_id"
            key = request.method + " /api/v1/" + resource + "/{" + param + "}" + (suffix or "")
            record(key, param, value, json.loads(request.content) if request.content else {}, reply.json(), "HTTP_FIXTURE")
        return reply
    httpx.Client.send = send
    read = ContinuityService.read_for_task

    @functools.wraps(read)
    def read_task(self, principal, task_id, generation, snapshot_ref, **kwargs):
        result = read(self, principal, task_id, generation, snapshot_ref, **kwargs)
        record("POST /api/v1/tasks/{task_id}/continuity", "task_id", task_id,
               {"schema_version": "2.3", "generation": generation, "snapshot_ref": snapshot_ref, **kwargs}, result, "APPLICATION_FIXTURE")
        return result
    ContinuityService.read_for_task = read_task
