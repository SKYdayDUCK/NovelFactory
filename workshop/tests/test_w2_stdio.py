"""W2 scoped messaging between independently authenticated Workshop stdio processes."""
import asyncio
import json
import os
from contextlib import AsyncExitStack
from uuid import uuid4

from agent_workshop.admin import enroll
from agent_workshop.auth import connect
from agent_workshop.organization import bootstrap
from test_w1_stdio import accepted, live_session


async def denied(client, tool, arguments, code):
    result = await client.call_tool(tool, arguments)
    assert result.isError and result.structuredContent == {"error": {"code": code}}
    assert [item.text for item in result.content] == [code]


def test_temporary_route_and_private_mail_are_scoped_over_stdio(tmp_path):
    dsn = os.environ["AW_DATABASE_URL"]
    orgs = [uuid4().hex for _ in range(2)]
    bindings = {}
    with connect(dsn) as conn:
        for org in orgs:
            installation = "install-" + org
            conn.execute("INSERT INTO aw_installations VALUES (%s,%s,true)", (installation, "nf-" + org))
            bootstrap(conn, org, installation_id=installation)
            for employee in ("manager", "script", "actor-1"):
                token = enroll(conn, installation, org, employee, 1, 1)
                path = tmp_path / f"{org}-{employee}.json"
                with os.fdopen(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w") as out:
                    json.dump({"database_url": dsn, "token": token}, out)
                bindings[org, employee] = path

    async def run():
        org, other = orgs
        async with AsyncExitStack() as clients:
            async def open_member(org_id, employee):
                return await clients.enter_async_context(live_session(bindings[org_id, employee]))

            manager = await open_member(org, "manager")
            script = await open_member(org, "script")
            actor = await open_member(org, "actor-1")
            foreign_actor = await open_member(other, "actor-1")
            foreign_manager = await open_member(other, "manager")
            names = {tool.name for tool in (await script.list_tools()).tools}
            assert {"aw_message_send", "aw_message_body_put", "aw_mailbox_get", "aw_delivery_ack"} <= names
            assert not {"aw_route_grant", "aw_route_revoke"} & names
            assert {"aw_route_grant", "aw_route_revoke"} <= {
                tool.name for tool in (await manager.list_tools()).tools
            }

            envelope = dict(operation_id="mail", recipient="actor-1", kind="ASSIGN",
                            scope_ref="organization", graph_revision=1, ttl_seconds=120)
            grant_args = dict(operation_id="permit", sender="script", recipient="actor-1", kind="ASSIGN",
                              scope_ref="organization", ttl_seconds=120, max_uses=1, expected_revision=1)
            body = dict(operation_id="note", recipient="actor-1", scope_ref="organization",
                        content="private test note for one employee only")
            for target in ("actor-1", "not-hired"):
                # An employee outside the visible directory and an absent employee receive identical denials.
                await denied(script, "aw_message_body_put", dict(body, recipient=target), "ROUTE_DENIED")
                for field in ("body_ref", "causation_id"):
                    with_reference = {**envelope, "recipient": target, field: str(uuid4())}
                    await denied(script, "aw_message_send", with_reference, "ROUTE_DENIED")
            await denied(script, "aw_route_grant", grant_args, "VIEW_DENIED")
            await denied(script, "aw_message_send", dict(envelope, sender="manager"), "INVALID_ARGUMENT")
            grant = await accepted(manager, "aw_route_grant", grant_args)
            assert await accepted(manager, "aw_route_grant", grant_args) == grant
            gid = grant["grant_id"]
            await denied(foreign_manager, "aw_route_revoke",
                         {"operation_id": "end", "grant_id": gid}, "ROUTE_DENIED")
            authorized = dict(envelope, route_grant_id=gid)
            for mismatch in ({"kind": "ASK"}, {"recipient": "actor-2"}, {"scope_ref": "other"}):
                await denied(script, "aw_message_send", {**authorized, **mismatch}, "ROUTE_DENIED")
            await denied(actor, "aw_message_send", dict(authorized, recipient="script"), "ROUTE_DENIED")

            body_ref = (await accepted(script, "aw_message_body_put", body))["body_ref"]
            call = dict(authorized, body_ref=body_ref)
            receipt = await accepted(script, "aw_message_send", call)
            assert receipt["status"] == "QUEUED" and receipt["sequence"] == 1
            assert await accepted(script, "aw_message_send", call) == receipt
            await denied(script, "aw_message_send", dict(call, operation_id="next"), "ROUTE_DENIED")

            message = {"message_id": receipt["message_id"]}
            view = await accepted(actor, "aw_mailbox_get", {})
            assert [item["message_id"] for item in view["items"]] == [receipt["message_id"]]
            assert view["items"][0]["has_body"] and body["content"] not in json.dumps(view)
            assert await accepted(actor, "aw_message_body_get", message) == {"content": body["content"]}
            assert await accepted(foreign_actor, "aw_mailbox_get", {}) == {"items": [], "next_cursor": 0}
            for tool in ("aw_message_body_get", "aw_delivery_ack"):
                await denied(foreign_actor, tool, message, "DELIVERY_UNAVAILABLE")
            ack = await accepted(actor, "aw_delivery_ack", message)
            assert ack["status"] == "ACKNOWLEDGED"
            assert await accepted(actor, "aw_delivery_ack", message) == ack

    asyncio.run(run())
    with connect(dsn) as conn:
        assert conn.execute("SELECT consumed_uses FROM aw_route_grants WHERE org_id=%s", (orgs[0],)).fetchone() == {
            "consumed_uses": 1
        }
        assert conn.execute("SELECT count(*) AS n FROM aw_outbox WHERE org_id=%s", (orgs[0],)).fetchone()["n"] == 1
