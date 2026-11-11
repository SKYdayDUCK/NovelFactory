import asyncio
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import sys
from uuid import uuid4

from mcp import StdioServerParameters

from agent_workshop.admin import enroll
from agent_workshop.auth import connect
from agent_workshop.organization import bootstrap
from agent_workshop.provision import allow_slot
from agent_workshop.reconcile import reconcile, remote
from novel_factory.application.agent_gateway import AgentGateway
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


def test_two_live_mcp_servers_recover_accepted_task_after_lost_reply(dual_databases, tmp_path):
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
            bootstrap(c, org)
            c.execute("INSERT INTO aw_installations VALUES (%s,%s,true)", (installation, instance))
            c.execute("INSERT INTO aw_budget_accounts VALUES (%s,1,'USD')", (org,))
            allow_slot(c, org, installation, "scene-A", "actor-1", policy)
            token = enroll(c, installation, org, "volume", 1, 1)
        aw = remote(
            bound_process(
                tmp_path,
                "agent_workshop.server",
                {"token": token, "database_url": dsn},
                Path(__file__).resolve().parents[1] / "src",
            )
        )
        nf = remote(
            bound_process(
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
        )

        async def run():
            args = dict(operation_id="initial", slot_id="scene-A", max_amount="1", token_limit=2000)
            requested = await aw("aw_delegate", args)
            op = requested["operation_id"]
            assert requested == await aw("aw_delegate", args)

            async def fail_after_commit(tool, request):
                result = await nf(tool, request)
                assert result["task_state"] == "READY"
                raise ConnectionError("injected after confirmed NF commit")

            assert await reconcile(dsn, installation, org, op, fail_after_commit) == "RECONCILING"

            def wake():
                with connect(dsn) as c:
                    c.execute(
                        "UPDATE aw_delegations SET next_check=now() WHERE operation_id=%s", (op,)
                    )

            wake()
            assert await reconcile(dsn, installation, org, op, nf) == "BOUND"
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

        asyncio.run(run())
    finally:
        db.close()
