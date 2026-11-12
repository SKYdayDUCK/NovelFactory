"""Run the deterministic project scheduler. A stopped service resumes on restart."""
import argparse
import json
import multiprocessing
import os
import time
from concurrent.futures import (
    FIRST_COMPLETED,
    Future,
    ProcessPoolExecutor,
    ThreadPoolExecutor,
    wait,
)
from pathlib import Path

import psycopg

from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.kernel import Kernel
from novel_factory.application.supervisor import HostJournal, RuntimeSupervisor
from novel_factory.domain.errors import DomainError
from novel_factory.infrastructure.capacity import (
    AdaptiveController,
    MeasuredThreadExecutor,
    PoolWindows,
)
from novel_factory.infrastructure.codex_host import (
    CodexAppServerAdapter,
    CodexRoleContextAdapter,
    CodexRoleLookupAdapter,
)
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.model_gateway import OpenAICompatibleAdapter
from novel_factory.infrastructure.objects import ObjectStore
from novel_factory.infrastructure.supervisor_wake import wait_for_change, wake_listener
from novel_factory.orchestration.processors import (
    PROCESSORS,
    SCOPED,
    TaskDispatcher,
    initialize_index_worker,
)


def log_result(result):
    print(json.dumps({key: result[key] for key in ("status", "reason", "task_id", "generation", "counts")
                      if key in result}), flush=True)


def serve(supervisor, principal, work, branch, *, workers, interval, processor=None, controller=None, observation_seconds=60):
    pending = {}
    windows = None
    if processor in (None, "model", "compute"):
        windows = PoolWindows(supervisor.kernel.db, processor or "model", "project_executor",
                              controller.policy.target if controller else workers, observation_seconds,
                              work=work, branch=branch, instance=controller.instance if controller else None)
        if controller:
            controller.monitor.occupancy = windows.meter
    if processor == "compute":
        url = supervisor.kernel.db.engine.url.render_as_string(hide_password=False)
        pool = ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("spawn"),
                                   initializer=initialize_index_worker, initargs=(url, str(supervisor.kernel.objects.root), windows.meter))
    else:
        pool = ThreadPoolExecutor(max_workers=workers)
    execution = MeasuredThreadExecutor(pool, windows.meter) if windows and processor != "compute" else pool
    with pool:
        while True:
            try:
                with wake_listener(supervisor.kernel.db.engine) as listener:
                    _drain(supervisor, principal, work, branch, execution, workers, pending, listener, interval, processor, controller, windows)
            except psycopg.OperationalError:
                # A new LISTEN session always begins with a durable scan.
                # Already admitted futures remain owned by this pool.
                print(json.dumps({"status": "RECONNECTING", "reason": "LISTENER_UNAVAILABLE"}), flush=True)
                time.sleep(min(interval, 5))


def _drain(supervisor, principal, work, branch, pool, workers, pending, listener, interval, processor=None, controller=None, windows=None):
    dispatcher = TaskDispatcher(supervisor, principal, work, branch) if processor else None
    next_reconciliation = 0.0
    while True:
        for future in tuple(pending):
            if future.done():
                started = pending.pop(future)
                try:
                    result = future.result()
                except DomainError as exc:
                    result = {"status": "BLOCKED", "reason": exc.code}
                if controller:
                    controller.monitor.completed(time.monotonic() - started, result["status"] not in {"BLOCKED", "FAILED"})
                log_result(result)
        target = controller.limit() if controller else workers
        if windows:
            windows.tick(target)
        if len(pending) >= target:
            timeout = min(windows.timeout(), controller.interval if controller else interval) if windows else interval
            wait(pending, timeout=timeout, return_when=FIRST_COMPLETED)
            continue
        if dispatcher:
            now = time.monotonic()
            reconcile = now >= next_reconciliation
            if reconcile:
                next_reconciliation = now + interval
            result = dispatcher.run_once(processor, executor=pool, reconcile=reconcile)
        else:
            result = supervisor.run_once(principal, work, branch, executor=pool)
        if isinstance(result, Future):
            pending[result] = time.monotonic()
        elif result["status"] == "IDLE":
            # Completion of CPU work, and changes in the target, can predate the periodic rescan.
            if pending:
                wait(pending, timeout=min(interval, .25), return_when=FIRST_COMPLETED)
            else:
                wait_for_change(listener, min(interval, windows.timeout()) if windows else interval)
        else:
            log_result(result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--access-file", required=True, type=Path, help="Private JSON with token, work_id and branch_id")
    parser.add_argument("--interval", type=float, default=60, help="Maximum seconds between durable reconciliations")
    parser.add_argument("--workers", type=int, default=2, help="Maximum concurrent Host tasks in this process")
    parser.add_argument("--processor", choices=SCOPED, help="Run this Redis-mode project processor")
    parser.add_argument("--adaptive", action="store_true", help="Adjust new admissions under the fixed worker ceiling")
    parser.add_argument("--sample-seconds", type=float, default=3)
    parser.add_argument("--observation-seconds", type=float, default=60, help="Worker utilization reporting window")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--set-transport", choices=("postgres", "redis"), help="Set admission mode for the authorized branch")
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--enroll", type=Path, help="Private JSON with task_id, fixed input/manifest refs, cut and budget")
    mode.add_argument("--register-next-volume", type=Path, help="Preauthorize one next-volume planner on continuity publication")
    mode.add_argument("--register-scene", type=Path, help="Register a bounded scene from an authorized private JSON file")
    args = parser.parse_args()
    if not .1 <= args.interval <= 60:
        parser.error("interval must be between 0.1 and 60 seconds")
    if not 1 <= args.workers <= 16:
        parser.error("workers must be between 1 and 16")
    if not .5 <= args.observation_seconds <= 3600:
        parser.error("observation-seconds must be between 0.5 and 3600")
    if not .1 <= args.sample_seconds <= 60:
        parser.error("sample-seconds must be between 0.1 and 60")
    if args.processor and args.workers > PROCESSORS[args.processor].max_workers:
        parser.error("workers exceeds this processor limit")
    access = json.loads(args.access_file.read_text())
    db = Database(pool_size=max(5, args.workers + 2))
    try:
        kernel = Kernel(db, ObjectStore(os.environ.get("NF_OBJECT_ROOT", "var/objects")))
        principal = kernel.authenticate(access["token"])
        adapters = {}
        model_needed = not args.set_transport and args.processor not in {"world", "compute", "recovery"}
        configured = os.environ.get("NF_MODEL_ENDPOINT") and os.environ.get("NF_MODEL_NAME")
        profiles_file = os.environ.get("NF_RUNTIME_PROFILES")
        if profiles_file:
            from novel_factory.infrastructure.runtime_providers import load_runtime_profiles
            adapters = load_runtime_profiles(profiles_file, HostJournal(kernel))
        if configured or (model_needed and not profiles_file):
            backend = OpenAICompatibleAdapter.from_environment()
            journal = HostJournal(kernel)
            legacy = {"external": backend, "codex-text": CodexAppServerAdapter(backend, journal)}
            if relay := os.environ.get("NF_ROLE_MCP_RELAY"):
                from novel_factory.interfaces.role_context_host import read_context
                legacy["codex-role-mcp"] = CodexRoleContextAdapter(backend, journal, relay=relay, context_reader=read_context)
                legacy["codex-role-tools"] = CodexRoleLookupAdapter(backend, journal, relay=relay, context_reader=read_context)
            if adapters.keys() & legacy.keys():
                raise DomainError("INVALID_MODEL_CONFIG", "Runtime profile collides with a configured legacy route")
            adapters.update(legacy)
        gateway = AgentGateway(kernel, adapters)
        supervisor = RuntimeSupervisor(kernel, gateway)
        if args.set_transport:
            print(json.dumps(TaskDispatcher(supervisor, principal, access["work_id"], access["branch_id"]).switch(args.set_transport)))
            return
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
            if args.processor:
                dispatcher = TaskDispatcher(supervisor, principal, access["work_id"], access["branch_id"])
                log_result(dispatcher.run_once(args.processor, reconcile=True))
            else:
                log_result(supervisor.run_once(principal, access["work_id"], access["branch_id"]))
            return
        processor = args.processor or "model"
        controller = AdaptiveController(db, processor, args.workers, PROCESSORS[processor].resource,
            work=access["work_id"], branch=access["branch_id"], interval=args.sample_seconds) if args.adaptive else None
        serve(supervisor, principal, access["work_id"], access["branch_id"],
              workers=args.workers, interval=args.interval, processor=args.processor, controller=controller, observation_seconds=args.observation_seconds)
    except DomainError as exc:
        raise SystemExit(exc.code) from None
    finally:
        db.close()


if __name__ == "__main__":
    main()
