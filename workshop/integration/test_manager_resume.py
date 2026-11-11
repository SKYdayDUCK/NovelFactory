import asyncio
from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
from uuid import uuid4

from agent_workshop.admin import enroll
from agent_workshop.auth import connect
from agent_workshop.manager_worker import run_once
from agent_workshop.organization import bootstrap
from agent_workshop.reconcile import remote
from novel_factory.application.kernel import Kernel
from novel_factory.fixtures import load_fixture
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.objects import ObjectStore
from test_dual_stdio import bound_process


def test_author_wakeup_recovers_through_real_nf_project_binding(dual_databases, tmp_path):
    db = Database(dual_databases["nf"])
    kernel = Kernel(db, ObjectStore(tmp_path / "objects"))
    try:
        w = load_fixture(kernel)
        wr = w["world_ref"]
        binding = {k: wr[k] for k in ("work_id", "branch_id")}
        g = kernel.issue_grant(
            w["principal"],
            wr["work_id"],
            role_id="resume_operator",
            capabilities=["PROJECT_RESUME"],
            branch_ids=[wr["branch_id"]],
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        project = remote(
            bound_process(
                tmp_path,
                "novel_factory.interfaces.mcp_stdio",
                dict(
                    token=g["token"],
                    database_url=dual_databases["nf"],
                    binding=binding,
                    object_root=str(tmp_path / "objects"),
                ),
                Path(os.environ["AW_NF_SOURCE"]) / "src",
            )
        )
        org, installation = str(uuid4()), str(uuid4())
        with connect(dual_databases["aw"]) as c:
            bootstrap(c, org)
            c.execute(
                "INSERT INTO aw_installations VALUES(%s,%s,true)", (installation, str(uuid4()))
            )
            token = enroll(c, installation, org, "author", 1, 1)
        aw = remote(
            bound_process(
                tmp_path,
                "agent_workshop.server",
                dict(token=token, database_url=dual_databases["aw"]),
                Path(__file__).resolve().parents[1] / "src",
            )
        )

        async def exercise():
            args = {"operation_id": "resume-after-restart"}
            req = await aw("aw_resume_project", args)
            assert req["state"] == "PENDING"

            async def unused(*_):
                raise AssertionError("idle project issued a delegation")

            assert (
                await run_once(dual_databases["aw"], org, installation, project, unused) == "DONE"
            )
            assert (
                await run_once(dual_databases["aw"], org, installation, project, unused) == "IDLE"
            )
            done = await aw("aw_resume_project", args)
            assert (
                done["request_id"] == req["request_id"]
                and done["result"]["workflow_state"] == "IDLE"
            )

        asyncio.run(exercise())
        with kernel.db.uow(write=False) as repo:
            assert not repo.many("tasks", {"work_id": wr["work_id"]})
    finally:
        db.close()
