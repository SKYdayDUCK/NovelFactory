"""Exercise W1's management and staff APIs through independent, restartable MCP processes."""
import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from uuid import uuid4

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from agent_workshop.admin import enroll
from agent_workshop.auth import connect
from agent_workshop.organization import bootstrap


@asynccontextmanager
async def live_session(binding):
    process = StdioServerParameters(
        command=sys.executable,
        args=["-m", "agent_workshop.server", "--binding", str(binding)],
    )
    async with stdio_client(process) as pipes, ClientSession(*pipes) as client:
        assert (await client.initialize()).serverInfo.name == "AgentWorkshop"
        yield client


async def accepted(client, name, arguments):
    reply = await client.call_tool(name, arguments)
    assert not reply.isError, reply.structuredContent
    return reply.structuredContent


def test_hiring_tenure_and_restart_over_stdio(tmp_path):
    dsn = os.environ["AW_DATABASE_URL"]
    installation, org = uuid4().hex, uuid4().hex
    with connect(dsn) as conn:
        conn.execute("INSERT INTO aw_installations VALUES (%s,%s,true)", (installation, "nf-w1"))
        bootstrap(conn, org, installation_id=installation)

    def member_binding(employee, revision):
        with connect(dsn) as conn:
            token = enroll(conn, installation, org, employee, revision, 1)
        path = tmp_path / f"{employee}-{revision}.json"
        with os.fdopen(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w") as out:
            json.dump({"database_url": dsn, "token": token}, out)
        return path

    manager = member_binding("manager", 1)
    hire = dict(operation_id="hire", expected_revision=1, employee_id="relief",
                role_id="character_actor", supervisor_id="volume", team_id="actors")

    async def run():
        async with live_session(manager) as lead:
            receipt = await accepted(lead, "aw_hire", hire)
            assert receipt["revision"] == 2
            assert await accepted(lead, "aw_hire", hire) == receipt
            wrong = await lead.call_tool("aw_hire", dict(hire, team_id="other"))
            assert wrong.isError and wrong.structuredContent["error"]["code"] == "IDEMPOTENCY_MISMATCH"
            actor = member_binding("relief", 1)
            async with live_session(actor) as staff:
                names = {tool.name for tool in (await staff.list_tools()).tools}
                assert "aw_roster_get" in names and "aw_hire" not in names
                denied = await staff.call_tool("aw_hire", hire)
                assert denied.isError and denied.structuredContent["error"]["code"] == "VIEW_DENIED"
                view = await accepted(staff, "aw_roster_get", {})
                assert {e["employee_id"] for e in view["employees"]} == {"relief", "volume"}
                pause = await accepted(lead, "aw_suspend", dict(operation_id="pause",
                                       expected_revision=2, employee_id="relief"))
                assert pause["employment_revision"] == 2
                assert "aw_roster_get" not in {t.name for t in (await staff.list_tools()).tools}
                assert (await staff.call_tool("aw_roster_get", {})).isError
                change = await accepted(lead, "aw_transfer", dict(hire, operation_id="transfer",
                                        expected_revision=3, team_id="new-team"))
                assert change["employment_revision"] == 3
                assert (await staff.call_tool("aw_roster_get", {})).isError
            async with live_session(member_binding("relief", 3)) as promoted:
                restored = await accepted(promoted, "aw_roster_get", {})
                own = next(e for e in restored["employees"] if e["employee_id"] == "relief")
                assert (own["team_id"], own["employment_revision"]) == ("new-team", 3)
                end = await accepted(lead, "aw_end", dict(operation_id="end", expected_revision=4,
                                     employee_id="relief"))
                assert (end["revision"], end["employment_revision"]) == (5, 4)
                assert (await promoted.call_tool("aw_roster_get", {})).isError
            expected_directory = await accepted(lead, "aw_roster_get", {})
        async with live_session(manager) as restarted:
            assert await accepted(restarted, "aw_roster_get", {}) == expected_directory
            state = await accepted(restarted, "aw_state_get", {})
            assert state["revision"] == 5
            ended = next(e for e in state["employees"] if e["employee_id"] == "relief")
            assert (ended["status"], ended["employment_revision"]) == ("ENDED", 4)

    asyncio.run(run())
