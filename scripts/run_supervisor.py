"""Run the deterministic project scheduler. A stopped service resumes on restart."""
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
import argparse
import json
import os
from pathlib import Path
import time

import psycopg

from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.kernel import Kernel
from novel_factory.application.supervisor import HostJournal, RuntimeSupervisor
from novel_factory.domain.errors import DomainError
from novel_factory.infrastructure.codex_host import CodexAppServerAdapter, CodexRoleContextAdapter
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.model_gateway import OpenAICompatibleAdapter
from novel_factory.infrastructure.objects import ObjectStore
from novel_factory.infrastructure.supervisor_wake import wait_for_change, wake_listener


def log_result(result):
    print(json.dumps({key: result[key] for key in ("status", "reason", "task_id", "generation", "counts")
                      if key in result}), flush=True)


def serve(supervisor, principal, work, branch, *, workers, interval):
    pending = set()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        while True:
            try:
                with wake_listener(supervisor.kernel.db.engine) as listener:
                    _drain(supervisor, principal, work, branch, pool, workers, pending, listener, interval)
            except psycopg.OperationalError:
                # A new LISTEN session always begins with a durable scan.
                # Already admitted futures remain owned by this pool.
                print(json.dumps({"status": "RECONNECTING", "reason": "LISTENER_UNAVAILABLE"}), flush=True)
                time.sleep(min(interval, 5))


def _drain(supervisor, principal, work, branch, pool, workers, pending, listener, interval):
    while True:
        for future in tuple(pending):
            if future.done():
                pending.remove(future)
                log_result(future.result())
        if len(pending) == workers:
            wait(pending, return_when=FIRST_COMPLETED)
            continue
        result = supervisor.run_once(principal, work, branch, executor=pool)
        if isinstance(result, Future):
            pending.add(result)
        elif result["status"] == "IDLE":
            wait_for_change(listener, interval)
        else:
            log_result(result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--access-file", required=True, type=Path, help="Private JSON with token, work_id and branch_id")
    parser.add_argument("--interval", type=float, default=60, help="Maximum seconds between durable reconciliations")
    parser.add_argument("--workers", type=int, default=2, help="Maximum concurrent Host tasks in this process")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--enroll", type=Path, help="Private JSON with task_id, fixed input/manifest refs, cut and budget")
    mode.add_argument("--register-next-volume", type=Path, help="Preauthorize one next-volume planner on continuity publication")
    mode.add_argument("--register-scene", type=Path, help="Register a bounded scene from an authorized private JSON file")
    args = parser.parse_args()
    if not .1 <= args.interval <= 60:
        parser.error("interval must be between 0.1 and 60 seconds")
    if not 1 <= args.workers <= 16:
        parser.error("workers must be between 1 and 16")
    access = json.loads(args.access_file.read_text())
    db = Database(pool_size=max(5, args.workers + 2))
    try:
        kernel = Kernel(db, ObjectStore(os.environ.get("NF_OBJECT_ROOT", "var/objects")))
        principal = kernel.authenticate(access["token"])
        backend = OpenAICompatibleAdapter.from_environment()
        journal = HostJournal(kernel)
        adapters = {"external": backend, "codex-text": CodexAppServerAdapter(backend, journal)}
        if relay := os.environ.get("NF_ROLE_MCP_RELAY"):
            from novel_factory.interfaces.role_context_host import read_context
            adapters["codex-role-mcp"] = CodexRoleContextAdapter(backend, journal, relay=relay, context_reader=read_context)
        gateway = AgentGateway(kernel, adapters)
        supervisor = RuntimeSupervisor(kernel, gateway)
        if args.register_scene or args.register_next_volume:
            from novel_factory.application.scene_runtime import register_scene
            from novel_factory.application.volume_successor import register_next_volume
            source = args.register_scene or args.register_next_volume
            scene = json.loads(source.read_text())
            if not isinstance(scene, dict) or not isinstance(scene.get("world_ref"), dict) or any(
                    scene["world_ref"].get(key) != access[key] for key in ("work_id", "branch_id")):
                parser.error("Scene must belong to the configured work and branch")
            register = register_scene if args.register_scene else register_next_volume
            print(json.dumps(register(kernel, gateway, principal, **scene), ensure_ascii=False))
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
        if args.once:
            log_result(supervisor.run_once(principal, access["work_id"], access["branch_id"]))
            return
        serve(supervisor, principal, access["work_id"], access["branch_id"],
              workers=args.workers, interval=args.interval)
    except DomainError as exc:
        raise SystemExit(exc.code) from None
    finally:
        db.close()


if __name__ == "__main__":
    main()
