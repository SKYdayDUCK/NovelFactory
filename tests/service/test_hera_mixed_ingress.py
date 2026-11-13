"""Real TCP REST + SDK Streamable HTTP and stdio share one migrated PostgreSQL ledger."""
import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import text

from benchmarks.http_load import loopback_server
from novel_factory.application.mcp_runtime import ProjectBinding, TaskOperationsBinding
from novel_factory.domain.canonical import fingerprint
from novel_factory.fixtures import point
from novel_factory.infrastructure.private_files import create_private_file
from novel_factory.interfaces.http import create_app

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


@asynccontextmanager
async def remote(base, token):
    headers = {"Authorization": "Bearer " + token}
    async with httpx.AsyncClient(base_url=base, headers=headers, trust_env=False) as client:
        async with streamable_http_client(base + "/mcp", http_client=client) as (read, write, _):
            async with ClientSession(read, write) as mcp:
                init = await mcp.initialize()
                assert init.protocolVersion == "2025-11-25"
                yield client, mcp


def test_mixed_task_lifecycle(service_env, service_world, tmp_path):
    env, world = service_env, service_world
    kernel, wr = env["kernel"], world["world_ref"]
    binding = TaskOperationsBinding(wr["work_id"], wr["branch_id"])
    other = kernel.issue_grant(world["principal"], wr["work_id"], role_id="writer",
                               capabilities=["READ"], planes=["WORLD"])
    resume = kernel.issue_grant(world["principal"], wr["work_id"], role_id="resume_operator",
                                branch_ids=[wr["branch_id"]], capabilities=["PROJECT_RESUME"],
                                expires_at=datetime.now(UTC) + timedelta(hours=1))
    unlisted_resume = kernel.issue_grant(world["principal"], wr["work_id"], role_id="resume_operator",
                                branch_ids=[wr["branch_id"]], capabilities=["PROJECT_RESUME"],
                                expires_at=datetime.now(UTC) + timedelta(hours=1))
    project = ProjectBinding(wr["work_id"], wr["branch_id"])
    bindings = {world["principal"]: binding, other["principal"]: binding, resume["principal"]: project}
    app = create_app(kernel, mcp_bindings=bindings)
    bindings.clear()  # Reusing the caller's manifest must not relax a mounted REST or MCP ceiling.
    spec = dict(schema_version="2.0", task_id=str(uuid4()), generation=0, role_id="control_operator",
                task_kind="hera_mixed", state="READY", world_ref=wr, story_time=point(0),
                context_bundle_id=None, policy_version="nf-policy-1", dependency_fingerprint=None,
                runtime_contract_version=fingerprint({"hera": 1}), capability_grant_id=world["principal"].grant_id,
                input_artifacts=[], budget={"currency": "USD", "max_amount": "1", "token_budget": 10},
                wall_deadline=(datetime.now(UTC) + timedelta(hours=1)).isoformat())
    foreign = dict(spec, task_id=str(uuid4()),
                   world_ref=kernel.fork(world["principal"], wr, "CANDIDATE", "test-foreign-branch"))
    kernel.create_task(world["principal"], foreign, "foreign-create")
    task_id = spec["task_id"]
    command = {"spec": spec, "idempotency_key": "hera-create"}
    status = {"task_id": task_id}
    cancel = {**status, "generation": 0, "idempotency_key": "hera-cancel"}
    task_url = "/api/v1/tasks/" + task_id

    async def exercise(base):
        async with remote(base, world["token"]) as (rest, mcp):
            assert {tool.name for tool in (await mcp.list_tools()).tools} == {
                "nf_task_submit", "nf_task_status", "nf_task_cancel"}
            m, h = await asyncio.gather(mcp.call_tool("nf_task_submit", command),
                                         rest.post("/api/v1/tasks", json=command))
            assert h.status_code == 200, h.text
            assert not m.isError and h.json() == m.structuredContent
            m, h = await asyncio.gather(mcp.call_tool("nf_task_status", status), rest.get(task_url))
            assert h.status_code == 200 and h.json() == m.structuredContent
            # A deployment may give a broad kernel principal a narrower installed binding.
            # Both adapters must recheck that same ceiling on status, submission and cancellation.
            foreign_id = foreign["task_id"]
            assert (await rest.get("/api/v1/tasks/" + foreign_id)).status_code == 403
            assert (await mcp.call_tool("nf_task_status", {"task_id": foreign_id})).isError
            assert (await rest.post("/api/v1/tasks", json={"spec": foreign, "idempotency_key": "foreign-create"})).status_code == 403
            assert (await rest.post("/api/v1/tasks/cancel", json={"task_id": foreign_id, "generation": 0,
                                                                "idempotency_key": "foreign-cancel"})).status_code == 403
            assert (await mcp.call_tool("nf_task_cancel", {"task_id": foreign_id, "generation": 0,
                                                           "idempotency_key": "foreign-cancel"})).isError
            claim = await rest.post("/api/v1/tasks/claim", json={"worker_id": "outside", "task_id": foreign_id})
            assert claim.status_code == 200 and claim.json() is None
            result = {"schema_version": "2.0", "result_id": str(uuid4()), "task_id": foreign_id,
                      "generation": 0, "attempt_id": str(uuid4()), "context_bundle_id": str(uuid4()),
                      "dependency_fingerprint": "0" * 64, "status": "SUCCEEDED", "artifacts": [],
                      "safe_summary": "scope check"}
            assert (await rest.post("/api/v1/tasks/results", json={"result": result, "worker_id": "outside"})).status_code == 403
            assert kernel.get_task(world["principal"], foreign_id)["state"] == "READY"
            async with remote(base, other["token"]) as (other_rest, other_mcp):
                denied = await other_mcp.call_tool("nf_task_status", status)
                assert denied.isError and denied.structuredContent["error"]["code"] == "VIEW_DENIED"
                assert (await other_rest.get(task_url)).status_code == 403
                assert (await other_mcp.call_tool("nf_task_submit", command)).isError
            m, h = await asyncio.gather(mcp.call_tool("nf_task_cancel", cancel),
                                         rest.post("/api/v1/tasks/cancel", json=cancel))
            assert h.status_code == 200 and m.structuredContent == h.json()
            assert (await mcp.call_tool("nf_task_submit", {**command, "principal_id": str(uuid4())})).isError
            assert (await rest.post("/mcp", headers={"Authorization": "Bearer revoked"})).status_code == 401

        async with remote(base, resume["token"]) as (rest, mcp):
            url = "/api/v1/works/{work_id}/branches/{branch_id}/workflow".format(**wr)
            foreign_url = "/api/v1/works/{work_id}/branches/{branch_id}/workflow".format(**foreign["world_ref"])
            assert (await rest.get(foreign_url)).status_code == 403
            assert (await rest.post(url + "/resume", headers={
                "Authorization": "Bearer " + unlisted_resume["token"]})).status_code == 403
            assert (await rest.get(url)).json() == (await mcp.call_tool("nf_project_status", {})).structuredContent
            assert (await rest.post(url + "/resume")).json() == (await mcp.call_tool("nf_resume_project", {})).structuredContent
        path = tmp_path / "binding.json"
        with os.fdopen(create_private_file(path), "w", encoding="utf8") as output:
            json.dump({"database_url": env["url"], "object_root": str(kernel.objects.root),
                       "binding": {"mode": "task_operations", **binding.__dict__},
                       "token": world["token"]}, output)
        args = StdioServerParameters(command=sys.executable, args=["-m", "novel_factory.interfaces.mcp_stdio",
                                                                  "--binding", str(path)],
                                     env={"PYTHONPATH": os.environ.get("PYTHONPATH", "")})
        async with stdio_client(args) as (read, write), ClientSession(read, write) as stdio:
            await stdio.initialize()
            receipt = await stdio.call_tool("nf_task_submit", command)
            assert receipt.structuredContent["task_id"] == task_id
            assert (await stdio.call_tool("nf_task_status", status)).structuredContent["state"] == "CANCELLED"

    with loopback_server(app, lifespan="on") as base:
        asyncio.run(exercise(base))
    with kernel.db.uow(write=False) as repo:
        assert repo.conn.scalar(text("SELECT count(*) FROM nf_outbox WHERE aggregate_id=:id AND topic='task.changed'"),
                                {"id": task_id}) == 2
        assert repo.conn.scalar(text("SELECT count(*) FROM nf_task_commands WHERE principal_id=:id"),
                                {"id": world["principal"].principal_id}) == 3


def test_mcp_transport_redacts_database_errors(service_env, service_world, monkeypatch):
    from sqlalchemy.exc import OperationalError

    from novel_factory.interfaces.http import HttpSettings
    from novel_factory.interfaces.mcp_http import RemoteTools

    def inject(*args, **kwargs):
        raise OperationalError("private query text", {}, RuntimeError("private database detail"))

    kernel = service_env["kernel"]
    world = service_world
    binding = TaskOperationsBinding(world["world_ref"]["work_id"], world["world_ref"]["branch_id"])
    bindings = {world["principal"]: binding}
    app = create_app(kernel, mcp_bindings=bindings)

    async def exercise(base):
        async with remote(base, world["token"]) as (client, _):
            with monkeypatch.context() as patch:
                patch.setattr(kernel, "authenticate", inject)
                response = await client.post("/mcp", json={})
                assert response.status_code == 503
                assert response.json()["code"] == "SERVICE_UNAVAILABLE" and "private" not in response.text

        async def bad_operation(*_):
            inject()
        from novel_factory.application.mcp_runtime import McpRuntime
        from novel_factory.domain.errors import DomainError
        transport = RemoteTools(kernel, bindings, None, bad_operation, HttpSettings())
        with pytest.raises(DomainError) as exc:
            await transport.run(McpRuntime(kernel, world["principal"], binding), lambda: None, (), {}, kind="query")
        assert exc.value.code == "SERVICE_UNAVAILABLE" and "private" not in exc.value.safe_message

    with loopback_server(app, lifespan="on") as base:
        asyncio.run(exercise(base))


@pytest.mark.parametrize("field", ["branch_id", "work_id"])
def test_task_operations_roster_requires_concrete_ids(field):
    from novel_factory.domain.errors import DomainError
    from novel_factory.interfaces.bindings import parse_binding

    entry = {"mode": "task_operations", "work_id": str(uuid4()), "branch_id": str(uuid4())}
    entry[field] = "*"
    with pytest.raises(DomainError) as error:
        parse_binding(entry)
    assert error.value.code == "INVALID_ARGUMENT"
