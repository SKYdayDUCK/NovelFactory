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
from novel_factory.interfaces.http import HttpSettings, create_app

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


@pytest.mark.parametrize("fuse_status_auth", [False, True])
def test_mixed_task_lifecycle(service_env, service_world, tmp_path, fuse_status_auth):
    env, world = service_env, service_world
    kernel, wr = env["kernel"], world["world_ref"]
    binding = TaskOperationsBinding(wr["work_id"], wr["branch_id"])
    other = kernel.issue_grant(world["principal"], wr["work_id"], role_id="writer",
                               capabilities=["READ"], planes=["WORLD"])
    resume = kernel.issue_grant(world["principal"], wr["work_id"], role_id="resume_operator",
                                branch_ids=[wr["branch_id"]], capabilities=["PROJECT_RESUME"],
                                expires_at=datetime.now(UTC) + timedelta(hours=1))
    project = ProjectBinding(wr["work_id"], wr["branch_id"])
    bindings = {world["principal"]: binding, other["principal"]: binding, resume["principal"]: project}
    app = create_app(kernel, mcp_bindings=bindings, settings=HttpSettings(
        fenced_task_status=True, coalesce_task_status=True, fuse_status_auth=fuse_status_auth))
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
                "nf_task_submit", "nf_task_status", "nf_task_status_batch", "nf_task_cancel", "nf_task_events"}
            m, h = await asyncio.gather(mcp.call_tool("nf_task_submit", command),
                                         rest.post("/api/v1/hera/tasks", json=command))
            assert h.status_code == 202, h.text
            assert not m.isError and h.json() == m.structuredContent
            assert h.json()["status_url"] == task_url
            m, h = await asyncio.gather(mcp.call_tool("nf_task_status", status), rest.get(task_url))
            assert h.status_code == 200 and h.json() == m.structuredContent
            batch = {"work_id": wr["work_id"], "branch_id": wr["branch_id"], "task_ids": [task_id]}
            mb, hb = await asyncio.gather(mcp.call_tool("nf_task_status_batch", batch),
                                           rest.post("/api/v1/hera/tasks/status-batch", json=batch))
            assert mb.structuredContent == hb.json() == {"tasks": [h.json()]}
            denied_batch = {**batch, "task_ids": [task_id, foreign["task_id"]]}
            mb, hb = await asyncio.gather(mcp.call_tool("nf_task_status_batch", denied_batch),
                                           rest.post("/api/v1/hera/tasks/status-batch", json=denied_batch))
            assert mb.isError and hb.status_code == 403
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
            assert kernel.get_task(world["principal"], foreign_id)["state"] == "READY"
            async with remote(base, other["token"]) as (other_rest, other_mcp):
                denied = await other_mcp.call_tool("nf_task_status", status)
                assert denied.isError and denied.structuredContent["error"]["code"] == "VIEW_DENIED"
                assert (await other_rest.get(task_url)).status_code == 403
                assert (await other_mcp.call_tool("nf_task_submit", command)).isError
            m, h = await asyncio.gather(mcp.call_tool("nf_task_cancel", cancel),
                                         rest.post("/api/v1/tasks/cancel", json=cancel))
            assert h.status_code == 200 and m.structuredContent == h.json()
            event = (await rest.get(task_url + "/events", params={"limit": 1})).json()
            assert [e["state"] for e in event["events"]] == ["READY"] and event["has_more"]
            page = await mcp.call_tool("nf_task_events", {**status, "cursor": event["cursor"]})
            assert [e["state"] for e in page.structuredContent["events"]] == ["CANCELLED"]
            assert (await mcp.call_tool("nf_task_submit", {**command, "principal_id": str(uuid4())})).isError
            assert (await rest.post("/mcp", headers={"Authorization": "Bearer revoked"})).status_code == 401

        async with remote(base, resume["token"]) as (rest, mcp):
            url = "/api/v1/works/{work_id}/branches/{branch_id}/workflow".format(**wr)
            assert (await rest.get(url)).json() == (await mcp.call_tool("nf_project_status", {})).structuredContent
            assert (await rest.post(url + "/resume")).json() == (await mcp.call_tool("nf_resume_project", {})).structuredContent
            scoped = {"expected_scope": {
                **{k: str(uuid4()) for k in ("org_id", "nf_instance_id", "installation_id", "operation_id", "receipt_ref")},
                "work_id": wr["work_id"], "branch_id": wr["branch_id"], "task_id": task_id,
                "generation": 1, "grant_epoch": 1,
            }}
            denied = await mcp.call_tool("nf_resume_project", scoped)
            assert denied.isError and denied.structuredContent["error"]["code"] == "VIEW_DENIED"
            assert (await rest.post(url + "/resume", json=scoped)).json()["code"] == "VIEW_DENIED"
            assert (await rest.post(url + "/resume", json={"invalid": True})).status_code == 422

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
