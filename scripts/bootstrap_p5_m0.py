"""Prepare an isolated, two-turn P5 sample project without starting a model."""
import argparse
from datetime import UTC, datetime, timedelta
import fcntl
import json
import os
from pathlib import Path
import stat
import tempfile
from uuid import uuid4

from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.kernel import Kernel
from novel_factory.application.runtime_tasks import RuntimeTaskService
from novel_factory.application.supervisor import HostJournal, RuntimeSupervisor
from novel_factory.fixtures import load_fixture, point
from novel_factory.infrastructure.codex_host import CodexAppServerAdapter
from novel_factory.infrastructure.database import Database, tables
from novel_factory.infrastructure.model_gateway import OpenAICompatibleAdapter
from novel_factory.infrastructure.objects import ObjectStore
from novel_factory.infrastructure.repository import KernelRepository


def save(directory, name, value):
    fd, tmp = tempfile.mkstemp(dir=directory)
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(value, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(tmp, directory / name)
        handle = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(handle)
        finally:
            os.close(handle)
    finally:
        Path(tmp).unlink(missing_ok=True)


def provision(db, store, private, route_name, hours):
    marker_path = private / "sample.json"
    if not marker_path.exists():
        if any(f.name != ".lock" for f in private.iterdir()):
            raise ValueError("A new sample requires an empty private directory")
        save(private, "sample.json", {"schema": "nf-m0-1", "work_id": str(uuid4()),
             "database": db.engine.url.render_as_string(hide_password=True), "objects": str(store.root)})
    marker = json.loads(marker_path.read_text())
    if (marker.get("schema") != "nf-m0-1" or marker["database"] != db.engine.url.render_as_string(hide_password=True)
            or marker["objects"] != str(store.root)):
        raise ValueError("Sample state belongs to another database or object root")

    # This bounded bootstrap does only local I/O. Use the same transaction for
    # the ordinary kernel services and commit only after both credentials are
    # durably installed. An interrupted pre-commit bootstrap rolls back in PG.
    with db.uow() as repo:
        existing = repo.one("works", {"work_id": marker["work_id"]})
        if existing:
            access = json.loads((private / "supervisor.json").read_text())
            verification = Kernel(db, store)
            principal = verification.authenticate(access["token"])
            verification.authorize(repo, principal, marker["work_id"], "ADMIN", access["branch_id"])
            return {"status": "EXISTS", "work_id": marker["work_id"], "branch_id": access["branch_id"]}
        kernel = Kernel(db, store).in_transaction(repo)
        world = load_fixture(kernel, "r1_sword", work_id=marker["work_id"])
        wr, controller = world["world_ref"], world["principal"]
        work, branch = wr["work_id"], wr["branch_id"]
        backend = OpenAICompatibleAdapter.from_environment()
        route = CodexAppServerAdapter(backend, HostJournal(kernel)) if route_name == "codex-text" else backend
        gateway = AgentGateway(kernel, {route_name: route})
        supervisor = RuntimeSupervisor(kernel, gateway)
        budget = kernel.tasks.create_budget(work, "2")
        task_ids = []
        for initial, assignment in (
            ("READY", "根据当前可见的持有关系撰写一条有证据的摘要。"),
            ("BLOCKED", "在前序摘要任务结束后，独立复核当前可见的物品归属。"),
        ):
            job = RuntimeTaskService(kernel, gateway).prepare(controller, wr, role_id="evidence_maintainer",
                task_kind="summary", story_time=point(1), subject_ids=[world["ids"]["sword"]],
                predicates=["owner"], assignment=assignment, adapter_id=route_name, queue_state=initial,
                deadline_seconds=86400)
            task_id = job.task["task_id"]
            supervisor.enroll(controller, task_id, manifest_ref=job.manifest_ref, knowledge_cut={"plane": "WORLD"},
                context_tokens=20000, input_ref=job.agent_input["input_ref"], budget_id=budget,
                reserve_amount="1", token_reservation=100000, accept_summary=True)
            task_ids.append(task_id)

        def authorize(conn):
            kernel.authorize(KernelRepository(conn, tables), controller, work, "ADMIN", branch)
        supervisor.workflow.wait(task_ids[1], 0, [task_ids[0]], mode="AND", work_id=work, branch_id=branch,
                                 authorize_in_tx=authorize)
        granted = kernel.issue_grant(controller, work, role_id="resume_operator", branch_ids=[branch],
                     capabilities=["PROJECT_RESUME"], expires_at=datetime.now(UTC) + timedelta(hours=hours))
        scope = {"work_id": work, "branch_id": branch}
        save(private, "supervisor.json", {**scope, "token": world["token"]})
        save(private, "resume.json", {"binding": scope, "token": granted["token"], "object_root": str(store.root),
                                      "database_url": db.engine.url.render_as_string(hide_password=False)})
        return {"status": "CREATED", **scope, "producer": task_ids[0], "waiting": task_ids[1]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-dir", type=Path, required=True)
    parser.add_argument("--route", choices=("codex-text", "external"), default="codex-text")
    parser.add_argument("--hours", type=int, default=24, help="Resume grant lifetime")
    args = parser.parse_args()
    if not 1 <= args.hours <= 24:
        parser.error("The sample's one-day task deadline requires 1–24 hours")
    private = args.private_dir
    private.mkdir(mode=0o700, parents=True, exist_ok=True)
    meta = private.lstat()
    if not stat.S_ISDIR(meta.st_mode) or meta.st_uid != os.getuid() or stat.S_IMODE(meta.st_mode) != 0o700:
        parser.error("Use an owned 0700 private directory")
    db = Database()
    try:
        with os.fdopen(os.open(private / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            print(json.dumps(provision(db, ObjectStore(os.environ.get("NF_OBJECT_ROOT", "var/objects")),
                                       private, args.route, args.hours)))
    finally:
        db.close()


if __name__ == "__main__":
    main()
