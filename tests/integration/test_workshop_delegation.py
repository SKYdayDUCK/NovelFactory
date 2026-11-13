"""Exercise the NF side of the two-database Saga with real PG17 and stdio MCP."""

import asyncio
import json
import os
import sys
import time
from threading import Event, current_thread, main_thread
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from sqlalchemy import event, text
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
    assert receipt["result_ref"] is None and receipt["wait_reason"] is None
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
        result = repo.one("task_results", {"result_id": settled["result_ref"]})
        assert result["task_id"] == final["task_id"] and result["generation"] == final["generation"]
        assert result["disposition"] == "CURRENT" and final["result_ref"] == settled["result_ref"]
        assert final["wait_reason"] is None and result["payload_hash"] not in json.dumps(final)
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
        and current["wait_reason"] == "DELEGATION_RECOVERY_FENCED"
        and not requests
    )


def test_receipt_match_cannot_unlock_missing_attempt_or_resume_model(site):
    from novel_factory.application.delegation import (
        installation_ledger,
        reconcile_restored_installation,
    )
    from novel_factory.infrastructure.recovery import fence_recovered_tasks

    runtime, submit, get, _, _, owner, requests, _ = site
    bound = runtime.call("nf_delegation_submit", submit)
    fence_recovered_tasks(runtime.kernel.db.engine, runtime.kernel.objects)
    snapshot = installation_ledger(runtime.kernel, owner, runtime.installation_id)
    assert snapshot[get["operation_id"]]["receipt_ref"] == bound["receipt_ref"]
    future = {
        k: dict(
            v,
            attempts=[
                {
                    "attempt_id": str(uuid4()),
                    "state": "UNKNOWN",
                    "actual_amount": None,
                    "actual_tokens": None,
                }
            ],
        )
        for k, v in snapshot.items()
    }
    with pytest.raises(DomainError, match="Receipts disagree"):
        reconcile_restored_installation(runtime.kernel, owner, runtime.installation_id, future)
    with pytest.raises(DomainError) as blocked:
        runtime.kernel.tasks.reserve_call(
            bound["task_id"],
            1,
            "stale-worker",
            budget_id=str(uuid4()),
            amount="0.1",
            call_key="retry",
            request_hash="a" * 64,
        )
    assert blocked.value.code == "DELEGATION_RECOVERY_FENCED" and not requests
    reconcile_restored_installation(runtime.kernel, owner, runtime.installation_id, snapshot)


def expire_installation(runtime):
    with runtime.kernel.db.uow() as repo:
        repo.change(
            "delegation_installations",
            {"installation_id": runtime.installation_id},
            {"expires_at": repo.now() - timedelta(seconds=1)},
        )


@pytest.mark.parametrize("boundary", ["reserve_call", "mark_call_sent"])
@pytest.mark.parametrize("authority_change", ["expiry", "policy", "slot_revocation"])
def test_current_delegation_authority_fences_new_model_spend(site, monkeypatch, boundary, authority_change):
    runtime, submit, get, _, supervisor, owner, requests, frozen = site
    bound = runtime.call("nf_delegation_submit", submit)
    ledger = runtime.kernel.tasks
    original = getattr(ledger, boundary)

    def change_before_authorization(*args, **kwargs):
        if authority_change == "expiry":
            expire_installation(runtime)
        elif authority_change == "slot_revocation":
            runtime.kernel.revoke_grant(owner, frozen.principal.grant_id)
        else:
            with runtime.kernel.db.uow() as repo:
                repo.change("works", {"work_id": get["work_id"]}, {"policy_version": "nf-policy-next"})
        return original(*args, **kwargs)

    monkeypatch.setattr(ledger, boundary, change_before_authorization)
    result = supervisor.admit_model(owner, get["work_id"], get["branch_id"])
    expected = {"expiry": "DELEGATION_RECOVERY_FENCED", "policy": "TASK_REBIND_REQUIRED", "slot_revocation": "VIEW_DENIED"}
    assert (result["status"], result["reason"]) == ("BLOCKED", expected[authority_change])
    assert not requests
    with runtime.kernel.db.uow(write=False) as repo:
        attempts = repo.many("call_attempts", {"task_id": bound["task_id"]})
        assert [attempt["state"] for attempt in attempts] == (
            ["PREPARED"] if boundary == "mark_call_sent" else []
        )
        budget = repo.one("budgets", {"work_id": get["work_id"]})
        assert budget["spent_amount"] == 0
        assert budget["reserved_amount"] == sum(attempt["reserved_amount"] for attempt in attempts)


def test_expiry_allows_settling_an_existing_unknown_invoice(site):
    runtime, submit, get, _, supervisor, owner, requests, _ = site
    bound = runtime.call("nf_delegation_submit", submit)
    supervisor.admit_model(owner, get["work_id"], get["branch_id"])
    before = runtime.call("nf_delegation_get", get)
    assert before["accounting_state"] == "UNKNOWN" and len(before["attempt_ids"]) == 1
    expire_installation(runtime)
    with pytest.raises(DomainError) as denied:
        runtime.call("nf_delegation_get", get)
    assert denied.value.code == "VIEW_DENIED"
    attempt_id = before["attempt_ids"][0]
    runtime.kernel.tasks.settle_call(
        attempt_id, state="SUCCEEDED", actual_amount="0.02", actual_tokens=18,
        provider_receipt="synthetic-late-invoice",
    )
    with runtime.kernel.db.uow(write=False) as repo:
        [attempt] = repo.many("call_attempts", {"task_id": bound["task_id"]})
        assert str(attempt["attempt_id"]) == attempt_id and attempt["actual_tokens"] == 18
        budget = repo.one("budgets", {"budget_id": attempt["budget_id"]})
        assert budget["spent_amount"] == attempt["actual_amount"] and budget["reserved_amount"] == 0
    assert len(requests) == 1


def test_two_installations_can_use_the_same_operation_and_hold(site):
    runtime, submit, get, _, supervisor, owner, requests, frozen = site
    kernel = runtime.kernel
    ticket = install(
        kernel,
        owner,
        nf_instance_id=get["nf_instance_id"],
        org_id=str(uuid4()),
        work_id=get["work_id"],
        branch_id=get["branch_id"],
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    peer = Delegations(kernel, ticket["installation_id"], ticket["token"])
    with kernel.db.uow(write=False) as repo:
        manifest = kernel.artifact(repo, frozen.manifest_ref, get["work_id"])["payload"]
    other = RuntimeTaskService(kernel, supervisor.gateway).define(
        owner,
        frozen.spec["world_ref"],
        role_id=submit["role_id"],
        task_kind="summary",
        story_time=point(1),
        subject_ids=manifest["subject_ids"],
        predicates=manifest["predicates"],
        assignment="Summarize observed state.",
        adapter_id="fixture",
        max_amount=submit["max_amount"],
        token_budget=submit["token_limit"],
    )
    provision_slot(
        supervisor,
        owner,
        peer.installation_id,
        scope_ref=submit["scope_ref"],
        task_template_id=submit["task_template_id"],
        frozen=other,
        budget_id=kernel.tasks.create_budget(get["work_id"], "2"),
        reserve_amount=submit["max_amount"],
        token_reservation=submit["token_limit"],
    )
    with ThreadPoolExecutor(2) as pool:
        left, right = pool.map(
            lambda service: service.call("nf_delegation_submit", submit), (runtime, peer)
        )
    assert left["task_id"] == frozen.spec["task_id"] and right["task_id"] == other.spec["task_id"]
    assert left["receipt_ref"] != right["receipt_ref"]
    assert (
        runtime.call("nf_delegation_get", get) == left
        and peer.call("nf_delegation_get", get) == right
    )
    assert not requests


def test_expiry_while_dispatch_authorization_waits_keeps_attempt_unsent(site, monkeypatch):
    from sqlalchemy import text

    runtime, submit, get, _, supervisor, owner, requests, _ = site
    bound = runtime.call("nf_delegation_submit", submit)
    tasks = runtime.kernel.tasks
    mark_sent = tasks.mark_call_sent

    def expire_during_authorization(attempt_id, *, worker_id, authorize_in_tx):
        with runtime.kernel.db.uow() as repo:
            deadline = repo.now() + timedelta(seconds=2)
            repo.change(
                "delegation_installations",
                {"installation_id": runtime.installation_id},
                {"expires_at": deadline},
            )

        def slow_authorization(conn):
            authorize_in_tx(conn)
            conn.execute(text("SELECT pg_sleep_until(:deadline)"), {"deadline": deadline})

        return mark_sent(attempt_id, worker_id=worker_id, authorize_in_tx=slow_authorization)

    monkeypatch.setattr(tasks, "mark_call_sent", expire_during_authorization)
    result = supervisor.admit_model(owner, get["work_id"], get["branch_id"])
    assert result["status"] == "BLOCKED" and result["reason"] == "DELEGATION_RECOVERY_FENCED"
    with runtime.kernel.db.uow(write=False) as repo:
        [attempt] = repo.many("call_attempts", {"task_id": bound["task_id"]})
        assert attempt["state"] == "PREPARED" and not requests

@pytest.mark.parametrize("boundary", ["reserve_call", "mark_call_sent"])
def test_installation_revoke_racing_model_spend_has_one_lock_order(site, monkeypatch, boundary):
    runtime, submit, get, _, supervisor, owner, requests, _ = site
    kernel = runtime.kernel
    runtime.call("nf_delegation_submit", submit)
    installation_locked, spending_at_installation = Event(), Event()
    spender_pid = None
    authorize = kernel.authorize
    spend = getattr(kernel.tasks, boundary)

    def pause_revoker(*args, **kwargs):
        if current_thread() is not main_thread() and args[3] == "ADMIN":
            # revoke() has already acquired FOR UPDATE on the installation.
            installation_locked.set()
            assert spending_at_installation.wait(10)
            conn = args[0].conn
            deadline = time.monotonic() + 10
            while not conn.scalar(
                text("SELECT pg_backend_pid() = ANY(pg_blocking_pids(:spender))"),
                {"spender": spender_pid},
            ):
                assert time.monotonic() < deadline, "spending transaction never waited on revocation"
                time.sleep(0.01)
        return authorize(*args, **kwargs)

    def before_sql(conn, cursor, statement, parameters, context, many):
        nonlocal spender_pid
        if installation_locked.is_set() and current_thread() is main_thread():
            if "FOR SHARE OF i" in statement:
                spender_pid = conn.connection.driver_connection.info.backend_pid
                spending_at_installation.set()

    def contend(*args, **kwargs):
        with ThreadPoolExecutor(1) as worker:
            revoked = worker.submit(revoke, kernel, owner, runtime.installation_id)
            assert installation_locked.wait(10)
            try:
                return spend(*args, **kwargs)
            finally:
                revoked.result(timeout=10)

    monkeypatch.setattr(kernel, "authorize", pause_revoker)
    monkeypatch.setattr(kernel.tasks, boundary, contend)
    event.listen(kernel.db.engine, "before_cursor_execute", before_sql)
    try:
        result = supervisor.admit_model(owner, get["work_id"], get["branch_id"])
    finally:
        event.remove(kernel.db.engine, "before_cursor_execute", before_sql)
    assert installation_locked.is_set() and spending_at_installation.is_set()
    assert result["status"] == "BLOCKED" and not requests


def test_denied_accounting_caller_cannot_distinguish_missing_from_expired(site):
    runtime, submit, get, _, supervisor, owner, requests, _ = site
    bound = runtime.call("nf_delegation_submit", submit)
    ledger = runtime.kernel.tasks
    supervisor.admit_model(owner, get["work_id"], get["branch_id"])
    [attempt_id] = runtime.call("nf_delegation_get", get)["attempt_ids"]
    expire_installation(runtime)

    def deny(_conn):
        raise DomainError("VIEW_DENIED", "Caller unavailable")

    for task in [bound["task_id"], str(uuid4())]:
        with pytest.raises(DomainError) as blocked:
            ledger.reserve_call(task, 1, "denied", budget_id=str(uuid4()), amount="0.01",
                                call_key="denied", request_hash="a" * 64, authorize_in_tx=deny)
        assert blocked.value.as_dict("same") == DomainError("VIEW_DENIED", "Caller unavailable").as_dict("same")
    for attempt in [attempt_id, str(uuid4())]:
        with pytest.raises(DomainError) as blocked:
            ledger.mark_call_sent(attempt, worker_id="denied", authorize_in_tx=deny)
        assert blocked.value.as_dict("same") == DomainError("VIEW_DENIED", "Caller unavailable").as_dict("same")
    assert len(requests) == 1


def test_reused_workshop_transport_reconnects_after_peer_death(site, monkeypatch):
    """A later read can find a committed submit after loss of its persistent NF peer."""
    import signal
    from pathlib import Path

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "workshop/src"))
    from agent_workshop.reconcile import batch_remote
    from anyio import BrokenResourceError, ClosedResourceError, EndOfStream
    from mcp.shared.exceptions import McpError

    runtime, submit, get, config, _, _, _, _ = site
    expected = runtime.call("nf_delegation_submit", submit)
    pidfile = config.with_name("stdio.pid")
    launcher = config.with_name("tracked_peer.py")
    launcher.write_text(
        "import os, runpy, sys\n"
        "from pathlib import Path\n"
        "Path(sys.argv.pop(1)).write_text(str(os.getpid()))\n"
        "runpy.run_module('novel_factory.interfaces.delegation_stdio', run_name='__main__')\n",
        encoding="utf-8",
    )
    params = StdioServerParameters(command=sys.executable, args=[
        str(launcher), str(pidfile), "--binding", str(config),
    ])

    async def run():
        async with batch_remote(params) as invoke:
            assert await invoke("nf_delegation_get", get) == expected
            pid = int(pidfile.read_text())
            # SIGTERM on POSIX / TerminateProcess via os.kill on Windows.
            os.kill(pid, signal.SIGTERM)
            with pytest.raises((BrokenResourceError, ClosedResourceError, EndOfStream, McpError, EOFError)):
                async with asyncio.timeout(5):
                    await invoke("nf_delegation_get", get)
            async with asyncio.timeout(15):
                assert await invoke("nf_delegation_get", get) == expected
            assert int(pidfile.read_text()) != pid

    asyncio.run(run())


def test_rest_delegation_disconnect_then_stdio_lookup_and_execution(site):
    from benchmarks.http_load import loopback_server
    from novel_factory.interfaces.http import HttpSettings, create_app
    import httpx

    runtime, submit, get, config, supervisor, owner, requests, frozen = site
    private = json.loads(config.read_text())
    path = "/api/v1/hera/installations/" + runtime.installation_id + "/delegations/"
    headers = {"Authorization": "Bearer " + private["token"]}
    app = create_app(runtime.kernel, settings=HttpSettings(hera_delegations=True,
                                                           per_principal_concurrency=1, queue_timeout_seconds=.2))
    async def two_entries(base):
        from mcp.client.streamable_http import streamable_http_client
        async with httpx.AsyncClient(base_url=base, headers=headers, trust_env=False) as rest:
            async with streamable_http_client(base+"/mcp/installations/"+runtime.installation_id, http_client=rest) as streams:
                async with ClientSession(streams[0], streams[1]) as sdk:
                    await sdk.initialize()
                    # Exhaust the shared, real application slot after protocol initialization.
                    async with app.state.admission.slot("installation:" + runtime.installation_id, "task"):
                        busy_rest, busy_mcp = await asyncio.gather(rest.post(path + "submit", json=submit),
                                                                  sdk.call_tool("nf_delegation_submit", submit))
                    assert busy_rest.status_code == 429 and busy_rest.headers["Retry-After"] == "1"
                    assert busy_mcp.isError and busy_mcp.structuredContent["error"] == {
                        "code": "BACKPRESSURE", "retryable": True, "retry_after_ms": 1000,
                        "recovery": "WAIT", "retry_same_operation": True}
                    with runtime.kernel.db.uow(write=False) as repo:
                        assert not repo.many("delegation_receipts", {"installation_id": runtime.installation_id})
                    response, replay = await asyncio.gather(rest.post(path + "submit", json=submit),
                                                           sdk.call_tool("nf_delegation_submit", submit))
                    assert response.status_code == 202 and response.json()["task_state"] == "READY"
                    assert not replay.isError and replay.structuredContent == response.json()
                    return response.json()["receipt_ref"]

    with loopback_server(app, lifespan="on") as base:
        receipt_id = asyncio.run(two_entries(base))

    async def read_via_stdio():
        params = StdioServerParameters(command=sys.executable,
            args=["-m", "novel_factory.interfaces.delegation_stdio", "--binding", str(config)])
        async with stdio_client(params) as stream, ClientSession(*stream) as sdk:
            await sdk.initialize()
            replay = await sdk.call_tool("nf_delegation_submit", submit)
            assert not replay.isError and replay.structuredContent["receipt_ref"] == receipt_id

    asyncio.run(read_via_stdio())
    for _ in range(2):
        supervisor.run_once(owner, get["work_id"], get["branch_id"])
    # New server and connection recover the receipt after the submitter left.
    with loopback_server(create_app(runtime.kernel, settings=HttpSettings(hera_delegations=True))) as base:
        with httpx.Client(base_url=base, trust_env=False) as rest:
            final = rest.post(path + "status", headers=headers, json=get)
            assert final.status_code == 200
            assert final.json()["task_id"] == frozen.spec["task_id"]
            assert final.json()["task_state"] == "ACCEPTED"
            assert len(requests) == len(final.json()["attempts"]) == 1
            revoke(runtime.kernel, owner, runtime.installation_id)
            assert rest.post(path + "status", headers=headers, json=get).status_code == 403
