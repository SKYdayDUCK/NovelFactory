"""Annotate the existing real NF two-binding and rolling-plan acceptance tests with a tool matrix."""
import json
import os
from pathlib import Path
from weakref import WeakKeyDictionary

from mcp import ClientSession
import pytest

TOOLS = {
    "nf_project_status", "nf_resume_project", "nf_context_get", "nf_task_get",
    "nf_query_batch", "nf_artifact_put", "nf_change_point_request",
    "nf_rolling_window_submit", "nf_change_point_review",
}
rows = {name: {"discovered": [], "success": [], "denied": []} for name in sorted(TOOLS)}
seen = WeakKeyDictionary()


@pytest.fixture(autouse=True)
def capture_bound_tools(monkeypatch, request):
    call = ClientSession.call_tool
    listing = ClientSession.list_tools

    async def discover(session, *args, **kwargs):
        result = await listing(session, *args, **kwargs)
        names = {item.name for item in result.tools}
        seen[session] = names
        for name in names & TOOLS:
            record(name, "discovered")
        return result

    def record(name, outcome):
        if request.node.nodeid not in rows[name][outcome]:
            rows[name][outcome].append(request.node.nodeid)

    async def observed(session, name, *args, **kwargs):
        result = await call(session, name, *args, **kwargs)
        if name not in TOOLS:
            return result
        advertised = seen.get(session)
        if not result.isError:
            assert advertised is not None and name in advertised
            record(name, "success")
        elif (advertised is not None and name not in advertised
              and result.structuredContent == {"error": {"code": "VIEW_DENIED"}}):
            record(name, "denied")
        return result

    monkeypatch.setattr(ClientSession, "list_tools", discover)
    monkeypatch.setattr(ClientSession, "call_tool", observed)


def pytest_sessionfinish(session, exitstatus):
    gaps = [(tool, field) for tool, fields in rows.items() for field, cases in fields.items() if not cases]
    Path(os.environ["NF_W0_TOOL_MATRIX"]).write_text(json.dumps({"tools": rows, "missing": gaps}, indent=2) + "\n")
    if gaps and exitstatus == pytest.ExitCode.OK:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
