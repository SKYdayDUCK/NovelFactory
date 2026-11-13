"""A single provisioned actor uses HTTP, MCP, a local host, and the one-shot CLI."""
import asyncio
import json
import subprocess
import sys

import httpx
import pytest
from sqlalchemy import text

from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.bound_tools import invoke_bound_tool
from novel_factory.application.mcp_runtime import McpRuntime, TaskBinding
from novel_factory.application.runtime_tasks import RuntimeTaskService
from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import load_fixture, point
from novel_factory.infrastructure.model_gateway import OpenAICompatibleAdapter
from novel_factory.interfaces.http import create_app
from test_p5_mcp import config, exercise

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def test_bound_actor_cross_entry_contracts(kernel_env, tmp_path):
    kernel = kernel_env["kernel"]
    world = load_fixture(kernel)
    subject = world["ids"]["sword"]
    adapter = OpenAICompatibleAdapter(endpoint="https://example.test/v1/chat/completions",
                                      model="fixture", api_key=None)
    role = RuntimeTaskService(kernel, AgentGateway(kernel, {"external": adapter})).prepare(
        world["principal"], world["world_ref"], role_id="character_actor", task_kind="character_action",
        actor_id=world["ids"]["protagonist"], story_time=point(1), subject_ids=[subject],
        predicates=["owner"], assignment="Propose an action", adapter_id="external")
    binding = TaskBinding(role.task["task_id"], role.task["generation"], role.worker_id)
    runtime = McpRuntime(kernel, role.principal, binding)
    path = tmp_path / "private.json"
    host = config(path, kernel_env, role.private_token, vars(binding))
    lookup = {"lookups": [{"subject_id": subject, "predicate": "owner"}]}
    query = invoke_bound_tool(runtime, "nf_query_batch", lookup)
    candidate = {"content": "I decline.", "idempotency_key": "same-decision"}
    receipt = invoke_bound_tool(runtime, "nf_artifact_put", candidate)

    def cli(name, args, code=0):
        process = subprocess.run(
            [sys.executable, "-m", "novel_factory.interfaces.bound_call", "--binding", str(path), name],
            input=json.dumps(args), capture_output=True, text=True, timeout=30)
        assert process.returncode == code, process.stderr
        return json.loads(process.stdout)

    assert cli("nf_artifact_put", candidate) == receipt
    assert cli("nf_artifact_put", {**candidate, "content": "Revised."}, 1) == {
        "error": {"code": "IDEMPOTENCY_MISMATCH"}}

    async def compare(session):
        batch = await session.call_tool("nf_query_batch", lookup)
        assert not batch.isError and batch.structuredContent == query
        write = await session.call_tool("nf_artifact_put", candidate)
        assert not write.isError and write.structuredContent == receipt
        with kernel.db.uow(write=False) as repo:
            cut = repo.one("contexts", {"context_bundle_id": role.context_bundle_id})["read_cut"]
        request = {"schema_version": "2.1", "task_id": binding.task_id, "generation": binding.generation,
                   "knowledge_cut": cut["knowledge_cut"],
                   "read_contract": {"revision": {"mode": "FIXED", "world_ref": world["world_ref"]},
                                     "process_policy": "ALLOW_PARTIAL", "index_policy": "AUTHORITATIVE",
                                     "max_wait_ms": 0},
                   "items": [{"item_id": "0", "subject_time": {"kind": "POINT", "at": point(1)},
                              "query": {"kind": "STATE", "subject_ids": [subject],
                                        "predicates": ["owner"]}, "page_size": 1}]}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(kernel)),
                                      base_url="http://test") as http:
            async def rest():
                return await http.post("/api/v1/queries/batch", json=request,
                                       headers={"Authorization": "Bearer " + role.private_token})
            response = await rest()
            assert response.is_success and response.json()["items"] == query["items"]
            kernel.cancel_task(role.principal, binding.task_id, binding.generation, "finished")
            assert (await rest()).json()["code"] == "TASK_NOT_ACCEPTABLE"
            denied = await session.call_tool("nf_query_batch", lookup)
            assert denied.structuredContent == {"error": {"code": "TASK_NOT_ACCEPTABLE"}}

    asyncio.run(exercise(host, compare))
    with pytest.raises(DomainError) as denied:
        invoke_bound_tool(runtime, "nf_query_batch", lookup)
    assert denied.value.code == "TASK_NOT_ACCEPTABLE"
    assert cli("nf_query_batch", lookup, 1) == {"error": {"code": "TASK_NOT_ACCEPTABLE"}}
    with kernel.db.uow(write=False) as repo:
        assert len(repo.many("artifacts", {"work_id": world["world_ref"]["work_id"],
                                          "kind": "MCP_TASK_CANDIDATE"})) == 1
        channels = set(repo.conn.scalars(text("""
            SELECT DISTINCT event_type FROM nf_audit_events WHERE task_id=:task
            AND evidence_id=:artifact AND tool_name='nf_artifact_put'
        """), {"task": binding.task_id, "artifact": receipt["artifact_ref"]["artifact_revision_id"]}))
        assert channels == {"local_tool.finished", "mcp.finished"}
