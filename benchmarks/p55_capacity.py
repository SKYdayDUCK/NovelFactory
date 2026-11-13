"""Measure fixed vs adaptive scoped ComputeWorker on real, precommitted world revisions."""
import argparse
import json
import multiprocessing
import os
import platform
import tempfile
import time
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import text

from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.kernel import Kernel
from novel_factory.application.supervisor import RuntimeSupervisor
from novel_factory.fixtures import load_fixture, point, run_command
from novel_factory.infrastructure.capacity import AdaptiveController, ResourceMonitor, percentile
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.objects import ObjectStore
from novel_factory.orchestration.processors import TaskDispatcher, initialize_index_worker


def initialize_warm_worker(url, objects, ready):
    initialize_index_worker(url, objects)
    # Every spawned process completes its actual PG warm-up before jobs are timed.
    warm()
    ready.wait(timeout=60)


def warm():
    # Warm spawned Python interpreter and SQL driver before any measured interval.
    from novel_factory.orchestration.processors import _index_db
    with _index_db.uow(write=False) as repo:
        repo.conn.exec_driver_sql("SELECT 1")
    time.sleep(.05)
    return os.getpid()


def trial(kernel, jobs, workers, adaptive, *, measure_resize=False):
    fixture = load_fixture(kernel, "white_bridge")
    principal, wr, ids = fixture["principal"], fixture["world_ref"], fixture["ids"]
    for n in range(jobs):
        wr = run_command(kernel, principal, wr, "RESOURCE_TRANSFER", {
            "from_account": ids["warehouse"], "to_account": ids["relief"], "resource_id": ids["grain"],
            "unit": "bag", "quantity": "1", "at": point(30 + n)})["world_ref"]
    db = kernel.db
    dispatcher = TaskDispatcher(RuntimeSupervisor(kernel, AgentGateway(kernel, {})), principal, wr["work_id"], wr["branch_id"])
    dispatcher.switch("redis")
    controller = AdaptiveController(db, "compute", workers, "cpu", work=wr["work_id"], branch=wr["branch_id"], interval=.15)
    with db.uow(write=False) as repo:
        source = repo.conn.scalar(text("""SELECT count(*) FROM nf_outbox WHERE topic='world.accepted'
            AND payload->'world_ref'->>'branch_id'=:branch"""), {"branch": wr["branch_id"]})
    assert source == jobs
    completions, service_times, limits, pressure, pending = [], [], [], [], {}
    done_ids = set()
    records = []
    monitor = ResourceMonitor()
    ctx = multiprocessing.get_context("spawn")
    ready = ctx.Barrier(workers + 1)
    with ProcessPoolExecutor(max_workers=workers, mp_context=ctx,
            initializer=initialize_warm_worker, initargs=(db.engine.url.render_as_string(hide_password=False),
                                                         str(kernel.objects.root), ready)) as pool:
        # Keep all bootstrap jobs outstanding until every initializer is ready.
        started = [pool.submit(warm_noarg, i) for i in range(workers)]
        ready.wait(timeout=60)
        for job in started:
            job.result(timeout=60)
        start = time.monotonic()
        monitor.host()
        controller.monitor.host()
        next_host = start
        while len(completions) < source:
            now = time.monotonic()
            if now - start > 90:
                raise TimeoutError("Index worker failed to drain within 90 seconds")
            limit = controller.limit() if adaptive else workers
            if now >= next_host:
                sample = monitor.host()
                pressure.append({k: sample[k] for k in ("cpu_pct", "cpu_psi", "rss_kib", "available_bytes", "throttle_pct")})
                limits.append([round(now - start, 3), limit])
                next_host = now + .1
            while len(pending) < limit and len(done_ids) + len(pending) < source:
                submitted = time.monotonic()
                result = dispatcher.run_once("compute", executor=pool, reconcile=True)
                if isinstance(result, dict):
                    break
                pending[result] = submitted
            if not pending:
                time.sleep(.01)
                continue
            finished, _ = wait(pending, timeout=.1, return_when=FIRST_COMPLETED)
            for future in finished:
                completed = time.monotonic()
                service = completed - pending.pop(future)
                receipt = future.result()
                assert receipt["status"] == "INDEXED" and receipt["message_id"] not in done_ids
                done_ids.add(receipt["message_id"])
                service_times.append(service)
                completions.append(completed - start)
                records.append({"source_event": receipt["message_id"], "completed_s": completed - start,
                                "service_s": service, "status": receipt["status"]})
                controller.monitor.completed(service)
        elapsed = time.monotonic() - start
        resize = []
        if measure_resize and adaptive:
            # Sample the real monitor after the last receipt. Stop as soon as
            # admission returns to the minimum or after bounded observation.
            drained = time.monotonic()
            while True:
                limit = controller.limit()
                age = time.monotonic() - drained
                resize.append([age, limit])
                if limit == controller.policy.minimum or age >= 5:
                    break
                time.sleep(controller.interval)
    with db.uow(write=False) as repo:
        assert repo.one("index_watermarks", {"branch_id": wr["branch_id"], "index_name": "timeline"})["world_revision_id"] == wr["world_revision_id"]
        calls = repo.conn.scalar(text("""SELECT count(*) FROM nf_call_attempts c JOIN nf_tasks t USING(task_id)
            WHERE t.branch_id=:b"""), {"b": wr["branch_id"]})
        decisions = repo.conn.execute(text("""SELECT previous,target,reason FROM nf_scale_decisions
            WHERE instance=:instance ORDER BY id"""), {"instance": controller.instance}).mappings().all()
    assert not calls
    def avg(key):
        vals = [row[key] for row in pressure if row[key] is not None]
        return round(sum(vals)/len(vals), 3) if vals else None
    return {"mode": "adaptive" if adaptive else "fixed", "jobs": jobs, "successful": len(done_ids), "failures": 0,
            "elapsed_s": elapsed, "jobs_per_s": jobs / elapsed, "mean_completion_s": sum(completions)/jobs,
            "p95_completion_s": percentile(completions,.95), "p99_completion_s": percentile(completions,.99),
            "p95_service_s": percentile(service_times,.95), "mean_cpu_pct": avg("cpu_pct"), "mean_cpu_psi_avg10": avg("cpu_psi"),
            "mean_parent_rss_kib": avg("rss_kib"), "mean_available_bytes": avg("available_bytes"),
            "warm_processes": workers, "capacity_series": limits, "scale_decisions": [dict(r) for r in decisions],
            "post_drain_targets": resize, "provider_calls": calls, "provider_invoice": None, "records": records}


def warm_noarg(_):
    return warm()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--jobs", type=int, default=24)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--measure-resize", action="store_true", help="record the real admission target after drain")
    args = parser.parse_args()
    if not 4 <= args.jobs <= 60 or not 1 <= args.workers <= 2:
        parser.error("Benchmark fixture supports 4–60 transfers and up to two default database slots")
    url = os.environ["NF_TEST_DATABASE_URL"]
    db = Database(url, pool_size=3)
    try:
        with tempfile.TemporaryDirectory() as directory:
            kernel = Kernel(db, ObjectStore(directory))
            # Symmetric ordering across two paired repeats.
            trials = [trial(kernel,args.jobs,args.workers,adaptive,measure_resize=args.measure_resize) for adaptive in (False, True, True, False)]
        import subprocess
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
        output = {"source_commit": commit, "measured_at": datetime.now(UTC).isoformat(), "system": platform.platform(),
                  "python": platform.python_version(), "affinity": len(os.sched_getaffinity(0)),
                  "postgres": db.engine.dialect.server_version_info, "load": "closed loop drain of simultaneously READY fixed-shape world revisions",
                  "model": "no provider call or synthetic usage", "parent_pool": 3, "child_pool_per_process": 1,
                  "cpu_quota_reported": ResourceMonitor().host()["quota_cpus"], "trials": trials}
        args.output.parent.mkdir(exist_ok=True, parents=True)
        args.output.write_text(json.dumps(output,indent=2) + "\n")
    finally:
        db.close()


if __name__ == "__main__":
    main()
