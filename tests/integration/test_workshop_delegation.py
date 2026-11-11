"""Exercise the NF side of the two-database Saga with real PG17 and stdio MCP."""

import asyncio
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from test_p5_supervisor import backend

from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.delegation import Delegations, install, provision_slot, revoke
from novel_factory.application.runtime_tasks import RuntimeTaskService
from novel_factory.application.supervisor import RuntimeSupervisor
from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import load_fixture, point

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


@pytest.fixture
def site(kernel_env, tmp_path):
    kernel = kernel_env["kernel"]
    world = load_fixture(kernel)
    owner, wr = world["principal"], world["world_ref"]
    inst, org = str(uuid4()), str(uuid4())
    ticket = install(
        kernel,
        owner,
        nf_instance_id=inst,
        org_id=org,
        work_id=wr["work_id"],
        branch_id=wr["branch_id"],
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    inst_id = ticket["installation_id"]
    runtime = Delegations(kernel, inst_id, ticket["token"])
    config = tmp_path / "binding.json"
    with os.fdopen(os.open(config, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w") as stream:
        json.dump(
            {
                "installation_id": inst_id,
                "token": ticket["token"],
                "database_url": kernel_env["url"],
                "object_root": str(kernel.objects.root),
            },
            stream,
        )
    with backend() as (adapter, requests, _):
        gateway = AgentGateway(kernel, {"fixture": adapter})
        supervisor = RuntimeSupervisor(kernel, gateway)
        budget_id = kernel.tasks.create_budget(wr["work_id"], "2")
        definition = RuntimeTaskService(kernel, gateway).define(
            owner,
            wr,
            role_id="evidence_maintainer",
            task_kind="summary",
            story_time=point(1),
            subject_ids=[world["ids"]["sword"]],
            predicates=["owner"],
            assignment="Summarize observed state.",
            adapter_id="fixture",
            max_amount="0.10",
            token_budget=100000,
        )
        provision_slot(
            supervisor,
            owner,
            inst_id,
            scope_ref="scene.summary.1",
            task_template_id="summary.v1",
            frozen=definition,
            budget_id=budget_id,
            reserve_amount="0.10",
            token_reservation=100000,
            accept_summary=True,
        )
        get = {
            "protocol_version": "aw-nf.v1",
            "nf_instance_id": inst,
            "work_id": wr["work_id"],
            "branch_id": wr["branch_id"],
            "operation_id": str(uuid4()),
            "grant_epoch": 1,
        }
        submit = dict(
            get,
            task_template_id="summary.v1",
            scope_ref="scene.summary.1",
            actor_binding_ref=None,
            role_id="evidence_maintainer",
            generation=0,
            budget_reservation_ref=str(uuid4()),
            max_amount="0.100000",
            currency="USD",
            token_limit=100000,
        )
        yield runtime, submit, get, config, supervisor, owner, requests, definition


def test_stdio_receipt_survives_lost_reply_and_supervisor_execution(site):
    runtime, submit, get, config, supervisor, owner, requests, frozen = site
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "novel_factory.interfaces.delegation_stdio", "--binding", str(config)],
    )

    async def call_once(method, args):
        async with stdio_client(params) as stream, ClientSession(*stream) as client:
            hello = await client.initialize()
            assert hello.serverInfo.name == "NovelFactory.Delegation"
            assert {t.name for t in (await client.list_tools()).tools} == {
                "nf_delegation_get",
                "nf_delegation_submit",
            }
            result = await client.call_tool(method, args)
            assert not result.isError, result.structuredContent
            return result.structuredContent

    asyncio.run(
        call_once("nf_delegation_submit", submit)
    )  # Discard return before a fresh process opens.
    receipt = asyncio.run(call_once("nf_delegation_get", get))
    assert receipt["task_id"] == frozen.spec["task_id"] and receipt["generation"] == 0
    assert (
        receipt["task_state"] == "READY"
        and receipt["accounting_state"] == "NO_ATTEMPT"
        and not requests
    )
    assert asyncio.run(call_once("nf_delegation_submit", submit)) == receipt
    for _ in range(2):
        supervisor.run_once(owner, get["work_id"], get["branch_id"])
    final = asyncio.run(call_once("nf_delegation_get", get))
    assert (
        final["receipt_ref"] == receipt["receipt_ref"]
        and final["task_state"] == "ACCEPTED"
        and final["generation"] == 1
    )
    assert (
        len(requests) == len(final["attempts"]) == 1 and final["attempts"][0]["state"] == "UNKNOWN"
    )
    # A completed candidate need not have a supplier invoice. Keep the original hold.
    assert final["attempts"][0]["actual_amount"] is None and final["accounting_state"] == "UNKNOWN"
    runtime.kernel.tasks.settle_call(
        final["attempt_ids"][0],
        state="SUCCEEDED",
        actual_amount="0.02",
        actual_tokens=18,
        provider_receipt="synthetic-invoice-1",
    )
    settled = asyncio.run(call_once("nf_delegation_get", get))
    assert settled["attempt_ids"] == final["attempt_ids"] and len(requests) == 1
    assert settled["actual_tokens"] == 18 and settled["accounting_state"] == "SETTLED"
    assert settled["actual_amount"] == "0.020000"
    with runtime.kernel.db.uow(write=False) as repo:
        assert len(repo.many("tasks", {"work_id": get["work_id"]})) == 1
        assert (
            len(repo.many("delegation_receipts", {"installation_id": runtime.installation_id})) == 1
        )
        assert (
            runtime.kernel.head(repo, get["work_id"], get["branch_id"]) == frozen.spec["world_ref"]
        )


def test_atomic_rejection_concurrent_replay_and_revocation(site, monkeypatch):
    runtime, submit, get, _, supervisor, owner, requests, _frozen = site

    def check_denied(method, data, code):
        with pytest.raises(DomainError) as e:
            runtime.call(method, data)
        assert e.value.code == code

    check_denied("nf_delegation_get", get, "DELEGATION_NOT_FOUND")
    for change, code in [
        ({"actor_binding_ref": str(uuid4())}, "VIEW_DENIED"),
        ({"role_id": "chief_editor"}, "VIEW_DENIED"),
        ({"branch_id": str(uuid4())}, "VIEW_DENIED"),
        ({"grant_epoch": 0}, "INVALID_ARGUMENT"),
        ({"task_template_id": "other"}, "VIEW_DENIED"),
        ({"max_amount": "0.01"}, "BUDGET_MISMATCH"),
        ({"capabilities": ["ADMIN"]}, "INVALID_ARGUMENT"),
        ({"generation": 1}, "INVALID_ARGUMENT"),
    ]:
        check_denied("nf_delegation_submit", dict(submit, **change), code)

    def crash_after_task(*args):
        raise RuntimeError("interrupt before receipt")

    with monkeypatch.context() as patch:
        patch.setattr(RuntimeSupervisor, "install_job", staticmethod(crash_after_task))
        with pytest.raises(RuntimeError):
            runtime.call("nf_delegation_submit", submit)
    with runtime.kernel.db.uow(write=False) as repo:
        assert not repo.many("tasks", {"work_id": get["work_id"]})
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(lambda _: runtime.call("nf_delegation_submit", submit), range(4)))
    assert results == [results[0]] * 4 and not requests
    check_denied("nf_delegation_submit", dict(submit, max_amount="0.11"), "IDEMPOTENCY_MISMATCH")
    check_denied("nf_delegation_submit", dict(submit, operation_id=str(uuid4())), "SLOT_CONSUMED")
    revoke(runtime.kernel, owner, runtime.installation_id)
    check_denied("nf_delegation_get", get, "VIEW_DENIED")
    assert (
        supervisor.run_once(owner, get["work_id"], get["branch_id"])["status"] == "IDLE"
        and not requests
    )


def test_restored_installation_requires_partner_reconciliation(site):
    from novel_factory.application.delegation import reconcile_restored_installation
    from novel_factory.infrastructure.recovery import fence_recovered_tasks

    runtime, submit, get, _, _, owner, requests, _ = site
    fence_recovered_tasks(runtime.kernel.db.engine, runtime.kernel.objects)
    with pytest.raises(DomainError) as e:
        runtime.call("nf_delegation_submit", submit)
    assert e.value.code == "DELEGATION_RECOVERY_FENCED"
    with pytest.raises(DomainError) as e:
        reconcile_restored_installation(
            runtime.kernel, owner, runtime.installation_id, {get["operation_id"]: str(uuid4())}
        )
    assert e.value.code == "DELEGATION_RECOVERY_FENCED"
    reconcile_restored_installation(runtime.kernel, owner, runtime.installation_id, {})
    bound = runtime.call("nf_delegation_submit", submit)
    fence_recovered_tasks(runtime.kernel.db.engine, runtime.kernel.objects)
    current = runtime.call("nf_delegation_get", get)
    assert (
        current["receipt_ref"] == bound["receipt_ref"]
        and current["task_state"] == "PAUSED"
        and not requests
    )
