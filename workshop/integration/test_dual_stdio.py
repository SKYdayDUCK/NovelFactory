import asyncio
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from uuid import uuid4

from mcp import StdioServerParameters
from redis_rebuild import rebuild_outboxes

from agent_workshop.admin import enroll
from agent_workshop.auth import Denied, connect
from agent_workshop.manager_worker import run_once as manager_run_once
from agent_workshop.organization import bootstrap
from agent_workshop.provision import allow_slot
from agent_workshop.reconcile import reconcile, remote
from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.budget_limits import SharedBudgetService
from novel_factory.application.delegation import install, provision_slot
from novel_factory.application.kernel import Kernel
from novel_factory.application.runtime_tasks import RuntimeTaskService
from novel_factory.application.supervisor import RuntimeSupervisor
from novel_factory.fixtures import load_fixture, point
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.model_gateway import OpenAICompatibleAdapter
from novel_factory.infrastructure.objects import ObjectStore


def bound_process(tmp, module, config, src):
    path = tmp / (module + ".json")
    with os.fdopen(os.open(path, os.O_EXCL | os.O_CREAT | os.O_WRONLY, 0o600), "w") as stream:
        json.dump(config, stream)
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", module, "--binding", str(path)],
        env={"PYTHONPATH": str(src)},
    )


@pytest.mark.parametrize("session_option", [(), ("--reuse-session",)], ids=["cold", "reuse"])
def test_two_live_mcp_servers_recover_accepted_task_after_lost_reply(dual_databases, tmp_path, session_option):
    dsn = dual_databases["aw"]
    db = Database(dual_databases["nf"])
    kernel = Kernel(db, ObjectStore(tmp_path / "objects"))
    try:
        world = load_fixture(kernel)
        wr, controller = world["world_ref"], world["principal"]
        instance, org = str(uuid4()), str(uuid4())
        grant = install(
            kernel,
            controller,
            nf_instance_id=instance,
            org_id=org,
            work_id=wr["work_id"],
            branch_id=wr["branch_id"],
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        installation = grant["installation_id"]
        actor = world["ids"]["protagonist"]
        route = OpenAICompatibleAdapter(
            endpoint="https://example.invalid/v1/chat/completions", model="fixture", api_key=None
        )
        gateway = AgentGateway(kernel, {"test": route})
        frozen = RuntimeTaskService(kernel, gateway).define(
            controller,
            wr,
            role_id="character_actor",
            task_kind="character_action",
            actor_id=actor,
            story_time=point(1),
            subject_ids=[world["ids"]["sword"]],
            predicates=["owner"],
            assignment="Propose an action.",
            adapter_id="test",
            max_amount="1",
            token_budget=2000,
        )
        budget = kernel.tasks.create_budget(wr["work_id"], "2")
        budget_view = SharedBudgetService(kernel)
        budget_scope = (controller, wr["work_id"], wr["branch_id"], budget)
        before_call = budget_view.set_limit(*budget_scope, 2000)
        assert before_call["available_tokens"] == 2000
        provision_slot(
            RuntimeSupervisor(kernel, gateway),
            controller,
            installation,
            scope_ref="scene-A",
            task_template_id="character-action",
            frozen=frozen,
            budget_id=budget,
            reserve_amount="1",
            token_reservation=2000,
            context_tokens=1000,
        )
        policy = dict(
            nf_instance_id=instance,
            work_id=wr["work_id"],
            branch_id=wr["branch_id"],
            generation=0,
            grant_epoch=grant["grant_epoch"],
            scope_ref="scene-A",
            task_template_id="character-action",
            actor_binding_ref=actor,
            role_id="character_actor",
            max_amount="1",
            currency="USD",
            token_limit=2000,
        )
        with connect(dsn) as c:
            c.execute("INSERT INTO aw_installations VALUES (%s,%s,true)", (installation, instance))
            bootstrap(c, org, installation_id=installation)
            c.execute("INSERT INTO aw_budget_accounts VALUES (%s,1,'USD')", (org,))
            allow_slot(c, org, installation, "scene-A", "actor-1", policy)
            token = enroll(c, installation, org, "volume", 1, 1)
            manager_token = enroll(c, installation, org, "manager", 1, 1)
        project_grant = kernel.issue_grant(controller, wr["work_id"], role_id="resume_operator",
            capabilities=["PROJECT_RESUME"], branch_ids=[wr["branch_id"]],
            expires_at=datetime.now(UTC) + timedelta(hours=1))
        project = remote(bound_process(tmp_path, "novel_factory.interfaces.mcp_stdio", {
            "token": project_grant["token"], "database_url": dual_databases["nf"],
            "binding": {key: wr[key] for key in ("work_id", "branch_id")},
            "object_root": str(tmp_path / "objects")}, Path(os.environ["AW_NF_SOURCE"]) / "src"))
        manager_dir = tmp_path / "manager"
        manager_dir.mkdir()
        manager = remote(bound_process(manager_dir, "agent_workshop.server",
            {"token": manager_token, "database_url": dsn}, Path(__file__).resolve().parents[1] / "src"))
        aw = remote(
            bound_process(
                tmp_path,
                "agent_workshop.server",
                {"token": token, "database_url": dsn},
                Path(__file__).resolve().parents[1] / "src",
            )
        )
        nf_params = bound_process(
            tmp_path,
            "novel_factory.interfaces.delegation_stdio",
            dict(
                token=grant["token"],
                installation_id=installation,
                database_url=dual_databases["nf"],
                object_root=str(tmp_path / "objects"),
            ),
            Path(os.environ["AW_NF_SOURCE"]) / "src",
        )
        nf = remote(nf_params)

        async def run():
            args = dict(operation_id="initial", slot_id="scene-A", max_amount="1", token_limit=2000)
            requested = await aw("aw_delegate", args)
            op = requested["operation_id"]
            assert requested == await aw("aw_delegate", args)

            message = await aw("aw_message_send", {
                "operation_id": op, "recipient": "actor-1", "kind": "ASSIGN",
                "scope_ref": "organization", "graph_revision": 1, "ttl_seconds": 600,
            })
            wake_key = {"operation_id": "on-terminal", "after_delegation_id": op}
            subscribed = await manager("aw_resume_project", wake_key)
            assert subscribed["state"] == "WAITING_DELEGATION"
            assert await manager_run_once(dsn, org, installation, project, nf) == "IDLE"
            with pytest.raises(Denied, match="IDEMPOTENCY_MISMATCH"):
                await manager("aw_resume_project", {"operation_id": wake_key["operation_id"]})
            committed = {}

            async def fail_after_commit(tool, request):
                result = await nf(tool, request)
                assert result["task_state"] == "READY"
                committed.update(result)
                raise ConnectionError("injected after confirmed NF commit")

            assert await reconcile(dsn, installation, org, op, fail_after_commit) == "RECONCILING"

            streams = rebuild_outboxes(tmp_path, dsn, db, org, message["message_id"], committed["task_id"])
            # remote() launches a new AW stdio process for each call.
            assert await aw("aw_delegate", args) == {"operation_id": op, "state": "RECONCILING"}
            with connect(dsn) as c:
                hold = c.execute("SELECT status,amount FROM aw_budget_holds WHERE operation_id=%s", (op,)).fetchone()
                assert hold["status"] == "HELD" and hold["amount"] == 1

            def wake():
                with connect(dsn) as c:
                    c.execute(
                        "UPDATE aw_delegations SET next_check=now() WHERE operation_id=%s", (op,)
                    )

            wake()
            worker = subprocess.run(
                [sys.executable, "-m", "agent_workshop.reconcile", "--installation", installation,
                 "--org", org, "--operation", op, *session_option, "--", nf_params.command, *nf_params.args],
                env={**os.environ, "AW_DATABASE_URL": dsn}, capture_output=True, text=True, check=True,
            )
            restarted_worker = json.loads(worker.stdout)
            assert restarted_worker == {"operation_id": op, "state": "BOUND"}
            get = {
                name: policy[name] for name in ("nf_instance_id", "work_id", "branch_id", "grant_epoch")
            }
            get.update(protocol_version="aw-nf.v1", operation_id=op)
            bound = await nf("nf_delegation_get", get)
            assert bound == committed
            redis_receipt = {
                **streams, "operation_id": op, "task_id": bound["task_id"],
                "generation": bound["generation"], "receipt_ref": bound["receipt_ref"],
                "hold_after_loss": hold["status"], "reserved_amount": str(hold["amount"]),
                "world_ref": bound["world_ref"], "restarted_aw_state": "RECONCILING",
                "restarted_worker": restarted_worker, "nf_after_get_matches_lost_reply": True,
            }
            handoff = {"operation_id": op, "handoff_id": "transfer-follow-up", "recipient": "actor-2"}
            handed = await aw("aw_handoff", handoff)
            assert handed == handoff and await aw("aw_handoff", handoff) == handed
            assert await nf("nf_delegation_get", get) == bound
            with connect(dsn) as c:
                moved = c.execute(
                    """SELECT d.owner_id,d.owner_revision,d.payload,h.status AS hold
                       FROM aw_delegations d JOIN aw_budget_holds h USING (org_id,operation_id)
                       WHERE d.org_id=%s AND d.operation_id=%s""", (org, op),
                ).fetchone()
                assert (moved["owner_id"], moved["owner_revision"], moved["hold"]) == ("actor-2", 1, "HELD")
                assert moved["payload"]["actor_binding_ref"] == actor
            with kernel.db.uow(write=False) as repo:
                rows = repo.many("delegation_receipts", {"installation_id": installation})
                assert len(rows) == 1 and rows[0]["operation_id"] == op
                task = rows[0]["task_id"]
                assert repo.one("supervisor_jobs", {"task_id": task})
                assert not repo.many("call_attempts", {"task_id": task})
            kernel.cancel_task(frozen.principal, task, 0, "test-final")
            wake()
            assert await reconcile(dsn, installation, org, op, nf) == "SETTLED"
            with connect(dsn) as c:
                hold = c.execute(
                    "SELECT * FROM aw_budget_holds WHERE operation_id=%s", (op,)
                ).fetchone()
                receipt = c.execute(
                    "SELECT * FROM aw_remote_receipts WHERE operation_id=%s", (op,)
                ).fetchone()
                assert hold["status"] == "SETTLED" and hold["settled_amount"] == 0
                assert str(receipt["task_id"]) == task and receipt["task_state"] == "CANCELLED"
            # Reconnect over independent AW and NF SDK stdio sessions. Only
            # this manager's preauthorized event can release the project wake.
            assert (await manager("aw_resume_project", wake_key))["state"] == "PENDING"
            checked = {}
            async def scoped_project(tool, args):
                checked.update(args["expected_scope"])
                for key in ("branch_id", "receipt_ref"):
                    wrong_scope = {**checked, key: str(uuid4())}
                    with pytest.raises(Denied, match="VIEW_DENIED"):
                        await project(tool, {"expected_scope": wrong_scope})
                return await project(tool, args)
            assert await manager_run_once(dsn, org, installation, scoped_project, nf) == "DONE"
            assert checked["receipt_ref"] == committed["receipt_ref"] and checked["task_id"] == task
            assert await manager_run_once(dsn, org, installation, project, nf) == "IDLE"
            assert (await manager("aw_resume_project", wake_key))["state"] == "DONE"
            cancelled = await nf("nf_delegation_get", get)
            assert cancelled["receipt_ref"] == committed["receipt_ref"]
            assert cancelled["attempts"] == [] and cancelled["actual_tokens"] == 0
            after_cancel = budget_view.status(*budget_scope)
            assert after_cancel == before_call
            if (receipt_path := os.environ.get("AW_W3_REDIS_EVIDENCE")) and not session_option:
                redis_receipt["cancel_before_attempt"] = {
                    "budget_id": budget, "operation_id": op, "receipt_ref": cancelled["receipt_ref"],
                    "nf_after_cancel": after_cancel, "nf_reported_tokens": cancelled["actual_tokens"],
                    "workshop_hold": hold["status"], "workshop_settled_amount": str(hold["settled_amount"]),
                    "workshop_remote_task": receipt["task_state"], "attempt_count": len(cancelled["attempts"]),
                }
                Path(receipt_path).write_text(json.dumps(redis_receipt, indent=2, default=str) + "\n")

        asyncio.run(run())
    finally:
        db.close()
