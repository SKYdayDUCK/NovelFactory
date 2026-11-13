"""Real task claims + renewals from two books, replacing only the fairness sort in FIFO."""

import argparse
import gzip
import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, Lock
from time import perf_counter

from sqlalchemy import event, text

from benchmarks.hera_mixed import population, task_spec
from benchmarks.http_load import percentiles
from benchmarks.p2_behavioral_load import isolated_kernel
from novel_factory.application.kernel import Kernel
from novel_factory.infrastructure.database import Database


def trial(kernel, armsize, mode):
    # This internal authorized fixture provisions real tasks using each owner's
    # identity. It then drives the underlying multi-work queue as a trusted
    # scheduler; a grant-bound HTTP claim ordinarily covers just one owner.
    actors = population(kernel, 2)
    sent = {}
    labels = {}

    def create(index, key):
        identity, wr = actors[index]
        spec = task_spec(identity["principal"], wr)
        kernel.create_task(identity["principal"], spec, key)
        labels[wr["work_id"]] = index
        return spec["task_id"]

    renewal = create(0, "renew")
    first = kernel.tasks.claim("renew", task_id=renewal, lease_seconds=300)
    assert str(first["task_id"]) == renewal and first["generation"] == 1
    for index in range(2):
        for j in range(armsize):
            tid = create(index, f"g{index}-{j}")
            sent[tid] = index

    active_query = None
    lock = Lock()

    def inspect(conn, cursor, query, params, context, many):
        nonlocal active_query
        marker = "ORDER BY nf_task_fairness.last_served_at, nf_tasks.created_at, nf_tasks.task_id"
        if marker in query:
            if mode == "fifo":
                query = query.replace(marker, "ORDER BY nf_tasks.created_at, nf_tasks.task_id")
            with lock:
                if active_query is None:
                    active_query = (query, params)
        return query, params

    event.listen(kernel.db.engine, "before_cursor_execute", inspect, retval=True)
    gate = Barrier(5)

    def claim(i):
        rows = []
        gate.wait()
        while True:
            began = perf_counter()
            row = kernel.tasks.claim(f"worker-{i}", lease_seconds=300)
            end = perf_counter()
            if row is None:
                return rows
            tid = str(row["task_id"])
            assert (
                row["generation"] == 1
                and row["state"] == "RUNNING"
                and labels[str(row["work_id"])] == sent[tid]
            )
            rows.append((tid, sent[tid], i, began, end))

    def renew():
        times = []
        gate.wait()
        for _ in range(armsize):
            start = perf_counter()
            expiry = kernel.tasks.renew(renewal, 1, "renew", lease_seconds=300)
            times.append((start, perf_counter(), expiry.isoformat()))
        return times

    try:
        with ThreadPoolExecutor(5) as pool:
            claims = [pool.submit(claim, i) for i in range(4)]
            renewals = pool.submit(renew)
            rows = [r for lane in claims for r in lane.result()]
            renews = renewals.result()
        assert len({r[0] for r in rows}) == len(rows) == len(sent)
        assert active_query is not None
        with kernel.db.uow() as repo:
            executions = repo.conn.execute(
                text("""SELECT t.task_id::text,t.work_id::text
                FROM nf_task_executions e JOIN nf_tasks t USING(task_id)
                WHERE t.task_id<>:renew ORDER BY e.created_at,t.task_id"""),
                {"renew": renewal},
            ).all()
            assert len(executions) == len(sent)
            order = [labels[w] for _, w in executions]
            # Plans for the original full backlog require actual READY tasks;
            # reset only this diagnostic transaction then roll it back.
            with repo.conn.begin_nested() as diagnostic:
                repo.conn.execute(
                    text("UPDATE nf_tasks SET state='READY',generation=0 WHERE task_id<>:renew"),
                    {"renew": renewal},
                )
                q, params = active_query
                plan = repo.conn.exec_driver_sql(
                    "EXPLAIN (ANALYZE,BUFFERS,WAL,FORMAT JSON) " + q, params
                ).scalar()
                diagnostic.rollback()
        start = min(r[3] for r in rows)
        return {
            "mode": mode,
            "claims": rows,
            "renewals": renews,
            "claim_plan": plan,
            "works": order,
            "first_served": [order.index(i) + 1 for i in range(2)],
            "peak_imbalance": max(
                abs(order[:n].count(1) - order[:n].count(0)) for n in range(1, len(order) + 1)
            ),
            "goodput": len(rows) / (max(r[4] for r in rows) - start),
            "claim_latency": percentiles([r[4] - r[3] for r in rows]),
            "renew_latency": percentiles([r[1] - r[0] for r in renews]),
        }
    finally:
        event.remove(kernel.db.engine, "before_cursor_execute", inspect)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--per-work", type=int, default=64)
    p.add_argument("--order", default="fair,fifo,fifo,fair")
    p.add_argument("--output", required=True, type=Path)
    args = p.parse_args()
    modes = args.order.split(",")
    if not 1 <= args.per_work <= 1000 or set(modes) - {"fair", "fifo"}:
        p.error("invalid size or policy")
    out = {
        "revision": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "cpu_affinity": sorted(os.sched_getaffinity(0)),
        "arms": [],
    }
    for mode in modes:
        with (
            TemporaryDirectory() as root,
            isolated_kernel(os.environ["NF_TEST_DATABASE_URL"], Path(root)) as (setup, url),
        ):
            setup.db.close()
            db = Database(url, pool_size=5, max_overflow=0)
            try:
                arm = trial(Kernel(db, setup.objects), args.per_work, mode)
                out["arms"].append(arm)
                with gzip.open(args.output, "wt") as handle:
                    json.dump(out, handle, separators=(",", ":"))
                print(
                    mode,
                    arm["goodput"],
                    arm["first_served"],
                    arm["peak_imbalance"],
                    arm["renew_latency"],
                    flush=True,
                )
            finally:
                db.close()


if __name__ == "__main__":
    main()
