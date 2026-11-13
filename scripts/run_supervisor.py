"""Run the deterministic project scheduler. A stopped service resumes on restart."""
import argparse
import json
import multiprocessing
import os
import signal
import time
from concurrent.futures import (
    FIRST_COMPLETED,
    Future,
    ProcessPoolExecutor,
    ThreadPoolExecutor,
    wait,
)
from pathlib import Path
from threading import Event

import psycopg
from sqlalchemy.exc import OperationalError, TimeoutError as PoolTimeout

from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.kernel import Kernel
from novel_factory.application.production_pipeline import production_material_reader
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
from novel_factory.infrastructure.private_files import read_private_bytes
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


def serve(supervisor, principal, work, branch, *, workers, interval, processor=None, controller=None, observation_seconds=60, stop_event=None, control_while_busy=False):
    stop_event = stop_event if stop_event is not None else Event()
    pending = {}
    windows = None
    if processor in (None, "model", "compute"):
        windows = PoolWindows(supervisor.kernel.db, processor or "model", "project_executor",
                              controller.policy.target if controller else workers, observation_seconds,
                              work=work, branch=branch, instance=controller.instance if controller else None)
        if controller:
            controller.monitor.occupancy = windows.meter
    while not stop_event.is_set():
        if processor == "compute":
            url = supervisor.kernel.db.engine.url.render_as_string(hide_password=False)
            pool = ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("spawn"),
                                       initializer=initialize_index_worker,
                                       initargs=(url, str(supervisor.kernel.objects.root), windows.meter))
        else:
            pool = ThreadPoolExecutor(max_workers=workers)
        execution = MeasuredThreadExecutor(pool, windows.meter) if windows and processor != "compute" else pool
        with pool:
            while not stop_event.is_set():
                try:
                    with wake_listener(supervisor.kernel.db.engine) as listener:
                        retire = _drain(supervisor, principal, work, branch, execution, workers, pending, listener,
                                        interval, processor, controller, windows, stop_event, control_while_busy)
                    if retire:
                        break
                except (psycopg.OperationalError, OperationalError, PoolTimeout):
                    # Reopen LISTEN, then rescan durable state. Keep admitted futures in this pool.
                    print(json.dumps({"status": "RECONNECTING", "reason": "DATABASE_UNAVAILABLE"}), flush=True)
                    stop_event.wait(min(interval, 5))


def _drain(supervisor, principal, work, branch, pool, workers, pending, listener, interval, processor, controller, windows, stop_event, control_while_busy):
    dispatcher = TaskDispatcher(supervisor, principal, work, branch) if processor else None
    next_reconciliation = 0.0
    next_busy_scan = 0.0
    busy_signaled = True
    last_busy_scan = float("-inf")
    used_pool = bool(pending)
    while not stop_event.is_set():
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
        if (used_pool and not pending and controller and controller.policy.idle_since is not None
                and time.monotonic() - controller.policy.idle_since >= 2 * controller.policy.cooldown):
            # No admitted execution owns the pool. Exit its context and release
            # native threads/processes; the replacement spawns on first submit.
            return True
        if windows:
            windows.tick(target)
        if len(pending) >= target:
            timeout = min(windows.timeout(), controller.interval if controller else interval) if windows else interval
            if control_while_busy and not processor:
                now = time.monotonic()
                if now >= next_busy_scan or (busy_signaled and now - last_busy_scan >= .1):
                    # The bounded control path can close expired jobs and advance durable
                    # dependencies even when every local model worker is still waiting.
                    result = supervisor.run_once(principal, work, branch, admit_model=False)
                    if result["status"] != "IDLE":
                        log_result(result)
                    last_busy_scan = now
                    next_busy_scan = now + interval
                    busy_signaled = False
                timeout = min(timeout, max(.001, next_busy_scan - time.monotonic()), 1)
                busy_signaled = wait_for_change(listener, timeout) or busy_signaled
            else:
                wait(pending, timeout=min(timeout, 1), return_when=FIRST_COMPLETED)
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
            used_pool = True
        elif result["status"] == "IDLE":
            timeout = min(interval, windows.timeout()) if windows else interval
            if "retry_after_seconds" in result:
                timeout = min(timeout, result["retry_after_seconds"])
                next_reconciliation = min(next_reconciliation, time.monotonic() + timeout)
            # Slot releases and new work both publish commit-time wake hints.
            # Check local futures between socket waits, keeping a quiet provider
            # call from triggering repeated durable scans while slots are free.
            until = time.monotonic() + timeout
            while not stop_event.is_set() and (remaining := until - time.monotonic()) > 0:
                if any(future.done() for future in pending) or wait_for_change(listener, min(remaining, 1)):
                    break
        else:
            log_result(result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--access-file", required=True, type=Path, help="Private JSON with token, work_id and branch_id")
    parser.add_argument("--interval", type=float, default=60, help="Maximum seconds between durable reconciliations")
    parser.add_argument("--workers", type=int, default=2, help="Maximum concurrent Host tasks in this process")
    parser.add_argument("--db-pool-size", type=int, default=5,
                        help="Maximum parent database pool connections; the listener uses one separate connection")
    parser.add_argument("--processor", choices=SCOPED, help="Run this Redis-mode project processor")
    parser.add_argument("--adaptive", action="store_true", help="Adjust new admissions under the fixed worker ceiling")
    parser.add_argument("--control-while-busy", action="store_true",
                        help="Continue bounded world and recovery scans with all local model slots occupied")
    parser.add_argument("--sample-seconds", type=float, default=3)
    parser.add_argument("--observation-seconds", type=float, default=60, help="Worker utilization reporting window")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--set-transport", choices=("postgres", "redis"), help="Set admission mode for the authorized branch")
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--enroll", type=Path, help="Private JSON with task_id, fixed input/manifest refs, cut and budget")
    mode.add_argument("--register-next-volume", type=Path, help="Preauthorize one next-volume planner on continuity publication")
    mode.add_argument("--register-scene", type=Path, help="Register a bounded scene from an authorized private JSON file")
    mode.add_argument("--register-pipeline", type=Path, help="Register chapter drafts, reviews and bounded future windows")
    mode.add_argument("--pipeline-status", action="store_true", help="Inspect production handoffs for this work and branch")
    mode.add_argument("--pipeline-results", action="store_true", help="Export this branch's exact draft text, advisory opinions and plan candidates")
    mode.add_argument("--revise-window", type=Path, help="Authorize a bounded new revision for one invalidated candidate")
    mode.add_argument("--transfer-window", type=Path, help="Submit a revalidated candidate window to native planning")
    args = parser.parse_args()
    if not .1 <= args.interval <= 60:
        parser.error("interval must be between 0.1 and 60 seconds")
    if not 1 <= args.workers <= 16:
        parser.error("workers must be between 1 and 16")
    if not 1 <= args.db_pool_size <= 20:
        parser.error("db-pool-size must be between 1 and 20")
    if not .5 <= args.observation_seconds <= 3600:
        parser.error("observation-seconds must be between 0.5 and 3600")
    if not .1 <= args.sample_seconds <= 60:
        parser.error("sample-seconds must be between 0.1 and 60")
    if args.processor and args.workers > PROCESSORS[args.processor].max_workers:
        parser.error("workers exceeds this processor limit")
    access = json.loads(read_private_bytes(args.access_file, 16384))
    db = Database(pool_size=args.db_pool_size)
    try:
        kernel = Kernel(db, ObjectStore(os.environ.get("NF_OBJECT_ROOT", "var/objects")))
        principal = kernel.authenticate(access["token"])
        adapters = {}
        model_needed = not (args.set_transport or args.pipeline_status or args.pipeline_results or args.transfer_window or args.revise_window) and args.processor not in {"world", "compute", "recovery"}
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
        gateway = AgentGateway(kernel, adapters, material_reader=production_material_reader(kernel))
        supervisor = RuntimeSupervisor(kernel, gateway)
        if args.set_transport:
            print(json.dumps(TaskDispatcher(supervisor, principal, access["work_id"], access["branch_id"]).switch(args.set_transport)))
            return
        if args.pipeline_status or args.pipeline_results:
            from novel_factory.application.production_pipeline import pipeline_status
            print(json.dumps(pipeline_status(kernel, principal, access["work_id"], access["branch_id"],
                                             include_outputs=args.pipeline_results), ensure_ascii=False))
            return
        if args.register_pipeline or args.transfer_window or args.revise_window:
            from novel_factory.application.production_pipeline import register_pipeline, revise_window, transfer_window
            payload = json.loads((args.register_pipeline or args.transfer_window or args.revise_window).read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or not isinstance(payload.get("world_ref"), dict) or any(
                    payload["world_ref"].get(k) != access[k] for k in ("work_id", "branch_id")):
                parser.error("Pipeline must belong to the configured work and branch")
            if args.register_pipeline:
                result = register_pipeline(kernel, gateway, principal, **payload)
            else:
                operation = transfer_window if args.transfer_window else revise_window
                result = operation(kernel, principal, **payload)
            print(json.dumps(result, ensure_ascii=False))
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
        stop_event = Event()

        def request_stop(signum, frame):
            stop_event.set()

        signals = [signal.SIGINT, signal.SIGTERM]
        if hasattr(signal, "SIGBREAK"):
            signals.append(signal.SIGBREAK)
        previous = {sig: signal.signal(sig, request_stop) for sig in signals}
        try:
            serve(supervisor, principal, access["work_id"], access["branch_id"],
                  workers=args.workers, interval=args.interval, processor=args.processor, controller=controller,
                  observation_seconds=args.observation_seconds, stop_event=stop_event,
                  control_while_busy=args.control_while_busy)
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
    except DomainError as exc:
        raise SystemExit(exc.code) from None
    finally:
        db.close()


if __name__ == "__main__":
    main()
