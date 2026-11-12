"""W3's organization edges and budget apply on every authenticated MCP request."""

import asyncio
import json
import os
from contextlib import AsyncExitStack
from decimal import Decimal
from uuid import UUID, uuid4

from agent_workshop.admin import enroll
from agent_workshop.auth import connect
from test_delegation import setup_slot
from test_w1_stdio import accepted, live_session
from test_w2_stdio import denied


def test_live_roles_handoff_budget_race_and_revoked_slot_replay(tmp_path):
    dsn, org, install, volume = setup_slot()
    _, _, _, other_volume = setup_slot()
    with connect(dsn) as conn:
        manager = enroll(conn, install, org, "manager", 1, 1)
        actor = enroll(conn, install, org, "actor-1", 1, 1)

    async def run():
        async with AsyncExitStack() as stack:
            sessions = []
            for number, token in enumerate((volume, volume, manager, actor, other_volume)):
                path = tmp_path / f"session-{number}.json"
                with os.fdopen(
                    os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w"
                ) as out:
                    json.dump({"database_url": dsn, "token": token}, out)
                sessions.append(await stack.enter_async_context(live_session(path)))
            first, second, lead, employee, foreign = sessions
            names = {tool.name for tool in (await first.list_tools()).tools}
            assert {"aw_delegate", "aw_handoff"} <= names
            staff_names = {tool.name for tool in (await employee.list_tools()).tools}
            assert not {"aw_delegate", "aw_handoff"} & staff_names

            # Two different slots compete through independent processes for one allowance.
            requests = [
                {
                    "operation_id": f"request-{slot}",
                    "slot_id": slot,
                    "max_amount": "1",
                    "token_limit": 200,
                }
                for slot in ("scene-A", "scene-B")
            ]
            results = await asyncio.gather(
                first.call_tool("aw_delegate", requests[0]),
                second.call_tool("aw_delegate", requests[1]),
            )
            (success,) = [i for i, result in enumerate(results) if not result.isError]
            assert results[1 - success].structuredContent == {"error": {"code": "BUDGET_EXCEEDED"}}
            original = results[success].structuredContent
            assert await accepted(first, "aw_delegate", requests[success]) == original
            await denied(employee, "aw_delegate", requests[success], "VIEW_DENIED")
            op = original["operation_id"]
            transfer = {"operation_id": op, "handoff_id": "handoff-1", "recipient": "actor-2"}
            for client in (lead, foreign, employee):
                await denied(client, "aw_handoff", transfer, "VIEW_DENIED")
                await denied(
                    client, "aw_handoff", dict(transfer, operation_id=str(uuid4())), "VIEW_DENIED"
                )
            await denied(first, "aw_handoff", dict(transfer, recipient="chief"), "VIEW_DENIED")
            assert await accepted(first, "aw_handoff", transfer) == transfer
            assert await accepted(second, "aw_handoff", transfer) == transfer
            await denied(
                second, "aw_handoff", dict(transfer, recipient="actor-3"), "IDEMPOTENCY_MISMATCH"
            )

            await accepted(
                lead,
                "aw_suspend",
                {"operation_id": "suspend", "expected_revision": 1, "employee_id": "actor-1"},
            )
            await denied(first, "aw_delegate", requests[success], "VIEW_DENIED")
            assert not (await employee.list_tools()).tools
            return op

    op = asyncio.run(run())
    with connect(dsn) as conn:
        rows = conn.execute(
            """SELECT d.operation_id,d.owner_id,d.owner_revision,d.state,h.amount,h.status
               FROM aw_delegations d JOIN aw_budget_holds h USING(org_id,operation_id)
               WHERE d.org_id=%s""",
            (org,),
        ).fetchall()
        assert rows == [
            {
                "operation_id": UUID(op),
                "owner_id": "actor-2",
                "owner_revision": 1,
                "state": "REQUESTED",
                "amount": Decimal(1),
                "status": "HELD",
            }
        ]
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM aw_handoffs WHERE org_id=%s", (org,)
            ).fetchone()["n"]
            == 1
        )
