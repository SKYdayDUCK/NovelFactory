"""Measure fresh, committed reservations on a disposable, migrated PG17 database."""
import argparse
import hashlib
import json
import os
import statistics
import tempfile
import time
from pathlib import Path

from sqlalchemy import event, text
from sqlalchemy.engine import make_url

from novel_factory.application.kernel import Kernel
from novel_factory.fixtures import create_bound_task, load_fixture, point
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.objects import ObjectStore
from novel_factory.orchestration import tasks


def measure(db, objects, history, samples):
    kernel = Kernel(db, ObjectStore(objects))
    book = load_fixture(kernel)
    wr = book["world_ref"]
    task = create_bound_task(kernel, book["principal"], wr, point(1))
    budget = kernel.tasks.create_budget(wr["work_id"], "1")
    counts = []

    def observe(_conn, _cursor, statement, _params, _context, _many):
        counts.append("sum(" in statement.lower() and "nf_call_attempts" in statement.lower())

    observations = []
    for i in range(history + samples):
        key = str(i)
        counts.clear()
        event.listen(db.engine, "before_cursor_execute", observe)
        try:
            started = time.perf_counter_ns()
            call = kernel.tasks.reserve_call(
                task["task_id"], task["generation"], task["worker_id"], budget_id=budget, amount="0.000001",
                call_key=key, request_hash=hashlib.sha256(key.encode()).hexdigest(), token_amount=1,
            )
            elapsed_ns = time.perf_counter_ns() - started
        finally:
            event.remove(db.engine, "before_cursor_execute", observe)
        assert call["state"] == "PREPARED"
        if i >= history:
            observations.append(dict(history=i, elapsed_ns=elapsed_ns, statements=len(counts), aggregates=sum(counts)))
        # Five distinct ledger states. Never send anything over the network.
        state = ("PREPARED", "SENT", "UNKNOWN", "SUCCEEDED", "FAILED")[i % 5]
        if state in {"SENT", "UNKNOWN", "SUCCEEDED"}:
            assert kernel.tasks.mark_call_sent(call["attempt_id"], worker_id=task["worker_id"])
        if state in {"UNKNOWN", "SUCCEEDED", "FAILED"}:
            kernel.tasks.settle_call(call["attempt_id"], state=state, actual_tokens=int(state == "SUCCEEDED"),
                                     actual_amount="0.000001" if state == "SUCCEEDED" else None)

    with db.uow(write=False) as repo:
        rows = repo.conn.execute(text("SELECT state, count(*) FROM nf_call_attempts WHERE task_id=:task GROUP BY state"),
                                 {"task": task["task_id"]}).all()
        pg = repo.conn.scalar(text("SHOW server_version"))
    return dict(postgres=pg, source_sha256=hashlib.sha256(Path(tasks.__file__).read_bytes()).hexdigest(),
                states=dict(rows), median_ms=statistics.median(row["elapsed_ns"] for row in observations) / 1e6,
                observations=observations)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", type=int, default=30)
    parser.add_argument("--samples", type=int, default=60)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    url = os.environ["NF_TEST_DATABASE_URL"]
    if (make_url(url).database != "nf_test" or make_url(url).host not in {"127.0.0.1", "localhost"}
            or not 0 <= args.history <= 1000 or not 1 <= args.samples <= 1000):
        parser.error("Use scripts/with_postgres.py --migrate and a bounded workload")
    db = Database(url, pool_size=2)
    try:
        with tempfile.TemporaryDirectory() as objects:
            report = measure(db, objects, args.history, args.samples)
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    finally:
        db.close()
