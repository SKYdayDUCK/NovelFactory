"""Wire-level compatibility checks for the shared role and task transports."""
import asyncio
import os
import sys

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import pytest
from sqlalchemy import text

from benchmarks.bound_stdio_throughput import provision
from benchmarks.hera_mixed import task_spec
from benchmarks.http_load import loopback_server
from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.mcp_runtime import TaskBinding, TaskOperationsBinding
from novel_factory.application.runtime_tasks import RuntimeTaskService
from novel_factory.fixtures import point
from novel_factory.infrastructure.model_gateway import OpenAICompatibleAdapter
from novel_factory.interfaces.http import HttpSettings, create_app

from .test_hera_mixed_ingress import remote

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def assert_tool_error(reply, code, **extra):
    assert reply.isError
    assert reply.structuredContent == {"error": {"code": code, **extra}}
    assert [(item.type, item.text) for item in reply.content] == [("text", code)]


def test_legacy_stdio_capacity_keeps_original_wire_shape(service_env, service_world, tmp_path):
    kernel = service_env["kernel"]
    role = provision(kernel, tmp_path, 0, world=service_world)
    args = StdioServerParameters(command=sys.executable,
        args=["-m", "novel_factory.interfaces.mcp_stdio", "--binding", str(role.path)],
        env={"PYTHONPATH": os.environ.get("PYTHONPATH", "")})

    async def exercise():
        async with stdio_client(args) as streams, ClientSession(*streams) as session:
            hello = await session.initialize()
            assert (hello.protocolVersion, hello.serverInfo.version) == ("2025-11-25", "2.1.0")
            assert "nf_task_get" in {tool.name for tool in (await session.list_tools()).tools}
            with kernel.db.engine.begin() as locker:
                # Hold both real stdio workers inside the database before the third call.
                locker.execute(text("LOCK TABLE nf_grants IN ACCESS EXCLUSIVE MODE"))
                pending = [asyncio.create_task(session.call_tool("nf_task_get", {})) for _ in range(2)]
                async with asyncio.timeout(15):
                    while True:
                        locker.execute(text("SELECT pg_stat_clear_snapshot()"))
                        blocked = locker.scalar(text("""
                            SELECT count(*) FROM pg_stat_activity
                            WHERE application_name=:name AND wait_event_type='Lock'
                              AND pg_backend_pid()=ANY(pg_blocking_pids(pid))"""), {"name": role.application_name})
                        if blocked == 2:
                            break
                        await asyncio.sleep(.025)
                refused = await session.call_tool("nf_task_get", {})
                assert_tool_error(refused, "BACKPRESSURE")
            for result in await asyncio.gather(*pending):
                assert not result.isError and result.structuredContent == role.expected["nf_task_get"]
    asyncio.run(exercise())


def test_remote_and_rest_send_retry_guidance_after_domain_backpressure(service_env, service_world):
    kernel, wr = service_env["kernel"], service_world["world_ref"]
    writer = kernel.issue_grant(service_world["principal"], wr["work_id"], role_id="writer",
                                capabilities=["READ", "TASK_CREATE"])
    first, second = (task_spec(writer["principal"], wr) for _ in range(2))
    binding = TaskOperationsBinding(wr["work_id"], wr["branch_id"])
    kernel.create_task(writer["principal"], first, first["task_id"])
    app = create_app(kernel, mcp_bindings={writer["principal"]: binding})
    command = {"spec": second, "idempotency_key": second["task_id"]}

    async def exercise(base):
        async with remote(base, writer["token"]) as (rest, mcp):
            h, m = await asyncio.gather(rest.post("/api/v1/hera/tasks", json=command),
                                        mcp.call_tool("nf_task_submit", command))
            assert h.status_code == 429 and h.headers["Retry-After"] == "1"
            assert h.json()["code"] == "BACKPRESSURE" and h.json()["retryable"] is True
            assert_tool_error(m, "BACKPRESSURE", retryable=True, retry_after_ms=1000)
            cancellation = {"task_id": first["task_id"], "generation": 0, "idempotency_key": "free-slot"}
            assert not (await mcp.call_tool("nf_task_cancel", cancellation)).isError
            accepted = await rest.post("/api/v1/hera/tasks", json=command)
            assert accepted.status_code == 202
            replay = await mcp.call_tool("nf_task_submit", command)
            assert replay.structuredContent == accepted.json()

    with kernel.db.uow() as repo:
        previous = repo.conn.execute(text("SELECT enabled, principal_limit FROM nf_durable_admission_policy")).mappings().one()
        repo.conn.execute(text("UPDATE nf_durable_admission_policy SET enabled=true, principal_limit=1"))
    try:
        with loopback_server(app, lifespan="on") as base:
            asyncio.run(exercise(base))
    finally:
        with kernel.db.uow() as repo:
            repo.conn.execute(text("UPDATE nf_durable_admission_policy SET enabled=:enabled, principal_limit=:principal_limit"),
                              previous)


def test_remote_actor_and_writer_keep_role_cut_and_frozen_world(service_env, service_world):
    kernel, owner = service_env["kernel"], service_world["principal"]
    wr = service_world["world_ref"]
    fork = kernel.fork(owner, wr, "CANDIDATE", "writer-branch")
    adapter = OpenAICompatibleAdapter(endpoint="https://example.test/v1/chat/completions", model="test", api_key=None)
    runtime = RuntimeTaskService(kernel, AgentGateway(kernel, {"external": adapter}))
    common = dict(story_time=point(1), subject_ids=[service_world["ids"]["sword"]],
                  predicates=["owner"], assignment="Use the authorized cut.", adapter_id="external")
    actor = runtime.prepare(owner, wr, role_id="character_actor", task_kind="character_action",
                            actor_id=service_world["ids"]["protagonist"], **common)
    writer = runtime.prepare(owner, fork, role_id="writer", task_kind="scene_draft", **common)
    bindings = {job.principal: TaskBinding(job.task["task_id"], job.task["generation"], job.worker_id)
                for job in (actor, writer)}
    with kernel.db.uow(write=False) as repo:
        expected = {job.context_bundle_id: repo.one("contexts", {"context_bundle_id": job.context_bundle_id})["payload"]
                    for job in (actor, writer)}
    app = create_app(kernel, mcp_bindings=bindings,
                     settings=HttpSettings(compact_remote_role_reads=True, fenced_task_status=True))

    async def exercise(base):
        async with remote(base, actor.private_token) as (a_rest, a_mcp), remote(base, writer.private_token) as (w_rest, w_mcp):
            # Existing authorized grant with no installed remote binding.
            async with httpx.AsyncClient(base_url=base, trust_env=False) as client:
                unbound = await client.post("/mcp", headers={"Authorization": "Bearer " + service_world["token"]})
                assert unbound.status_code == 403 and unbound.json()["code"] == "VIEW_DENIED"
            wire_headers = {"Accept": "application/json"}
            handshake = await a_rest.post("/mcp", headers=wire_headers, json={
                "jsonrpc": "2.0", "id": 80, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                           "clientInfo": {"name": "profile-check", "version": "1"}},
            })
            assert handshake.status_code == 200 and handshake.json()["result"]["protocolVersion"] == "2025-06-18"
            invalid_version = await a_rest.post("/mcp", headers={**wire_headers, "MCP-Protocol-Version": "unknown"},
                                                 json={"jsonrpc": "2.0", "id": 81, "method": "tools/list", "params": {}})
            assert invalid_version.status_code == 400 and "error" in invalid_version.json()
            a, w = await asyncio.gather(a_mcp.call_tool("nf_context_get", {}), w_mcp.call_tool("nf_context_get", {}))
            for reply, job, expected_ref in ((a, actor, wr), (w, writer, fork)):
                assert not reply.isError
                ctx = reply.structuredContent
                assert ctx == expected[job.context_bundle_id]
                assert (ctx["role_id"], ctx["world_ref"]) == (job.spec["role_id"], expected_ref)
            assert a.structuredContent["facts"] == []
            assert {item["status"] for item in w.structuredContent["facts"]} == {"FACT"}
            for session, client, other in ((a_mcp, a_rest, writer), (w_mcp, w_rest, actor)):
                assert_tool_error(await session.call_tool("nf_context_get", {"world_ref": other.spec["world_ref"]}),
                                  "INVALID_ARGUMENT")
                assert_tool_error(await session.call_tool("nf_task_status", {"task_id": other.task["task_id"]}),
                                  "VIEW_DENIED")
                assert (await client.get("/api/v1/tasks/" + other.task["task_id"])).status_code == 403
            kernel.revoke_grant(owner, actor.principal.grant_id)
            assert (await a_rest.post("/mcp")).status_code == 401
            remaining = await w_mcp.call_tool("nf_context_get", {})
            assert not remaining.isError and remaining.structuredContent == w.structuredContent
    with loopback_server(app, lifespan="on") as base:
        asyncio.run(exercise(base))
