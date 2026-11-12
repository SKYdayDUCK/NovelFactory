"""A live W4 Skill is used by an NF delegated runtime on separate PostgreSQL clusters."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from shlex import quote
import sys
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_workshop.admin import enroll
from agent_workshop.auth import connect
from agent_workshop.organization import bootstrap
from agent_workshop.provision import allow_slot
from agent_workshop.reconcile import reconcile, remote
from agent_workshop.reviews_skills import register_skill
from mcp import ClientSession
from mcp.client.stdio import stdio_client
from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.delegation import install, provision_slot
from novel_factory.application.kernel import Kernel
from novel_factory.application.queries import WorldQueryService
from novel_factory.application.query_policy import profiles
from novel_factory.application.runtime_tasks import RuntimeTaskService
from novel_factory.application.supervisor import RuntimeSupervisor
from novel_factory.application.workshop_skills import DynamicSkillAuthority
from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import load_fixture, point
from novel_factory.infrastructure.creative_config import CreativeHarnessConfig
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.model_gateway import OpenAICompatibleAdapter
from novel_factory.infrastructure.objects import ObjectStore
from test_dual_stdio import bound_process


async def _rpc(port, name, args):
    async with stdio_client(port) as streams, ClientSession(*streams) as client:
        await client.initialize()
        result = await client.call_tool(name, args)
        assert not result.isError, result.structuredContent
        return result.structuredContent


def call(port, name, **args):
    return asyncio.run(_rpc(port, name, args))


class OfflineAdapter(OpenAICompatibleAdapter):
    @property
    def configuration(self):
        return {**super().configuration, "execution_kind": "TEST_FIXTURE"}


class DynamicSite(SimpleNamespace):
    def __repr__(self):
        return "DynamicSite(two isolated local PostgreSQL services)"


@pytest.fixture
def dynamic_site(dual_databases, tmp_path, monkeypatch, request):
    role_id, task_kind = getattr(request, "param", ("evidence_maintainer", "summary"))
    root = Path(os.environ["AW_NF_SOURCE"]).resolve()
    dsn, db = dual_databases["aw"], Database(dual_databases["nf"])
    kernel = Kernel(db, ObjectStore(tmp_path / "objects"))
    world = load_fixture(kernel)
    wr, owner = world["world_ref"], world["principal"]
    instance, org = str(uuid4()), str(uuid4())
    tickets = install(
        kernel,
        owner,
        nf_instance_id=instance,
        org_id=org,
        work_id=wr["work_id"],
        branch_id=wr["branch_id"],
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    installation = tickets["installation_id"]
    nf_delegation = bound_process(
        tmp_path,
        "novel_factory.interfaces.delegation_stdio",
        {
            "installation_id": installation,
            "token": tickets["token"],
            "database_url": dual_databases["nf"],
            "object_root": str(kernel.objects.root),
        },
        root / "src",
    )
    with connect(dsn) as conn:
        conn.execute("INSERT INTO aw_installations VALUES (%s,%s,true)", (installation, instance))
        bootstrap(conn, org, "Long-form", installation_id=installation)
        conn.execute("INSERT INTO aw_budget_accounts VALUES (%s,20,'USD')", (org,))
        employee = conn.execute(
            "SELECT employee_id FROM aw_employees WHERE org_id=%s AND role_id=%s LIMIT 1",
            (org, role_id),
        ).fetchone()["employee_id"]
        sender = conn.execute(
            "SELECT src FROM aw_current_message_edges WHERE org_id=%s AND dst=%s AND kind='ASSIGN' LIMIT 1",
            (org, employee),
        ).fetchone()["src"]
        staff = {name: enroll(conn, installation, org, name, 1, 1) for name in {employee, sender, "manager"}}
    ports = {}
    for name, token in staff.items():
        folder = tmp_path / name
        folder.mkdir()
        ports[name] = bound_process(
            folder, "agent_workshop.server", {"database_url": dsn, "token": token}, root / "workshop" / "src"
        )
    worker_binding = str(tmp_path / employee / "agent_workshop.server.json")
    flags = json.dumps(CreativeHarnessConfig(enabled=True, dynamic_agents=True).as_dict())
    # Observe the real serialized requests crossing NF -> W4 without changing server behavior.
    wire = tmp_path / "workshop-requests.jsonl"
    wire.touch(mode=0o600)
    relay = tmp_path / "observed-workshop"
    relay.write_text(
        "#!/bin/sh\ntee -a "
        + quote(str(wire))
        + " | "
        + quote(sys.executable)
        + ' -m agent_workshop.server "$@"\n'
    )
    relay.chmod(0o700)
    monkeypatch.setenv("NF_DYNAMIC_WORKSHOP_EXECUTABLE", str(relay))
    monkeypatch.setenv("NF_DYNAMIC_WORKSHOP_BINDING", worker_binding)
    monkeypatch.setenv("NF_CREATIVE_HARNESS_CONFIG", flags)
    nf_controller = bound_process(
        tmp_path,
        "novel_factory.interfaces.mcp_stdio",
        {
            "binding": {
                "mode": "creative_controller",
                "work_id": wr["work_id"],
                "branch_id": wr["branch_id"],
            },
            "database_url": dual_databases["nf"],
            "object_root": str(kernel.objects.root),
            "token": world["token"],
        },
        root / "src",
    )
    nf_controller.env.update(
        NF_DYNAMIC_WORKSHOP_BINDING=worker_binding,
        NF_DYNAMIC_WORKSHOP_EXECUTABLE=str(relay),
        NF_CREATIVE_HARNESS_CONFIG=flags,
    )
    requests, entered, release = [], Event(), Event()
    release.set()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            packet = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(packet)
            entered.set()
            release.wait(30)
            system = packet["messages"][0]["content"]
            identity = json.loads(system.split("\nIdentity: ", 1)[1].split("\nPayload JSON Schema: ", 1)[0])
            identity.pop("dependency_fingerprint", None)
            result = {
                **identity,
                "status": "SUCCEEDED",
                "unresolved_questions": [],
                "payload": {
                    "text": "The bridge keeper lifted the lamp.",
                    "event_refs": [],
                    "important_deltas": [],
                    "evidence_refs": [],
                },
            }
            raw = json.dumps(
                {
                    "id": "synthetic-receipt",
                    "choices": [{"message": {"content": json.dumps(result)}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 11, "completion_tokens": 7},
                }
            ).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    adapter = OfflineAdapter(
        endpoint=f"http://127.0.0.1:{server.server_port}/v1/chat/completions",
        model="synthetic-no-inference",
        api_key="offline-synthetic-only",
        allow_loopback_http=True,
    )
    gateway = AgentGateway(kernel, {"fixture": adapter})
    tasks, supervisor = RuntimeTaskService(kernel, gateway), RuntimeSupervisor(kernel, gateway)
    budget_id = kernel.tasks.create_budget(wr["work_id"], "20")

    def share(version, content):
        with connect(dsn) as conn:
            register_skill(conn, org, "close-reading", version, "creative", task_kind, content.encode())
        row = call(
            ports["manager"],
            "aw_skill_share",
            operation_id="share-" + version,
            skill_id="close-reading",
            source_revision=version,
            policy="PRIVATE",
            target=employee,
            ttl_seconds=3600,
        )
        return {
            "org_id": org,
            "installation_id": installation,
            "employee_id": employee,
            "employment_revision": 1,
            "grant_epoch": 1,
            "binding_id": row["binding_id"],
            "skill_id": "close-reading",
            "source_revision": version,
            "content_sha": row["content_sha"],
        }

    def delegate(slot_id, instance_ref=None, *, accept_summary=False, knowledge_cut=None, input_artifacts=()):
        frozen = tasks.define(
            owner,
            wr,
            role_id=role_id,
            task_kind=task_kind,
            knowledge_cut=knowledge_cut,
            input_artifacts=input_artifacts,
            story_time=point(1),
            subject_ids=[world["ids"]["sword"]],
            predicates=["owner"],
            assignment="辨清灯船的动作。",
            adapter_id="fixture",
            dynamic_instance_ref=instance_ref,
            max_amount="1",
            token_budget=60000,
        )
        provision_slot(
            supervisor,
            owner,
            installation,
            scope_ref=slot_id,
            task_template_id=task_kind,
            frozen=frozen,
            budget_id=budget_id,
            reserve_amount="1",
            token_reservation=60000,
            accept_summary=accept_summary,
        )
        with connect(dsn) as conn:
            allow_slot(
                conn,
                org,
                installation,
                slot_id,
                employee,
                {
                    "nf_instance_id": instance,
                    "work_id": wr["work_id"],
                    "branch_id": wr["branch_id"],
                    "generation": 0,
                    "grant_epoch": tickets["grant_epoch"],
                    "scope_ref": slot_id,
                    "task_template_id": task_kind,
                    "actor_binding_ref": None,
                    "role_id": role_id,
                    "max_amount": "1",
                    "currency": "USD",
                    "token_limit": 60000,
                },
            )
        op = call(
            ports[sender],
            "aw_delegate",
            operation_id="delegate-" + slot_id,
            slot_id=slot_id,
            max_amount="1",
            token_limit=60000,
        )
        assert (
            asyncio.run(reconcile(dsn, installation, org, op["operation_id"], remote(nf_delegation)))
            == "BOUND"
        )
        tid, worker = frozen.spec["task_id"], "worker-" + slot_id
        row = kernel.claim_task(frozen.principal, worker, task_id=tid, lease_seconds=600)
        gen = row["generation"]
        ctx = WorldQueryService(kernel).context_compile(
            frozen.principal,
            {
                "schema_version": "2.1",
                "task_id": tid,
                "generation": gen,
                "recipe_id": profiles()[role_id]["allowed_recipes"][0],
                "input_manifest_ref": frozen.manifest_ref,
                "read_contract": {
                    "revision": {"mode": "FIXED", "world_ref": wr},
                    "process_policy": "ALLOW_PARTIAL",
                    "index_policy": "AUTHORITATIVE",
                    "max_wait_ms": 0,
                },
                "knowledge_cut": frozen.cut,
                "subject_time": {"kind": "POINT", "at": point(1)},
                "token_budget": 30000,
                "idempotency_key": "compile-" + slot_id,
            },
        )
        assert ctx["status"] == "READY"
        with kernel.db.uow(write=False) as repo:
            job = repo.one("supervisor_jobs", {"task_id": tid})
        req = {
            "schema_version": "2.1",
            "task_id": tid,
            "generation": gen,
            "worker_id": worker,
            "call_key": "dispatch-" + slot_id,
            **job["config"]["dispatch"],
        }
        return SimpleNamespace(
            frozen=frozen, task_id=tid, generation=gen, principal=frozen.principal, request=req
        )

    def activate(
        source,
        binding,
        content,
        profile="light-profile",
        *,
        role_text="Read the lantern keeper's hesitation before describing his decision.",
        requested_tools=None,
    ):
        request = dict(
            task_id=source.task_id,
            generation=source.generation,
            workshop_binding=binding,
            profile_version=profile,
            role_instructions=role_text,
            skill_instructions=content,
            requested_tools=requested_tools or [],
            trigger="TASK_DEMAND",
            evidence_refs=[source.frozen.manifest_ref],
            quota={
                "currency": "USD",
                "max_amount": "1",
                "max_tokens": 60000,
                "max_attempts": 1,
                "budget_id": budget_id,
                "deadline": (datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
            },
            idempotency_key="register-" + profile,
        )
        revision = call(nf_controller, "nf_harness_agent_register", **request)
        state = call(
            nf_controller, "nf_harness_agent_history", world_ref=wr, instance_ref=revision["instance_ref"]
        )
        call(
            nf_controller,
            "nf_harness_agent_select",
            world_ref=wr,
            instance_ref=revision["instance_ref"],
            revision_ref=revision["revision_ref"],
            expected_epoch=state["epoch"],
            idempotency_key="select-" + profile,
        )
        return revision

    try:
        yield DynamicSite(
            kernel=kernel,
            db=db,
            dsn=dsn,
            world=world,
            owner=owner,
            wr=wr,
            employee=employee,
            supervisor=supervisor,
            gateway=gateway,
            org=org,
            installation=installation,
            budget_id=budget_id,
            nf_controller=nf_controller,
            ports=ports,
            delegate=delegate,
            share=share,
            activate=activate,
            requests=requests,
            entered=entered,
            release=release,
            wire=wire,
        )
    finally:
        release.set()
        server.shutdown()
        thread.join()
        server.server_close()
        db.close()


def test_registered_employee_source_is_verified_in_workshop_before_and_after_delegation(dynamic_site):
    s = dynamic_site
    content = "Compare the pause before the lantern is lifted."
    bound = s.share("v1", content)
    authority = DynamicSkillAuthority(s.kernel)
    selected = authority.register(
        s.owner, s.wr, workshop_binding=bound, role_id="evidence_maintainer", task_kind="summary"
    )
    assert (
        selected["binding"] == bound
        and selected["binding"]["content_sha"] == sha256(content.encode()).hexdigest()
    )
    source = s.delegate("source")
    live = authority.refresh(source.principal, source.task_id, source.generation, workshop_binding=bound)
    with s.kernel.db.uow() as repo:
        current = authority.verify(
            repo, source.principal, source.task_id, source.generation, workshop_binding=bound, live=live
        )
    assert current["receipt_ref"] and current["scope_ref"] == "source" and current["task_kind"] == "summary"
    with pytest.raises(DomainError):
        authority.refresh(
            source.principal,
            source.task_id,
            source.generation,
            workshop_binding={**bound, "org_id": str(uuid4())},
        )
    call(s.ports["manager"], "aw_skill_revoke", operation_id="revoke-v1", binding_id=bound["binding_id"])
    with pytest.raises(DomainError):
        authority.refresh(source.principal, source.task_id, source.generation, workshop_binding=bound)


ROLE_TEXT = "Tell the archivist to compare the boatman's pause with his next physical action."
SKILL_TEXT = "Before naming the feeling, quote the hand that moves the lantern."


def test_real_delegated_gateway_receives_selected_role_and_skill_and_bills_one_attempt(dynamic_site):
    s = dynamic_site
    source = s.delegate("proposal-source")
    binding = s.share("v1", SKILL_TEXT)
    register = s.activate(source, binding, SKILL_TEXT, role_text=ROLE_TEXT)
    dispatched = s.delegate("deliver", register["instance_ref"])
    output = s.gateway.execute(dispatched.principal, dispatched.request)
    assert output["status"] == "RESULT_STORED" and output["validation_status"] == "VALIDATED_CANDIDATE", (
        output
    )
    assert len(s.requests) == 1
    system = s.requests[0]["messages"][0]["content"]
    assert ROLE_TEXT in system and SKILL_TEXT in system
    assert "tools" not in s.requests[0]
    records = [json.loads(line) for line in s.wire.read_text().splitlines()]
    attestations = [
        m["params"]["arguments"]
        for m in records
        if m.get("method") == "tools/call" and m["params"]["name"] == "aw_dynamic_skill_attest"
    ]
    assert any(
        a.get("task", {}).get("task_id") == dispatched.task_id and a["freeze_task"] for a in attestations
    )
    assert all(set(a) <= {"task", "nf", "freeze_task", "binding_id"} for a in attestations)
    assert SKILL_TEXT not in s.wire.read_text() and ROLE_TEXT not in s.wire.read_text()
    with s.kernel.db.uow() as repo:
        calls = repo.many("call_attempts", {"task_id": dispatched.task_id})
        assert len(calls) == 1 and calls[0]["attempt_id"] == output["attempt_id"]
        assert calls[0]["state"] == "UNKNOWN" and not calls[0]["actual_amount"]
    s.kernel.tasks.settle_call(
        output["attempt_id"],
        state="SUCCEEDED",
        actual_amount="0.02",
        actual_tokens=18,
        provider_receipt="synthetic-offline-invoice",
    )
    with s.kernel.db.uow() as repo:
        billed = repo.one("call_attempts", {"attempt_id": output["attempt_id"]})
        assert (
            billed["state"] == "SUCCEEDED"
            and str(billed["actual_amount"]) == "0.020000"
            and billed["actual_tokens"] == 18
        )
        assert not repo.many("call_attempts", {"task_id": source.task_id})
    with connect(s.dsn) as conn:
        rows = conn.execute(
            """SELECT to_jsonb(s) AS source,to_jsonb(f) AS freeze,to_jsonb(d) AS delegation
                                FROM aw_skill_sources s JOIN aw_skill_snapshots f USING(org_id,skill_id)
                                JOIN aw_remote_receipts r ON (f.org_id,f.task_ref)=(r.org_id,r.task_id)
                                JOIN aw_delegations d ON (d.org_id,d.operation_id)=(r.org_id,r.operation_id)
                                WHERE s.org_id=%s AND r.task_id=%s""",
            (s.org, dispatched.task_id),
        ).fetchall()
        assert len(rows) == 1 and rows[0]["freeze"]["content_sha"] == sha256(SKILL_TEXT.encode()).hexdigest()
        serialized = json.dumps(rows)
        assert SKILL_TEXT not in serialized and ROLE_TEXT not in serialized


@pytest.mark.parametrize("change", ["employee_exit", "skill_revocation"])
def test_employee_exit_or_skill_revocation_during_real_model_wait_blocks_result(dynamic_site, change):
    s = dynamic_site
    binding = s.share("v1", SKILL_TEXT)
    revision = s.activate(s.delegate("registration"), binding, SKILL_TEXT, role_text=ROLE_TEXT)
    dispatched = s.delegate("in-flight", revision["instance_ref"])
    s.release.clear()
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(s.gateway.execute, dispatched.principal, dispatched.request)
        try:
            assert s.entered.wait(25)
            assert SKILL_TEXT in s.requests[0]["messages"][0]["content"]
            if change == "employee_exit":
                call(
                    s.ports["manager"],
                    "aw_end",
                    operation_id="leave",
                    expected_revision=1,
                    employee_id=s.employee,
                )
            else:
                call(
                    s.ports["manager"],
                    "aw_skill_revoke",
                    operation_id="revoke",
                    binding_id=binding["binding_id"],
                )
        finally:
            s.release.set()
        with pytest.raises(DomainError):
            pending.result(timeout=30)
    with s.kernel.db.uow() as repo:
        task = repo.one("tasks", {"task_id": dispatched.task_id})
        assert task["current_result_id"] is None and not repo.many(
            "task_results", {"task_id": dispatched.task_id}
        )
        assert len(repo.many("call_attempts", {"task_id": dispatched.task_id})) == 1
    assert len(s.requests) == 1


@pytest.mark.parametrize("dynamic_site", [("volume_director", "window_plan")], indirect=True)
def test_planning_controller_rejects_source_skill_revoked_after_proposal(dynamic_site, monkeypatch):
    from novel_factory.application.creative_planning import CreativePlanningService
    from novel_factory.application.mcp_runtime import CreativeControllerBinding, McpRuntime
    from novel_factory.application.planning import PlanningService

    root = Path(os.environ["AW_NF_SOURCE"]).resolve()
    monkeypatch.syspath_prepend(str(root / "tests" / "integration"))
    from test_planning import make_plan

    site = dynamic_site
    planner = PlanningService(site.kernel)
    plan, anchor = make_plan(planner, site.world)
    selected = planner.adopt_plan(site.owner, site.wr, anchor, None, "initial-plan")
    inputs = {"knowledge_cut": selected["knowledge_cut"],
              "input_artifacts": [anchor, plan["volume_contract_ref"], selected["plan_manifest_ref"]]}
    source = site.delegate("source-plan", **inputs)
    instruction = "Compare the consequences of each available route."
    binding = site.share("for-planning", instruction)
    revision = site.activate(source, binding, instruction, requested_tools=["plan_propose"])
    task = site.delegate("target-plan", revision["instance_ref"], **inputs)
    node = plan["nodes"][0]["node_id"]
    planner.bind_task_for_controller(site.owner, task.task_id, task.generation, anchor, [node], "assign-nodes")
    quota = {"budget_id": site.budget_id, "max_amount": "0.1", "max_tokens": 10000, "max_attempts": 1,
             "currency": "USD", "deadline": (datetime.now(UTC) + timedelta(minutes=3)).isoformat()}
    candidate = CreativePlanningService(site.kernel).propose(
        task.principal, task.task_id, task.generation, worker_id=task.request["worker_id"], plan_ref=anchor,
        node_ids=[node], trigger="ORDINARY", quota=quota, idempotency_key="propose")
    call(site.ports["manager"], "aw_skill_revoke", operation_id="retire-planning-skill",
         binding_id=binding["binding_id"])
    controller = McpRuntime(site.kernel, site.owner,
                            CreativeControllerBinding(site.wr["work_id"], site.wr["branch_id"]))
    with pytest.raises(DomainError) as rejected:
        controller.decide_creative_plan(
            task_id=task.task_id, generation=task.generation, candidate_ref=candidate["candidate_ref"],
            option_id=candidate["candidate"]["alternatives"][0]["option_id"], idempotency_key="adopt")
    assert rejected.value.code == "DYNAMIC_SKILL_DENIED"
    with site.kernel.db.uow(write=False) as repo:
        pointer = repo.one("selections", {"work_id": site.wr["work_id"],
                           "selection_kind": planner.selection_key(plan["volume_id"])})
        assert pointer["artifact_ref"] == anchor
        assert repo.one("grants", {"grant_id": task.principal.grant_id})["active"]
        assert not repo.many("artifacts", {"work_id": site.wr["work_id"], "kind": "CREATIVE_PLAN_DECISION"})
