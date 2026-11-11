"""Exercise sender and recipient stdio processes across Redis loss and in-flight revocation."""
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import sys
from uuid import uuid4

from alembic import command
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import pytest

from agent_workshop.admin import enroll
from agent_workshop.auth import connect, digest
from agent_workshop.notifications import notify_batch, receive_batch, replay
from agent_workshop.organization import bootstrap
from test_installation_ownership import isolated_database


@asynccontextmanager
async def live(path):
    params = StdioServerParameters(command=sys.executable, args=["-m", "agent_workshop.server", "--binding", str(path)])
    async with stdio_client(params) as streams, ClientSession(*streams) as session:
        hello = await session.initialize()
        assert hello.serverInfo.name == "AgentWorkshop" and hello.protocolVersion == "2025-11-25"
        yield session


async def invoke(session, tool, args, error=None):
    reply = await session.call_tool(tool, args)
    if error:
        assert reply.isError and reply.structuredContent == {"error": {"code": error}}
    else:
        assert not reply.isError, (tool, reply.structuredContent)
    return reply.structuredContent


@pytest.mark.skipif(not os.environ.get("AW_REDIS_TEST_URL"), reason="An isolated Redis Streams endpoint is required")
def test_persistent_mailbox_after_stream_loss_and_sender_revocation(isolated_database, tmp_path):
    from redis import Redis
    dsn, migration = isolated_database
    command.upgrade(migration, "head")
    org = uuid4().hex
    redis = Redis.from_url(os.environ["AW_REDIS_TEST_URL"], decode_responses=True)
    stream, subscriber = "aw-w2-" + org, "worker-" + org
    with connect(dsn) as conn:
        conn.execute("INSERT INTO aw_installations VALUES (%s,%s,true)", (org, "fixture-nf"))
        bootstrap(conn, org, installation_id=org)
        tickets = {n: enroll(conn, org, org, n, 1, 1) for n in ("volume", "actor-1")}
    for name, token in tickets.items():
        with os.fdopen(os.open(tmp_path / name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w") as handle:
            json.dump({"database_url": dsn, "token": token}, handle)

    async def exercise():
        async with live(tmp_path / "volume") as director, live(tmp_path / "actor-1") as actor:
            for role in (director, actor):
                assert {"aw_message_send", "aw_mailbox_get", "aw_delivery_ack"} <= {t.name for t in (await role.list_tools()).tools}
            common = {"recipient": "actor-1", "scope_ref": "organization"}
            body = await invoke(director, "aw_message_body_put", {**common, "operation_id": "body", "content": "bounded fixture"})
            ordinary = {**common, "graph_revision": 1, "kind": "ASSIGN", "ttl_seconds": 300}
            send = {**ordinary, "operation_id": "first", "body_ref": body["body_ref"]}
            first = await invoke(director, "aw_message_send", send)
            assert await invoke(director, "aw_message_send", send) == first
            second = await invoke(director, "aw_message_send", {**send, "operation_id": "inflight"})
            await invoke(actor, "aw_message_send", {**ordinary, "operation_id": "upward", "recipient": "volume"}, "ROUTE_DENIED")
            assert notify_batch(dsn, redis, stream) == 2
            redis.xgroup_create(stream, subscriber, id="0")
            pending = redis.xreadgroup(subscriber, "interrupted", {stream: ">"}, count=2)
            assert len(pending[0][1]) == 2
            assert receive_batch(dsn, redis, stream, subscriber, "recovered", idle_ms=0) == 2
            assert redis.delete(stream) == 1
            assert replay(dsn) == 2 and notify_batch(dsn, redis, stream) == 2
            assert receive_batch(dsn, redis, stream, subscriber, "replayed", idle_ms=0) == 0
            items = (await invoke(actor, "aw_mailbox_get", {}))["items"]
            assert [item["message_id"] for item in items] == [first["message_id"], second["message_id"]]
            key = {"message_id": first["message_id"]}
            assert (await invoke(actor, "aw_message_body_get", key))["content"] == "bounded fixture"
            acknowledged = await invoke(actor, "aw_delivery_ack", key)
            assert await invoke(actor, "aw_delivery_ack", key) == acknowledged
            with connect(dsn) as conn:
                conn.execute("UPDATE aw_bindings SET revoked=true,grant_epoch=grant_epoch+1 WHERE token_digest=%s", (digest(tickets["volume"]),))
            assert not (await director.list_tools()).tools
            pending_key = {"message_id": second["message_id"]}
            await invoke(actor, "aw_message_body_get", pending_key, "SENDER_BINDING_UNVERIFIED")
            await invoke(actor, "aw_delivery_ack", pending_key, "SENDER_BINDING_UNVERIFIED")
        async with live(tmp_path / "actor-1") as restarted:
            assert (await invoke(restarted, "aw_mailbox_get", {"after": 1}))["items"] == [{"sequence": 2, "status": "UNAVAILABLE"}]

    try:
        asyncio.run(exercise())
    finally:
        redis.delete(stream)
    with connect(dsn) as conn:
        evidence = conn.execute("""SELECT
            (SELECT count(*) FROM aw_messages WHERE org_id=%s) AS messages,
            (SELECT count(*) FROM aw_outbox WHERE org_id=%s) AS outbox,
            (SELECT count(*) FROM aw_inbox WHERE subscriber=%s) AS consumed_once,
            (SELECT count(*) FROM aw_deliveries WHERE org_id=%s AND acknowledged_at IS NOT NULL) AS acknowledged""",
            (org, org, subscriber, org)).fetchone()
        assert evidence == {"messages": 2, "outbox": 2, "consumed_once": 2, "acknowledged": 1}
    output = os.environ.get("AW_W2_TRANSPORT_EVIDENCE")
    if output:
        Path(output).write_text(json.dumps(dict(evidence, stream_redeliveries_accepted=0, inflight_after_revoke="UNAVAILABLE", restarted=True), indent=2) + "\n")
