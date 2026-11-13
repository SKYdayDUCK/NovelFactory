"""Separate client, API and PG17. Hold six business connections and 2+2 API slots fixed."""

import argparse
import asyncio
import gzip
import json
import logging
import multiprocessing
import os
import resource
import subprocess
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from time import perf_counter

import psycopg
from sqlalchemy import text
from sqlalchemy.engine import make_url

from benchmarks.hera_mixed import population, task_spec
from benchmarks.hera_routing import measure
from benchmarks.http_load import SqlMeasurements, loopback_server
from benchmarks.p2_behavioral_load import isolated_kernel
from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.derived_worker import DerivedWorldWorker
from novel_factory.application.kernel import Kernel
from novel_factory.application.supervisor import RuntimeSupervisor
from novel_factory.application.task_operations import TaskOperationsBinding
from novel_factory.fixtures import load_fixture, point, run_command
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.objects import ObjectStore
from novel_factory.interfaces.http import HttpSettings, create_app
from novel_factory.orchestration.processors import TaskDispatcher


def drain(kernel, scope, count, gate, stop):
    if not gate.wait(120):
        raise RuntimeError("Reader did not start")
    start = perf_counter()
    service = DerivedWorldWorker(kernel)
    from threading import Lock

    lock = Lock()
    rows = []

    def worker():
        while not stop.is_set():
            if perf_counter() - start > 150:
                raise RuntimeError(f"Index drain exceeded deadline after {len(rows)} receipts")
            begin = perf_counter()
            receipt = service.rebuild_one(*scope)
            if receipt:
                with lock:
                    rows.append((receipt["message_id"], begin, perf_counter()))
                    if len(rows) == count:
                        stop.set()
            else:
                stop.wait(0.025)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(worker) for _ in range(2)]
        for f in futures:
            f.result()
    assert len({r[0] for r in rows}) == len(rows) == count
    return rows


def process_info():
    r = resource.getrusage(resource.RUSAGE_SELF)
    return {"pid": os.getpid(), "cpu_s": r.ru_utime + r.ru_stime, "rss_kib": r.ru_maxrss}


def index_only(channel, url, objects, scope, count, gate, stop, nice_increment):
    if nice_increment:
        os.nice(nice_increment)
    db = Database(url, pool_size=2, max_overflow=0)
    try:
        t0 = perf_counter()
        with SqlMeasurements(db.engine) as meter:
            before = process_info()
            rows = drain(Kernel(db, ObjectStore(objects)), scope, count, gate, stop)
            result = {
                "rows": rows,
                "sql": meter.result(perf_counter() - t0),
                "before": before,
                "after": process_info(),
                "nice": os.getpriority(os.PRIO_PROCESS, 0),
            }
        channel.send(result)
    finally:
        db.close()
        channel.close()


def serve(channel, url, objects, roster, scope, mode, count, gate, stop):
    separate = mode in ("pools", "process", "process-nice")
    api_db = Database(url, pool_size=4 if separate else 6, max_overflow=0)
    with ExitStack() as stack:
        stack.callback(channel.close)
        stack.callback(api_db.close)
        kernel = Kernel(api_db, ObjectStore(objects))
        index_kernel = kernel
        if mode == "pools":
            db = Database(url, pool_size=2, max_overflow=0)
            stack.callback(db.close)
            index_kernel = Kernel(db, kernel.objects)
        settings = HttpSettings(
            max_queued_operations=256,
            max_queued_ingress=256,
            queue_timeout_seconds=60,
            rate_burst=1000,
            fenced_task_status=True,
        )
        app = create_app(kernel, settings=settings, mcp_bindings=roster)
        # Pool size normally determines both API and authentication admission.
        # Pin the controller's limits before startup for this independent variable.
        controller = app.state.admission
        controller.capacity = 2
        controller.class_limits = {
            name: min(2, value) for name, value in controller.class_limits.items()
        }
        assert app.state.authentication_admission.capacity == 2
        future = None
        if mode in ("shared", "logical", "pools"):
            pool = stack.enter_context(ThreadPoolExecutor(1))
            future = pool.submit(drain, index_kernel, scope, count, gate, stop)
        with loopback_server(app, lifespan="on") as base:
            channel.send(base)
            if channel.recv() != "measure":
                return
            start = perf_counter()
            before = process_info()
            with ExitStack() as measurements:
                api_meter = measurements.enter_context(SqlMeasurements(api_db.engine))
                index_meter = None
                if mode == "pools":
                    index_meter = measurements.enter_context(
                        SqlMeasurements(index_kernel.db.engine)
                    )
                channel.send("measuring")
                gate.set()
                assert channel.recv() == "sample"
                dt = perf_counter() - start
                out = {
                    "sql": api_meter.result(dt),
                    "operation": controller.snapshot(),
                    "auth": app.state.authentication_admission.snapshot(),
                    "before": before,
                    "after": process_info(),
                    "epoch": start,
                }
                if index_meter is not None:
                    out["index_sql"] = index_meter.result(dt)
                channel.send(out)
            if future:
                channel.send(future.result(timeout=160))
            assert channel.recv() == "stop"


def observer(dsn, db_name, gate, finished, rows):
    with psycopg.connect(dsn, autocommit=True) as conn:
        clock_ticks = os.sysconf("SC_CLK_TCK")
        page_size = os.sysconf("SC_PAGE_SIZE")
        gate.wait(120)
        while not finished.is_set():
            active = conn.execute(
                """SELECT pid, state, wait_event_type,wait_event FROM pg_stat_activity
                WHERE datname=%s AND backend_type='client backend'""",
                (db_name,),
            ).fetchall()
            counters = []
            for pid, *_ in active:
                proc = Path(f"/proc/{pid}")
                try:
                    stat = (proc / "stat").read_text().rsplit(")", 1)[1].split()
                    io = dict(
                        line.split(": ", 1) for line in (proc / "io").read_text().splitlines()
                    )
                    counters.append(
                        (
                            pid,
                            (int(stat[11]) + int(stat[12])) / clock_ticks,
                            int(stat[21]) * page_size,
                            int(io["read_bytes"]),
                            int(io["write_bytes"]),
                        )
                    )
                except (FileNotFoundError, ProcessLookupError, PermissionError):
                    pass
            rows.append((perf_counter(), active, counters))
            finished.wait(0.05)


def run(url, objects, roster, records, scope, mode, count):
    ctx = multiprocessing.get_context("spawn")
    gate, stop, finished = ctx.Event(), ctx.Event(), ctx.Event()
    host, server = ctx.Pipe()
    child = ctx.Process(
        target=serve, args=(server, url, objects, roster, scope, mode, count, gate, stop)
    )
    idx = idx_chan = None
    if mode in ("process", "process-nice"):
        idx_chan, ix = ctx.Pipe()
        idx = ctx.Process(target=index_only, args=(ix, url, objects, scope, count, gate, stop,
                                                 10 if mode == "process-nice" else 0))
        idx.start()
        ix.close()
    child.start()
    server.close()
    observed = []
    admin = (
        make_url(url)
        .set(drivername="postgresql", database="postgres")
        .render_as_string(hide_password=False)
    )
    thread = Thread(
        target=observer, args=(admin, make_url(url).database, gate, finished, observed), daemon=True
    )
    thread.start()
    try:
        assert host.poll(60)
        base = host.recv()
        client_before = process_info()
        read = asyncio.run(measure([(base, host)], records, "single"))
        read["client_process"] = {"before": client_before, "after": process_info()}
        assert read["verified"] == read["offered"] == 128
        # hera_routing originally reports four product connections; use this trial's actual bounds.
        read["pool_budget_total"] = 6
        assert read["operation_budget_total"] == read["metrics"][0]["operation"]["capacity"] == 2
        assert read["auth_budget_total"] == read["metrics"][0]["auth"]["capacity"] == 2
        end = read["clock_origin"] + read["seconds"]
        if mode in ("process", "process-nice"):
            assert idx_chan.poll(180)
            worker = idx_chan.recv()
            rows = worker.pop("rows")
        elif mode == "quiet":
            rows, worker = [], {}
        else:
            assert host.poll(180)
            rows, worker = host.recv(), {}
        host.send("stop")
        child.join(10)
        assert child.exitcode == 0
        if idx:
            idx.join(10)
            assert idx.exitcode == 0
        return {
            "mode": mode,
            "read": read,
            "api_pool": 4 if mode in ("pools", "process", "process-nice") else 6,
            "dedicated_index_pool": 2 if mode in ("pools", "process", "process-nice") else 0,
            "sql_observer_connections": 1,
            "overlapping": sum(r[2] <= end for r in rows),
            "index_rows": rows,
            "worker": worker,
            "pg_samples": observed,
        }
    finally:
        finished.set()
        stop.set()
        gate.set()
        thread.join(2)
        for proc in (child, idx):
            if proc is not None and proc.is_alive():
                proc.terminate()
                proc.join(5)
        host.close()
        if idx_chan:
            idx_chan.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--revisions", type=int, default=60)
    parser.add_argument(
        "--order", default="quiet,shared,logical,pools,process,process,pools,logical,shared,quiet"
    )
    args = parser.parse_args()
    modes = args.order.split(",")
    if (
        set(modes) - {"quiet", "shared", "logical", "pools", "process", "process-nice"}
        or not 1 <= args.revisions <= 100
    ):
        parser.error("Unknown arm or invalid revision count")
    logging.getLogger("mcp").setLevel(logging.ERROR)
    out = {
        "revision": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "cpu_affinity": sorted(os.sched_getaffinity(0)),
        "runs": [],
    }
    for mode in modes:
        with (
            TemporaryDirectory() as directory,
            isolated_kernel(os.environ["NF_TEST_DATABASE_URL"], Path(directory)) as (kernel, url),
        ):
            records, roster = [], {}
            for identity, wr in population(kernel, 8):
                spec = task_spec(identity["principal"], wr)
                kernel.create_task(identity["principal"], spec, spec["task_id"])
                records.append((identity, spec))
                roster[identity["principal"]] = TaskOperationsBinding(
                    wr["work_id"], wr["branch_id"]
                )
            book = load_fixture(kernel)
            principal, wr, ids = book["principal"], book["world_ref"], book["ids"]
            for i in range(args.revisions):
                wr = run_command(
                    kernel,
                    principal,
                    wr,
                    "RESOURCE_TRANSFER",
                    {
                        "from_account": ids["warehouse"],
                        "to_account": ids["relief"],
                        "resource_id": ids["grain"],
                        "unit": "bag",
                        "quantity": "1",
                        "at": point(i + 30),
                    },
                )["world_ref"]
            scope = (principal, wr["work_id"], wr["branch_id"])
            TaskDispatcher(RuntimeSupervisor(kernel, AgentGateway(kernel, {})), *scope).switch(
                "redis"
            )
            if mode == "logical":
                with kernel.db.uow() as repo:
                    repo.conn.execute(
                        text("""INSERT INTO nf_dispatch_limits VALUES('RESOURCE','CPU_COMPUTE',1)
                        ON CONFLICT(scope,subject) DO UPDATE SET slots=1""")
                    )
            kernel.db.close()
            arm = run(url, kernel.objects.root, roster, records, scope, mode, args.revisions)
            if mode != "quiet":
                with kernel.db.uow(write=False) as repo:
                    watermark = repo.one(
                        "index_watermarks", {"branch_id": wr["branch_id"], "index_name": "timeline"}
                    )
                    assert str(watermark["world_revision_id"]) == wr["world_revision_id"]
                    arm["verified_watermark"] = str(watermark["world_revision_id"])
            out["runs"].append(arm)
            with gzip.open(args.output, "wt") as handle:
                json.dump(out, handle, separators=(",", ":"))
            print(
                mode,
                arm["read"]["goodput_s"],
                arm["read"]["latency"],
                arm["overlapping"],
                flush=True,
            )


if __name__ == "__main__":
    main()
