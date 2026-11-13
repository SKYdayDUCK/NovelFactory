"""Real MCP SDK client over a child stdio server, backed by real PostgreSQL."""
import asyncio
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.runtime_tasks import RuntimeTaskService
from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import load_fixture, point
from novel_factory.infrastructure.model_gateway import OpenAICompatibleAdapter
from novel_factory.infrastructure.private_files import create_private_file

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def config(path, env, token, binding):
    with os.fdopen(create_private_file(path), "w") as file:
        json.dump({"binding": binding, "database_url": env["url"], "token": token,
                   "object_root": str(env["store"].root)}, file)
    return StdioServerParameters(command=sys.executable,
             args=["-m", "novel_factory.interfaces.mcp_stdio", "--binding", str(path)])


async def exercise(params, callback):
    async with stdio_client(params) as streams, ClientSession(*streams) as session:
        result = await session.initialize()
        assert result.protocolVersion == "2025-11-25" and result.serverInfo.name == "NovelFactory"
        return await callback(session)


async def error(session, name, arguments, code):
    result = await session.call_tool(name, arguments)
    assert result.isError and result.structuredContent == {"error": {"code": code}}
    assert result.content[0].text == code
    return result.structuredContent["error"]["code"]


async def discovered_tools(session, binding, record_testsuite_property):
    tools = {tool.name: tool.inputSchema for tool in (await session.list_tools()).tools}
    record_testsuite_property("nf_discovery." + binding, json.dumps(sorted(tools)))
    return tools


def test_project_stdio_resume_is_scoped_and_live(kernel_env, tmp_path, record_testsuite_property):
    kernel = kernel_env["kernel"]
    world = load_fixture(kernel)
    binding = {k: world["world_ref"][k] for k in ("work_id", "branch_id")}
    scoped = kernel.issue_grant(world["principal"], binding["work_id"], role_id="resume_operator",
        capabilities=["PROJECT_RESUME"], branch_ids=[binding["branch_id"]],
        expires_at=datetime.now(UTC) + timedelta(hours=1))

    async def unbound(session):
        assert not (await session.list_tools()).tools
        await error(session, "nf_project_status", {}, "VIEW_DENIED")

    other_branch = kernel.fork(world["principal"], world["world_ref"], "CANDIDATE", "w0-scope")
    other_work = load_fixture(kernel)["world_ref"]
    for label, ref in (("other-branch", other_branch), ("other-work", other_work)):
        other = {key: ref[key] for key in binding}
        asyncio.run(exercise(config(tmp_path / (label + ".json"), kernel_env, scoped["token"], other), unbound))

    async def check(session):
        tools = await discovered_tools(session, "resume_operator", record_testsuite_property)
        assert set(tools) == {"nf_project_status", "nf_resume_project"}
        status = await session.call_tool("nf_project_status", {})
        assert not status.isError and status.structuredContent["workflow_state"] == "IDLE"
        assert status.structuredContent["counts"] == {"READY": 0, "WAITING": 0, "RUNNING": 0}
        assert status.structuredContent["resume_grant_expires_at"]
        for _ in range(2):
            resp = await session.call_tool("nf_resume_project", {})
            assert resp.structuredContent["resume_grant_expires_at"]
            assert resp.structuredContent["workflow_state"] == "IDLE"
            assert resp.structuredContent["pending_author_approval_count"] == 0
            assert not resp.isError and resp.structuredContent["counts"] == {"READY": 0, "WAITING": 0, "RUNNING": 0}
            assert resp.structuredContent["awakened"] == 0
        await error(session, "nf_resume_project", {"work_id": str(uuid4())}, "INVALID_ARGUMENT")
        for name in ("nf_task_get", "nf_context_get", "nf_query_batch", "nf_artifact_put"):
            await error(session, name, {}, "VIEW_DENIED")
        kernel.revoke_grant(world["principal"], scoped["principal"].grant_id)
        await error(session, "nf_resume_project", {}, "VIEW_DENIED")
        assert not (await session.list_tools()).tools

    asyncio.run(exercise(config(tmp_path / "operator.json", kernel_env, scoped["token"], binding), check))
    with pytest.raises(DomainError, match="Operation is outside this grant|Resource unavailable"):
        kernel.put_artifact(scoped["principal"], binding["work_id"], "text")

    # An administrator must mint a narrow grant, even for the author-facing MCP process.
    async def deny_admin(session):
        assert not (await session.list_tools()).tools
        await error(session, "nf_resume_project", {}, "VIEW_DENIED")

    asyncio.run(exercise(config(tmp_path / "admin.json", kernel_env, world["token"], binding), deny_admin))


@pytest.mark.parametrize("fence", ["expiry", "policy"])
def test_operator_database_fences_apply_outside_mcp(kernel_env, fence):
    kernel = kernel_env["kernel"]
    world = load_fixture(kernel)
    wr = world["world_ref"]
    scoped = kernel.issue_grant(world["principal"], wr["work_id"], role_id="resume_operator",
        capabilities=["PROJECT_RESUME"], branch_ids=[wr["branch_id"]], expires_at=datetime.now(UTC) + timedelta(days=1))
    with kernel.db.uow() as repo:
        if fence == "expiry":
            repo.change("resume_grants", {"grant_id": scoped["principal"].grant_id}, {"expires_at": repo.now()})
        else:
            repo.change("works", {"work_id": wr["work_id"]}, {"policy_version": "policy-v-next"})
    with pytest.raises(DomainError, match="Grant expired or policy changed"):
        kernel.authenticate(scoped["token"])
    with kernel.db.uow() as repo, pytest.raises(DomainError, match="Grant expired or policy changed"):
        kernel.authorize(repo, scoped["principal"], wr["work_id"], "PROJECT_RESUME", wr["branch_id"])


@pytest.mark.parametrize("flags", [[], ["--compact-role-reads"]])
def test_actor_stdio_own_cut_only_and_candidate_replay(kernel_env, tmp_path, record_testsuite_property, flags):
    kernel = kernel_env["kernel"]
    world = load_fixture(kernel)
    subject = world["ids"]["sword"]
    adapter = OpenAICompatibleAdapter(endpoint="https://example.test/v1/chat/completions", model="test", api_key=None)
    service = RuntimeTaskService(kernel, AgentGateway(kernel, {"external": adapter}))
    a, b = [service.prepare(world["principal"], world["world_ref"], role_id="character_actor",
             task_kind="character_action", actor_id=world["ids"][who], story_time=point(1), subject_ids=[subject],
             predicates=["owner"], assignment="Propose an action", adapter_id="external")
            for who in ("protagonist", "rival")]
    binding = {"task_id": a.task["task_id"], "generation": a.task["generation"], "worker_id": a.worker_id}

    async def check(session):
        tools = await discovered_tools(session, "character_actor", record_testsuite_property)
        assert set(tools) == {"nf_context_get", "nf_task_get", "nf_query_batch", "nf_artifact_put"}
        task_view = await session.call_tool("nf_task_get", {})
        assert not task_view.isError
        assert task_view.structuredContent["task_id"] == a.task["task_id"]
        assert task_view.structuredContent["generation"] == a.task["generation"]
        await error(session, "nf_resume_project", {}, "VIEW_DENIED")
        for override in ({"actor_id": world["ids"]["rival"]}, {"task_id": b.task["task_id"]},
                         {"generation": -1}, {"knowledge_cut": {"plane": "PLAN"}},
                         {"knowledge_cut": {"plane": "READER"}}, {"knowledge_cut": {"plane": "WORLD"}}):
            await error(session, "nf_context_get", override, "INVALID_ARGUMENT")
            await error(session, "nf_query_batch", {"lookups": [{"subject_id": subject, "predicate": "owner"}],
                                                   **override}, "INVALID_ARGUMENT")
        ctx = (await session.call_tool("nf_context_get", {})).structuredContent
        assert ctx["actor_id"] == world["ids"]["protagonist"] and ctx["context_bundle_id"] == a.context_bundle_id
        assert ctx["world_ref"] == world["world_ref"] and all(f["status"] == "BELIEF" for f in ctx["facts"])
        batch = await session.call_tool("nf_query_batch", {"lookups": [{"subject_id": subject, "predicate": "owner"}]})
        assert not batch.isError and batch.structuredContent["all_items_succeeded"]
        state = batch.structuredContent["items"][0]["payload"]["state_items"][0]["answer"]
        assert state["plane"] == "BELIEF" and state["knowledge_status"] == "UNKNOWN" and state["data"] is None
        for outside in (world["ids"]["rival"], str(uuid4())):
            await error(session, "nf_query_batch", {"lookups": [{"subject_id": outside, "predicate": "owner"}]}, "VIEW_DENIED")
        candidate = {"content": "I decline the invitation.", "idempotency_key": "first-intention"}
        receipt = (await session.call_tool("nf_artifact_put", candidate)).structuredContent
        assert receipt["status"] == "CANDIDATE_STORED"
        assert (await session.call_tool("nf_artifact_put", candidate)).structuredContent == receipt
        await error(session, "nf_artifact_put", dict(candidate, content="I attend."), "IDEMPOTENCY_MISMATCH")
        with kernel.db.uow(write=False) as repo:
            assert kernel.head(repo, world["world_ref"]["work_id"]) == world["world_ref"]
            assert len(repo.many("artifacts", {"kind": "MCP_TASK_CANDIDATE", "work_id": world["world_ref"]["work_id"]})) == 1
        kernel.cancel_task(a.principal, binding["task_id"], binding["generation"], "cancel")
        await error(session, "nf_context_get", {}, "TASK_NOT_ACCEPTABLE")
        await error(session, "nf_artifact_put", candidate, "TASK_NOT_ACCEPTABLE")

    params = config(tmp_path / "actor.json", kernel_env, a.private_token, binding)
    params.args.extend(flags)
    asyncio.run(exercise(params, check))

    async def bad_binding(session):
        await error(session, "nf_context_get", {}, "VIEW_DENIED")
        assert not (await session.list_tools()).tools

    params = config(tmp_path / "swapped.json", kernel_env, a.private_token,
                    {"task_id": b.task["task_id"], "generation": b.task["generation"], "worker_id": b.worker_id})
    params.args.extend(flags)
    asyncio.run(exercise(params, bad_binding))
