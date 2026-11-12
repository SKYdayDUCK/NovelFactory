"""Isolated PostgreSQL comparison of authorized task status and trusted lease renewal."""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import threading
import time
from uuid import uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from benchmarks.http_load import percentiles, SqlMeasurements
from experiments.db_execution.coordinator import Coordinator
from experiments.db_execution.operations import LeaseRenewal, TaskStatus, execute
from novel_factory.application.kernel import Kernel
from novel_factory.fixtures import load_fixture
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.objects import ObjectStore

MODES = ("baseline", "queues", "sql", "micro", "micro_queues", "hybrid")
ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def private_kernel(workers):
    url = make_url(os.environ["NF_TEST_DATABASE_URL"])
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    name = "nf_db_batch_" + uuid4().hex
    with admin.connect() as conn:
        if int(conn.scalar(text("SHOW server_version_num"))) // 10000 != 17:
            raise ValueError("PostgreSQL 17 required")
        conn.exec_driver_sql(f'CREATE DATABASE "{name}"')
    target = url.set(database=name).render_as_string(hide_password=False)
    try:
        subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT,
                       env={**os.environ, "NF_DATABASE_URL": target, "DATABASE_URL": target}, check=True)
        db = Database(target, pool_size=workers, max_overflow=0)
        try:
            with tempfile.TemporaryDirectory() as objects:
                yield Kernel(db, ObjectStore(objects)), target
        finally:
            db.close()
    finally:
        with admin.connect() as conn:
            conn.exec_driver_sql(f'DROP DATABASE "{name}"')
        admin.dispose()


def task_fixture(kernel, book, principal, role_name, index):
    task_id = str(uuid4())
    spec = {"schema_version": "2.0", "task_id": task_id, "generation": 0, "role_id": role_name,
            "task_kind": "kernel_acceptance", "state": "READY", "world_ref": book["world_ref"],
            "story_time": {"tick": 0, "phase": 0}, "context_bundle_id": None, "policy_version": "nf-policy-1",
            "dependency_fingerprint": None, "runtime_contract_version": "a" * 64,
            "capability_grant_id": principal.grant_id, "input_artifacts": [],
            "budget": {"currency": "USD", "max_amount": "1", "token_budget": 1000},
            "wall_deadline": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()}
    kernel.create_task(principal, spec, f"experiment-seed-{index}")
    return TaskStatus(principal, task_id)


def seed(kernel, agents):
    books = [load_fixture(kernel) for _ in range(2)]
    writes = []
    for i, book in enumerate(books):
        owner = book["principal"]
        op = task_fixture(kernel, book, owner, "control_operator", i)
        worker = f"fixture-supervisor-{i}"
        claim = kernel.claim_task(owner, worker, lease_seconds=3600, task_id=op.task_id)
        writes.append(LeaseRenewal(op.task_id, claim["generation"], worker))
    reads, expected = [], []
    for i in range(agents):
        book = books[i % 2]
        identity = kernel.issue_grant(book["principal"], book["world_ref"]["work_id"], role_id="writer")["principal"]
        op = task_fixture(kernel, book, identity, "writer", i)
        reads.append(op)
        expected.append(kernel.get_task(identity, op.task_id))
    return reads, writes, expected


class Observer:
    def __init__(self, url):
        self.engine = create_engine(url, pool_size=1, max_overflow=0, isolation_level="AUTOCOMMIT")
        self.stop = threading.Event()
        self.ready = threading.Event()
        self.samples = []

    def run(self):
        with self.engine.connect() as conn:
            query = text("""SELECT count(*), count(*) FILTER (WHERE state='active'),
                     count(*) FILTER (WHERE wait_event_type='Lock') FROM pg_stat_activity
                     WHERE datname=current_database() AND pid<>pg_backend_pid()""")
            while not self.stop.is_set():
                n, active, locked = conn.execute(query).one()
                self.samples.append((time.monotonic(), n, active, locked))
                self.ready.set()
                self.stop.wait(.05)

    def __enter__(self):
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()
        if not self.ready.wait(5):
            raise RuntimeError("PostgreSQL observer did not start")
        return self

    def __exit__(self, *_):
        self.stop.set()
        self.thread.join(5)
        self.engine.dispose()


async def measure(kernel, url, fixture, args):
    reads, writes, expected = fixture
    kernel = Kernel(kernel.db, kernel.objects, joined_task_status=args.mode == "sql")
    pool = ThreadPoolExecutor(max_workers=args.workers)
    loop = asyncio.get_running_loop()
    queue_mode = args.mode not in ("baseline", "sql")
    coordinator = Coordinator(kernel, workers=args.workers, capacity=args.capacity,
                              batch=args.batch if args.mode in ("micro", "micro_queues", "hybrid") else 1,
                              window_ms=args.window_ms, split_queues=args.mode in ("queues", "micro_queues", "hybrid"),
                              join_input=args.mode == "hybrid")
    semaphore = asyncio.Semaphore(args.workers)
    nearest = []
    results = []
    started = 0.
    next_seq = 0

    async def client():
        nonlocal next_seq
        while next_seq < args.requests:
            i = next_seq
            next_seq += 1
            is_write = args.write_every > 0 and i % args.write_every == 0
            # Rotate books across renewals and readers independently.
            agent = i % len(reads)
            book = (i // args.write_every) % 2 if is_write else agent % 2
            op = writes[book] if is_write else reads[agent]
            begin = loop.time()
            try:
                if queue_mode:
                    value = await coordinator.submit(op, timeout=args.timeout)
                else:
                    op.validate()
                    async with asyncio.timeout(args.timeout):
                        async with semaphore:
                            nearest.append(loop.time() - begin)
                            value, = await loop.run_in_executor(
                                pool, lambda: execute(kernel, [op], join_input=args.mode == "sql", grouped=False))
                    if isinstance(value, Exception):
                        raise value
                ok = value > datetime.now(timezone.utc) if is_write else value == expected[agent]
                code = "verified" if ok else "WRONG_RESULT"
            except Exception as exc:
                code = getattr(exc, "code", type(exc).__name__)
            results.append({"seq": i, "book": book, "kind": "renew" if is_write else "status", "result": code,
                            "started_s": begin - started, "latency_s": loop.time() - begin})

    try:
        if queue_mode:
            await coordinator.__aenter__()
        with Observer(url) as observer, SqlMeasurements(kernel.db.engine) as sql:
            cpu = time.process_time()
            started = loop.time()
            await asyncio.gather(*(client() for _ in range(min(args.concurrency, args.requests))))
            duration = loop.time() - started
            cpu = time.process_time() - cpu
        metrics = sql.result(duration)
    finally:
        if queue_mode:
            await coordinator.__aexit__(None, None, None)
        pool.shutdown()
    # Verify the durable lease binding after every operation has drained.
    for op in writes:
        with kernel.db.uow(write=False) as repo:
            task = repo.one("tasks", {"task_id": op.task_id})
            if (task["generation"], task["lease_owner"], task["state"]) != (op.generation, op.worker_id, "RUNNING"):
                raise ValueError("Unexpected durable lease state")
    errors = Counter(r["result"] for r in results)
    per_kind = {kind: percentiles([r["latency_s"] for r in results if r["kind"] == kind]) for kind in ("status", "renew")}
    try:
        import resource
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        rss_bytes = rss if sys.platform == "darwin" else 1024 * rss
    except ImportError:
        rss_bytes = None
    return {"mode": args.mode, "agents": len(reads), "db_works": 2, "requests": args.requests,
            "workers": args.workers, "concurrency": args.concurrency, "capacity": args.capacity,
            "requested_batch": args.batch, "window_ms": args.window_ms, "write_every": args.write_every,
            "arrival": "closed-loop; clients request again after result validation", "seconds": duration,
            "cpu_process_s": cpu, "peak_process_rss_bytes_including_setup": rss_bytes,
            "goodput": errors["verified"] / duration, "outcomes": errors,
            "latency": percentiles([r["latency_s"] for r in results]), "latency_by_kind": per_kind,
            "sql": metrics, "stats_activity_interval_s": .05,
            "pg_samples_time_connections_active_lockwait": [[round(t-started, 6), n, a, l] for t,n,a,l in observer.samples],
            "queue": dict(coordinator.counts), "batch_sizes": dict(coordinator.batches),
            "max_queue_wait_s": coordinator.max_queue_wait_s if queue_mode else max(nearest, default=0),
            "raw": sorted(results, key=lambda r: r["seq"])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=MODES, required=True)
    parser.add_argument("--agents", type=int, default=50)
    parser.add_argument("--requests", type=int, default=600)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--concurrency", type=int, default=64)
    parser.add_argument("--capacity", type=int, default=128)
    parser.add_argument("--batch", type=int, default=32)
    parser.add_argument("--window-ms", type=float, default=2)
    parser.add_argument("--write-every", type=int, default=10, help="0 means a read-only run")
    parser.add_argument("--timeout", type=float, default=15)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if min(args.agents, args.requests, args.workers, args.concurrency, args.capacity) <= 0 or args.write_every < 0:
        parser.error("Workload dimensions must be positive; write-every can also be zero")
    if args.concurrency > args.capacity:
        parser.error("Keep the workload within the same admission budget for all modes")
    with private_kernel(args.workers) as (kernel, url):
        fixture = seed(kernel, args.agents)
        result = asyncio.run(measure(kernel, url, fixture, args))
    result["python"] = platform.python_version()
    result["platform"] = platform.platform()
    result["git_head"] = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(args.mode, dict(result["outcomes"]), round(result["goodput"], 2))
    if result["outcomes"] != {"verified": args.requests}:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
