"""Verify W2 employee and NF delegation profiles against separately owned databases."""
import asyncio
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from mcp import StdioServerParameters
from test_w0_dual_services import WORKSHOP, session

from novel_factory.application.kernel import Kernel
from novel_factory.fixtures import load_fixture, point
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.objects import ObjectStore


def private_json(path, value):
    with os.fdopen(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w") as out:
        json.dump(value, out)


async def rejected(client, tool, args, code):
    reply = await client.call_tool(tool, args)
    assert reply.isError and reply.structuredContent == {"error": {"code": code}}
    assert [part.text for part in reply.content] == [code]


def test_two_live_profiles_bind_one_operation_to_its_nf_installation(tmp_path, record_property):
    aw_dsn = os.environ["AW_DATABASE_URL"]
    nf_dsn = os.environ["NF_DATABASE_URL"]
    aw_bin = Path(os.environ.get("AW_W0_PYTHON", WORKSHOP / ".venv/bin/python")).parent
    nf_admin = WORKSHOP.parent / "scripts/manage_workshop_binding.py"
    objects = ObjectStore(tmp_path / "objects")
    db = Database(nf_dsn)
    try:
        kernel = Kernel(db, objects)
        world = load_fixture(kernel)
        wr = world["world_ref"]
        budget = kernel.tasks.create_budget(wr["work_id"], "2.00")
    finally:
        db.close()
    access = tmp_path / "nf-admin.json"
    private_json(access, {"database_url": nf_dsn, "object_root": str(objects.root), "token": world["token"],
                          "work_id": wr["work_id"], "branch_id": wr["branch_id"]})

    def nf(*args):
        result = subprocess.run([sys.executable, str(nf_admin), "--access-file", access, *args],
                                check=True, capture_output=True, text=True,
                                env={**os.environ, "NF_MODEL_API_KEY": "", "NF_MODEL_NAME": "offline-fixture",
                                     "NF_MODEL_ENDPOINT": "https://fixture.invalid/v1/chat/completions"})
        return json.loads(result.stdout) if result.stdout.strip() else None

    def aw(*args):
        subprocess.run([aw_bin / "aw-admin", *args], env={"AW_DATABASE_URL": aw_dsn}, check=True)

    instance, org = str(uuid4()), str(uuid4())
    bound, foreign, staff = [tmp_path / name for name in ("nf.json", "other-nf.json", "employee.json")]
    expiry = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    installation = nf("install", "--nf-instance-id", instance, "--org-id", org, "--expires-at", expiry,
                      "--binding-output", bound)
    nf("install", "--nf-instance-id", instance, "--org-id", str(uuid4()), "--expires-at", expiry,
       "--binding-output", foreign)
    install_id = installation["installation_id"]
    aw("install", install_id, instance)
    aw("bootstrap", org, "--installation", install_id)
    aw("enroll", install_id, org, "script", staff, "--revision", "1")
    employee_at_nf = tmp_path / "employee-at-nf.json"
    bridge_at_aw = tmp_path / "bridge-at-aw.json"
    nf_binding, aw_binding = [json.loads(p.read_text()) for p in (bound, staff)]
    private_json(employee_at_nf, {**nf_binding, "token": aw_binding["token"]})
    private_json(bridge_at_aw, {**aw_binding, "token": nf_binding["token"]})
    slot = tmp_path / "slot.json"
    private_json(slot, {"scope_ref": "scene.summary.1", "task_template_id": "summary.v1", "budget_id": budget,
                        "reserve_amount": "0.10", "token_reservation": 100000, "accept_summary": True,
                        "definition": {"world_ref": wr, "role_id": "evidence_maintainer", "task_kind": "summary",
                                       "story_time": point(1), "subject_ids": [world["ids"]["sword"]],
                                       "predicates": ["owner"], "assignment": "Summarize the assigned scene.",
                                       "adapter_id": "external", "max_amount": "0.10", "token_budget": 100000}})
    nf("slot", "--installation-id", install_id, "--input", slot)
    get = {"protocol_version": "aw-nf.v1", "nf_instance_id": instance, "work_id": wr["work_id"],
           "branch_id": wr["branch_id"], "grant_epoch": installation["grant_epoch"], "operation_id": str(uuid4())}
    submit = {**get, "task_template_id": "summary.v1", "scope_ref": "scene.summary.1", "actor_binding_ref": None,
              "generation": 0, "role_id": "evidence_maintainer", "budget_reservation_ref": str(uuid4()),
              "max_amount": "0.100000", "currency": "USD", "token_limit": 100000}

    def bridge(binding):
        params = StdioServerParameters(command=sys.executable, args=["-m", "novel_factory.interfaces.delegation_stdio",
                                                                     "--binding", str(binding)])
        return session(params, "NovelFactory.Delegation", record_property)

    async def check():
        def aw_client(binding):
            params = StdioServerParameters(command=str(aw_bin / "aw-mcp"), args=["--binding", str(binding)])
            return session(params, "AgentWorkshop", record_property)

        async with bridge(employee_at_nf) as wrong_nf, aw_client(bridge_at_aw) as wrong_aw:
            assert not (await wrong_nf.list_tools()).tools and not (await wrong_aw.list_tools()).tools
            await rejected(wrong_nf, "nf_delegation_get", get, "VIEW_DENIED")
            await rejected(wrong_aw, "aw_capabilities", {"protocol_version": "aw-nf.v1"}, "VIEW_DENIED")
        async with aw_client(staff) as employee:
            capabilities = await employee.call_tool("aw_capabilities", {"protocol_version": "aw-nf.v1"})
            assert not capabilities.isError
            assert capabilities.structuredContent["nf_instance_id"] == instance
            assert capabilities.structuredContent["installation_id"] == install_id
            staff_tools = {tool.name for tool in (await employee.list_tools()).tools}
            assert "aw_mailbox_get" in staff_tools and all(tool.startswith("aw_") for tool in staff_tools)
            await rejected(employee, "nf_delegation_submit", submit, "VIEW_DENIED")
            async with bridge(bound) as client:
                assert {tool.name for tool in (await client.list_tools()).tools} == {"nf_delegation_submit", "nf_delegation_get"}
                for tool in ("aw_roster_get", "nf_resume_project", "nf_context_get"):
                    await rejected(client, tool, {}, "VIEW_DENIED")
                await rejected(client, "nf_delegation_submit", {**submit, "org_id": org}, "INVALID_ARGUMENT")
                assert not (await client.call_tool("nf_delegation_submit", submit)).isError
            async with bridge(bound) as reconnected, bridge(foreign) as different_org:
                read = await reconnected.call_tool("nf_delegation_get", get)
                assert not read.isError
                receipt = read.structuredContent
                assert receipt["world_ref"] == wr and receipt["operation_id"] == get["operation_id"]
                assert not {"token", "database_url", "object_root", "grant_id", "context_bundle"} & receipt.keys()
                assert (receipt["task_state"], receipt["generation"], receipt["accounting_state"]) == ("READY", 0, "NO_ATTEMPT")
                assert not receipt["attempt_ids"] and not receipt["attempts"]
                replay = await reconnected.call_tool("nf_delegation_submit", submit)
                assert not replay.isError and replay.structuredContent == receipt
                await rejected(different_org, "nf_delegation_get", get, "DELEGATION_NOT_FOUND")
                await rejected(different_org, "nf_delegation_submit", submit, "VIEW_DENIED")
                nf("revoke", "--installation-id", install_id)
                assert not (await reconnected.list_tools()).tools
                await rejected(reconnected, "nf_delegation_get", get, "VIEW_DENIED")
                assert not (await employee.call_tool("aw_mailbox_get", {})).isError
                record_property("bridge.operation", json.dumps({k: receipt[k] for k in (
                    "operation_id", "receipt_ref", "task_id", "generation", "accounting_state")}, sort_keys=True))

    asyncio.run(check())
