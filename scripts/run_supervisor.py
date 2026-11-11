"""Run the deterministic project scheduler. A stopped service resumes on restart."""
import argparse
import json
import os
from pathlib import Path
import time

from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.kernel import Kernel
from novel_factory.application.supervisor import HostJournal, RuntimeSupervisor
from novel_factory.domain.errors import DomainError
from novel_factory.infrastructure.codex_host import CodexAppServerAdapter
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.model_gateway import OpenAICompatibleAdapter
from novel_factory.infrastructure.objects import ObjectStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--access-file", required=True, type=Path, help="Private JSON with token, work_id and branch_id")
    parser.add_argument("--interval", type=float, default=1)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--enroll", type=Path, help="Private JSON with task_id, fixed input/manifest refs, cut and budget")
    mode.add_argument("--register-scene", type=Path, help="Register a bounded scene from an authorized private JSON file")
    args = parser.parse_args()
    if not .1 <= args.interval <= 60:
        parser.error("interval must be between 0.1 and 60 seconds")
    access = json.loads(args.access_file.read_text())
    db = Database()
    try:
        kernel = Kernel(db, ObjectStore(os.environ.get("NF_OBJECT_ROOT", "var/objects")))
        principal = kernel.authenticate(access["token"])
        backend = OpenAICompatibleAdapter.from_environment()
        gateway = AgentGateway(kernel, {"external": backend,
            "codex-text": CodexAppServerAdapter(backend, HostJournal(kernel))})
        supervisor = RuntimeSupervisor(kernel, gateway)
        if args.register_scene:
            from novel_factory.application.scene_runtime import register_scene
            scene = json.loads(args.register_scene.read_text())
            if not isinstance(scene, dict) or not isinstance(scene.get("world_ref"), dict) or any(
                    scene["world_ref"].get(key) != access[key] for key in ("work_id", "branch_id")):
                parser.error("Scene must belong to the configured work and branch")
            print(json.dumps(register_scene(kernel, gateway, principal, **scene), ensure_ascii=False))
            return
        if args.enroll:
            enrollment = json.loads(args.enroll.read_text())
            with db.uow(write=False) as repo:
                kernel.authorize(repo, principal, access["work_id"], "ADMIN", access["branch_id"])
                task = repo.one("tasks", {"work_id": access["work_id"], "branch_id": access["branch_id"],
                                          "task_id": enrollment["task_id"]})
                if task is None:
                    raise DomainError("VIEW_DENIED", "Task outside the configured project")
            enrolled = supervisor.enroll(principal, **enrollment)
            print(json.dumps(enrolled))
            return
        while True:
            result = supervisor.run_once(principal, access["work_id"], access["branch_id"])
            if args.once or result["status"] != "IDLE":
                print(json.dumps({k: result[k] for k in ("status", "reason", "task_id", "generation", "counts")
                                  if k in result}), flush=True)
            if args.once:
                return
            if result["status"] == "IDLE":
                time.sleep(args.interval)
    except DomainError as exc:
        raise SystemExit(exc.code) from None
    finally:
        db.close()


if __name__ == "__main__":
    main()
