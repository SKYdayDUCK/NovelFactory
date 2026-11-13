import asyncio
from contextlib import AsyncExitStack

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import text

from benchmarks.http_load import loopback_server
from novel_factory.application.task_operations import TaskOperationsBinding
from novel_factory.fixtures import load_fixture
from novel_factory.interfaces.http import create_app

from .conftest import make_client


pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def test_open_mixed_client_rechecks_grant_across_two_works(service_env, service_world):
    kernel = service_env["kernel"]
    second = load_fixture(kernel, "r1_sword")
    first_client = make_client(service_env, service_world)
    second_client = make_client(service_env, second)
    clients = (first_client, second_client)
    bindings = {
        client["principal"]: TaskOperationsBinding(work["world_ref"]["work_id"], work["world_ref"]["branch_id"])
        for client, work in zip(clients, (service_world, second))
    }
    task_id = first_client["task"]["task_id"]
    foreign_id = second_client["task"]["task_id"]
    status_path = "/api/v1/tasks/" + task_id
    app = create_app(kernel, mcp_bindings=bindings)

    async def exercise(base):
        async with AsyncExitStack() as stack:
            http = await stack.enter_async_context(httpx.AsyncClient(
                base_url=base, headers={"Authorization": "Bearer " + first_client["token"]}, trust_env=False))
            read, write, _ = await stack.enter_async_context(streamable_http_client(base + "/mcp", http_client=http))
            sdk = await stack.enter_async_context(ClientSession(read, write))
            await sdk.initialize()
            receipt = await sdk.call_tool("nf_task_status", {"task_id": task_id})
            assert not receipt.isError and receipt.structuredContent == (await http.get(status_path)).json()
            assert (await http.get("/api/v1/tasks/" + foreign_id)).status_code == 403
            denied = await sdk.call_tool("nf_task_status", {"task_id": foreign_id})
            assert denied.isError and denied.structuredContent["error"]["code"] == "VIEW_DENIED"
            kernel.revoke_grant(service_world["principal"], first_client["principal"].grant_id)
            assert (await http.get(status_path)).status_code == 401
            rpc = {"jsonrpc": "2.0", "id": 100, "method": "tools/call",
                   "params": {"name": "nf_task_status", "arguments": {"task_id": task_id}}}
            denied_rpc = await http.post("/mcp", headers={"Accept": "application/json, text/event-stream"}, json=rpc)
            assert denied_rpc.status_code == 401 and denied_rpc.json()["code"] == "UNAUTHENTICATED"
            good = await http.get("/api/v1/tasks/" + foreign_id,
                                  headers={"Authorization": "Bearer " + second_client["token"]})
            assert good.status_code == 200 and good.json()["task_id"] == foreign_id

    with loopback_server(app, lifespan="on") as base:
        asyncio.run(exercise(base))
    with kernel.db.uow(write=False) as repo:
        count = repo.conn.scalar(text("SELECT count(*) FROM nf_outbox WHERE aggregate_id=:grant AND topic='authorization.revoked'"),
                                 {"grant": first_client["principal"].grant_id})
        assert count == 1
        assert all(repo.one("tasks", {"task_id": tid})["state"] == "RUNNING" for tid in (task_id, foreign_id))
