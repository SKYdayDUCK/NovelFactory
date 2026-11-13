"""One authorized Workshop operation across two ingress transports and both SQL ledgers."""

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from agent_workshop.admin import enroll
from agent_workshop.auth import Denied, connect
from agent_workshop.manager_worker import run_once
from agent_workshop.organization import bootstrap
from agent_workshop.provision import allow_slot
from agent_workshop.reconcile import reconcile, remote
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from test_dual_stdio import bound_process

from benchmarks.http_load import loopback_server
from benchmarks.p59_workflow_load import FixtureAdapter
from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.delegation import install, provision_slot
from novel_factory.application.kernel import Kernel
from novel_factory.application.runtime_tasks import RuntimeTaskService
from novel_factory.application.supervisor import RuntimeSupervisor
from novel_factory.fixtures import load_fixture, point
from novel_factory.infrastructure.database import Database, tables
from novel_factory.infrastructure.objects import ObjectStore
from novel_factory.infrastructure.repository import KernelRepository
from novel_factory.interfaces.http import HttpSettings, create_app


@pytest.mark.parametrize("submit_via", ["mcp", "rest"])
def test_workshop_wakes_exact_project_after_cross_transport_completion(
    dual_databases, tmp_path, submit_via, record_property
):
    dsn, db = dual_databases["aw"], Database(dual_databases["nf"])
    kernel = Kernel(db, ObjectStore(tmp_path / "objects"))
    try:
        world = load_fixture(kernel)
        owner, wr = world["principal"], world["world_ref"]
        project = {k: wr[k] for k in ("work_id", "branch_id")}
        org, instance = str(uuid4()), str(uuid4())
        expires = datetime.now(UTC) + timedelta(hours=1)
        grant = install(
            kernel, owner, nf_instance_id=instance, org_id=org, expires_at=expires, **project
        )
        installation = grant["installation_id"]
        adapter = FixtureAdapter()
        gateway = AgentGateway(kernel, {"fixture": adapter})
        runtime, supervisor = (
            RuntimeTaskService(kernel, gateway),
            RuntimeSupervisor(kernel, gateway),
        )
        spec = {
            "role_id": "evidence_maintainer",
            "task_kind": "summary",
            "story_time": point(1),
            "subject_ids": [world["ids"]["sword"]],
            "predicates": ["owner"],
            "assignment": "Summarize the evidence.",
            "adapter_id": "fixture",
            "max_amount": "0.10",
            "token_budget": 100000,
        }
        frozen = runtime.define(owner, wr, **spec)
        task = frozen.spec["task_id"]
        adapter.jobs[task] = 1
        budget = kernel.tasks.create_budget(wr["work_id"], "1")
        provision_slot(
            supervisor,
            owner,
            installation,
            scope_ref="scene",
            task_template_id="summary",
            frozen=frozen,
            budget_id=budget,
            reserve_amount="0.10",
            token_reservation=100000,
            accept_summary=True,
        )
        policy = dict(
            nf_instance_id=instance,
            **project,
            generation=0,
            grant_epoch=grant["grant_epoch"],
            scope_ref="scene",
            task_template_id="summary",
            actor_binding_ref=None,
            role_id=spec["role_id"],
            max_amount=spec["max_amount"],
            currency="USD",
            token_limit=100000,
        )
        with connect(dsn) as conn:
            conn.execute(
                "INSERT INTO aw_installations VALUES (%s,%s,true)", (installation, instance)
            )
            bootstrap(conn, org, preset="Long-form", installation_id=installation)
            conn.execute("INSERT INTO aw_budget_accounts VALUES (%s,1,'USD')", (org,))
            allow_slot(conn, org, installation, "scene", "memory", policy)
            chief_token, manager_token = [
                enroll(conn, installation, org, employee, 1, 1) for employee in ("chief", "manager")
            ]
        aw_src = Path(__file__).resolve().parents[1] / "src"

        def aw_peer(name, token):
            directory = tmp_path / name
            directory.mkdir()
            return bound_process(
                directory, "agent_workshop.server", {"token": token, "database_url": dsn}, aw_src
            )

        chief, manager = aw_peer("chief", chief_token), aw_peer("manager", manager_token)
        resume = kernel.issue_grant(
            owner,
            wr["work_id"],
            role_id="resume_operator",
            capabilities=["PROJECT_RESUME"],
            branch_ids=[wr["branch_id"]],
            expires_at=expires,
        )
        project_peer = bound_process(
            tmp_path,
            "novel_factory.interfaces.mcp_stdio",
            {
                "token": resume["token"],
                "database_url": dual_databases["nf"],
                "binding": project,
                "object_root": str(kernel.objects.root),
            },
            Path(os.environ["AW_NF_SOURCE"]) / "src",
        )

        def authorize(conn):
            kernel.authorize(
                KernelRepository(conn, tables), owner, wr["work_id"], "ADMIN", wr["branch_id"]
            )

        def due(operation):
            with connect(dsn) as conn:
                conn.execute(
                    "UPDATE aw_delegations SET next_check=now() WHERE org_id=%s AND operation_id=%s",
                    (org, operation),
                )

        async def exercise(base):
            headers = {"Authorization": "Bearer " + grant["token"]}
            sender, author, project_call = map(remote, (chief, manager, project_peer))
            async with (
                httpx.AsyncClient(base_url=base, headers=headers, trust_env=False) as http,
                streamable_http_client(
                    base + "/mcp/installations/" + installation, http_client=http
                ) as wires,
                ClientSession(*wires[:2]) as sdk,
            ):
                await sdk.initialize()

                async def mcp_call(tool, args):
                    reply = await sdk.call_tool(tool, args)
                    assert not reply.isError, reply.structuredContent
                    return reply.structuredContent

                async def rest_call(tool, args):
                    action, code = {
                        "nf_delegation_submit": ("submit", 202),
                        "nf_delegation_get": ("status", 200),
                    }[tool]
                    reply = await http.post(
                        f"/api/v1/hera/installations/{installation}/delegations/{action}", json=args
                    )
                    assert reply.status_code == code, reply.text
                    return reply.json()

                create, read = (
                    (mcp_call, rest_call) if submit_via == "mcp" else (rest_call, mcp_call)
                )
                operation = (
                    await sender(
                        "aw_delegate",
                        {
                            "operation_id": "chapter-summary",
                            "slot_id": "scene",
                            "max_amount": "0.10",
                            "token_limit": policy["token_limit"],
                        },
                    )
                )["operation_id"]
                wake = {"operation_id": "on-summary", "after_delegation_id": operation}
                request = await author("aw_resume_project", wake)
                assert request["state"] == "WAITING_DELEGATION"
                assert await run_once(dsn, org, installation, project_call, read) == "IDLE"
                with connect(dsn) as conn:
                    intent = conn.execute(
                        """SELECT d.payload,h.status FROM aw_delegations d JOIN aw_budget_holds h
                        USING(org_id,operation_id) WHERE d.org_id=%s AND d.operation_id=%s""",
                        (org, operation),
                    ).fetchone()
                assert intent["status"] == "HELD"
                payload = intent["payload"]
                get = {
                    k: payload[k]
                    for k in (
                        "nf_instance_id",
                        "protocol_version",
                        "work_id",
                        "branch_id",
                        "operation_id",
                        "grant_epoch",
                    )
                }

                committed = {}

                async def lose_reply(tool, args):
                    committed["tool"], committed["payload"] = tool, args
                    committed["receipt"] = await create(tool, args)
                    raise ConnectionError("reply lost after confirmed NF commit")

                assert (
                    await reconcile(dsn, installation, org, operation, lose_reply) == "RECONCILING"
                )
                # Outside reconcile's exception boundary, validate the exact live input and output.
                assert (
                    committed["tool"] == "nf_delegation_submit" and committed["payload"] == payload
                )
                created = committed["receipt"]
                assert created["task_id"] == task and created["task_state"] == "READY"
                assert await read("nf_delegation_get", get) == created
                due(operation)
                assert await reconcile(dsn, installation, org, operation, read) == "BOUND"
                ready = await read("nf_delegation_get", get)
                assert await read("nf_delegation_submit", payload) == ready
                successor = runtime.prepare(owner, wr, queue_state="BLOCKED", **spec).task[
                    "task_id"
                ]
                supervisor.workflow.wait(
                    successor, 0, [task], mode="AND", authorize_in_tx=authorize, **project
                )
                supervisor.run_once(owner, **project)
                # Acceptance and project recovery are distinct public worker stages. Leave recovery to the subscribed manager.
                supervisor.advance_world(owner, **project)
                terminal = await read("nf_delegation_get", get)
                assert (
                    terminal["task_state"],
                    terminal["accounting_state"],
                    terminal["actual_tokens"],
                ) == ("ACCEPTED", "SETTLED", 18)
                assert len(terminal["attempts"]) == adapter.calls[task] == 1
                assert terminal["result_ref"] and terminal["receipt_ref"] == ready["receipt_ref"]
                with kernel.db.uow(write=False) as repo:
                    receipts = repo.many("delegation_receipts", {"installation_id": installation})
                    assert (
                        len(receipts) == 1
                        and receipts[0]["budget_reservation_ref"]
                        == payload["budget_reservation_ref"]
                    )
                    assert repo.one("tasks", {"task_id": successor})["state"] == "BLOCKED"
                due(operation)
                assert await reconcile(dsn, installation, org, operation, read) == "SETTLED"
                assert (await author("aw_resume_project", wake))["state"] == "PENDING"
                expected_scope = dict(
                    **project,
                    nf_instance_id=instance,
                    grant_epoch=grant["grant_epoch"],
                    installation_id=installation,
                    org_id=org,
                    operation_id=operation,
                    task_id=task,
                    receipt_ref=terminal["receipt_ref"],
                    generation=terminal["generation"],
                )

                async def exact_project(tool, args):
                    assert tool == "nf_resume_project" and args == {
                        "expected_scope": expected_scope
                    }
                    with pytest.raises(Denied, match="VIEW_DENIED"):
                        await project_call(
                            tool,
                            {"expected_scope": {**expected_scope, "receipt_ref": str(uuid4())}},
                        )
                    return await project_call(tool, args)

                assert await run_once(dsn, org, installation, exact_project, read) == "DONE"
                assert await run_once(dsn, org, installation, project_call, read) == "IDLE"
                done = await author("aw_resume_project", wake)
                assert (
                    done["request_id"] == request["request_id"] and done["result"]["awakened"] == 1
                )
                with kernel.db.uow(write=False) as repo:
                    assert repo.one("tasks", {"task_id": successor})["state"] == "READY"
                    assert not repo.many("call_attempts", {"task_id": successor})
                    assert kernel.head(repo, **project) == wr
                with connect(dsn) as conn:
                    mirror = conn.execute(
                        """SELECT r.receipt_ref,r.task_id,r.task_state,h.status,h.settled_amount,c.attempt_id,c.actual_tokens
                        FROM aw_remote_receipts r JOIN aw_budget_holds h USING(org_id,operation_id)
                        JOIN aw_cost_observations c USING(org_id,operation_id) WHERE r.org_id=%s AND r.operation_id=%s""",
                        (org, operation),
                    ).fetchall()
                assert len(mirror) == 1 and mirror[0]["settled_amount"] == 0
                assert (
                    str(mirror[0]["receipt_ref"]),
                    str(mirror[0]["task_id"]),
                    mirror[0]["task_state"],
                    mirror[0]["status"],
                    str(mirror[0]["attempt_id"]),
                    mirror[0]["actual_tokens"],
                ) == (
                    terminal["receipt_ref"],
                    task,
                    "ACCEPTED",
                    "SETTLED",
                    terminal["attempt_ids"][0],
                    18,
                )
                record_property(
                    "cross_transport_completion",
                    json.dumps(
                        {
                            **expected_scope,
                            "submitted_via": submit_via,
                            "read_and_reconciled_via": "rest" if submit_via == "mcp" else "mcp",
                            "manager_request": done["request_id"],
                            "successor": successor,
                            "awakened": done["result"]["awakened"],
                            "provider_calls": adapter.calls[task],
                            "result_ref": terminal["result_ref"],
                            "actual_tokens": mirror[0]["actual_tokens"],
                            "hold": mirror[0]["status"],
                            "budget_reservation_ref": payload["budget_reservation_ref"],
                        },
                        sort_keys=True,
                    ),
                )

        with loopback_server(
            create_app(kernel, settings=HttpSettings(hera_delegations=True)), lifespan="on"
        ) as base:
            asyncio.run(exercise(base))
    finally:
        db.close()
