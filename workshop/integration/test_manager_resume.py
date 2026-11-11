import asyncio
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

from agent_workshop.admin import enroll
from agent_workshop.auth import connect
from agent_workshop.organization import bootstrap
from agent_workshop.reconcile import remote
from novel_factory.application.kernel import Kernel
from novel_factory.fixtures import load_fixture
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.objects import ObjectStore
from test_dual_stdio import bound_process


def test_author_wakeup_recovers_through_real_nf_project_binding(
    dual_databases, tmp_path, record_property
):
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
        project = bound_process(
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
        org, installation = str(uuid4()), str(uuid4())
        with connect(dual_databases["aw"]) as c:
            c.execute(
                "INSERT INTO aw_installations VALUES(%s,%s,true)", (installation, str(uuid4()))
            )
            bootstrap(c, org, installation_id=installation)
            token = enroll(c, installation, org, "author", 1, 1)
        aw = remote(
            bound_process(
                tmp_path,
                "agent_workshop.server",
                dict(token=token, database_url=dual_databases["aw"]),
                Path(__file__).resolve().parents[1] / "src",
            )
        )

        # Invoke the installed broker console script, as the systemd oneshot does.
        # A marker catches an attempted idle launch even when a peer error is handled.
        workshop = Path(__file__).resolve().parents[1]
        aw_python = Path(os.environ.get("AW_W0_PYTHON", workshop / ".venv/bin/python"))
        broker = aw_python.with_name("aw-manager-run")
        marker = tmp_path / "unexpected-peer-start"
        unused = [
            sys.executable,
            "-c",
            "from pathlib import Path; import sys; Path(sys.argv[1]).touch()",
            str(marker),
        ]
        route = tmp_path / "route.json"
        config = dict(
            org_id=org,
            installation_id=installation,
            project=[project.command, *project.args],
            delegation=unused,
        )
        with os.fdopen(os.open(route, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w") as f:
            json.dump(config, f)
        env = {"PATH": os.defpath, "HOME": str(tmp_path), "AW_DATABASE_URL": dual_databases["aw"]}

        def broker_once():
            return subprocess.run(
                [str(broker), "--route", str(route)],
                cwd=tmp_path,
                env=env,
                capture_output=True,
                text=True,
                timeout=40,
                check=True,
            ).stdout.strip()

        args = {"operation_id": "resume-after-restart"}
        req = asyncio.run(aw("aw_resume_project", args))
        assert req["state"] == "PENDING"
        assert broker_once() == "DONE"
        # Restart the worker without an NF launcher. With no work it must not touch a peer.
        route.write_text(json.dumps({**config, "project": unused}))
        assert broker_once() == "IDLE"
        assert not marker.exists()
        done = asyncio.run(aw("aw_resume_project", args))
        assert (
            done["request_id"] == req["request_id"] and done["result"]["workflow_state"] == "IDLE"
        )
        record_property(
            "installed_manager_resume",
            json.dumps(
                {
                    "request_id": req["request_id"],
                    "states": ["PENDING", "DONE", "IDLE"],
                    "peer_launched_while_idle": marker.exists(),
                    "nf_workflow": done["result"]["workflow_state"],
                },
                sort_keys=True,
            ),
        )
        with kernel.db.uow(write=False) as repo:
            assert not repo.many("tasks", {"work_id": wr["work_id"]})
    finally:
        db.close()
