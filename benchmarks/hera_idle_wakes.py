"""Two live index workers wake on committed notifications while an unrelated work submits tasks."""

import argparse
import gzip
import json
import multiprocessing
import os
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, process_time, sleep

from sqlalchemy import event, func, select, text

from benchmarks.hera_mixed import population, task_spec
from benchmarks.p2_behavioral_load import isolated_kernel
from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.derived_worker import CONSUMER
from novel_factory.application.kernel import Kernel
from novel_factory.application.supervisor import RuntimeSupervisor
from novel_factory.fixtures import load_fixture, point, run_command
from novel_factory.infrastructure.database import Database, tables
from novel_factory.infrastructure.event_streams import EventEnvelope
from novel_factory.infrastructure.objects import ObjectStore
from novel_factory.orchestration.processors import TaskDispatcher, handler_for
from scripts.run_supervisor import serve


def worker(pipe, url, scope, objects, stop):
    db = Database(url, pool_size=2, max_overflow=0)
    kernel = Kernel(db, ObjectStore(objects))
    sql = []

    def count(conn, cursor, statement, parameters, ctx, many):
        sql.append((monotonic(), statement))

    event.listen(db.engine, "before_cursor_execute", count)
    try:
        pipe.send("started")
        cpu = process_time()
        serve(
            RuntimeSupervisor(kernel, AgentGateway(kernel, {})),
            *scope,
            workers=2,
            interval=1,
            processor="compute",
            stop_event=stop,
        )
        pipe.send({"sql": sql, "cpu_s": process_time() - cpu})
    finally:
        db.close()
        pipe.close()


def trial(kernel, url):
    actor, actor_ref = population(kernel, 1)[0]
    work = load_fixture(kernel)
    ref, ids = work["world_ref"], work["ids"]
    scope = work["principal"], ref["work_id"], ref["branch_id"]
    TaskDispatcher(RuntimeSupervisor(kernel, AgentGateway(kernel, {})), *scope).switch("redis")
    ctx = multiprocessing.get_context("spawn")
    stop = ctx.Event()
    client, server = ctx.Pipe()
    child = ctx.Process(
        target=worker, args=(server, url, scope, str(kernel.objects.root), stop)
    )
    child.start()
    server.close()

    def indexing(seq):
        nonlocal ref
        ref = run_command(
            kernel,
            work["principal"],
            ref,
            "RESOURCE_TRANSFER",
            {
                "from_account": ids["warehouse"],
                "to_account": ids["relief"],
                "resource_id": ids["grain"],
                "unit": "bag",
                "quantity": "1",
                "at": point(30 + seq),
            },
        )["world_ref"]
        # Feed the actual committed world.accepted source to the normal compute
        # subscriber, which persists its branch hint before emitting the wake.
        with kernel.db.uow() as repo:
            message = repo.one("outbox", {"dedupe_key": "world:" + ref["world_revision_id"]})
            handler_for("compute")(repo.conn, EventEnvelope.from_source(message))
        committed = monotonic()
        with kernel.db.uow(write=False) as repo:
            # Poll each real receipt with READ COMMITTED, without locking any row.
            while monotonic() - committed < 10:
                if repo.conn.scalar(
                    text("""SELECT EXISTS(SELECT FROM nf_inbox i JOIN nf_outbox o
                    ON o.outbox_id=i.message_id WHERE i.consumer=:consumer AND o.dedupe_key=:key)"""),
                    {"consumer": CONSUMER, "key": "world:" + ref["world_revision_id"]},
                ):
                    return monotonic() - committed
                sleep(0.005)
        raise TimeoutError("Index never materialized")

    try:
        assert client.poll(30) and client.recv() == "started"
        indexing(0)  # Start the pool on real work before measuring scans or latency.
        sleep(0.15)
        begin = monotonic()
        created = []
        for _ in range(64):
            spec = task_spec(actor["principal"], actor_ref)
            kernel.create_task(actor["principal"], spec, spec["task_id"])
            created.append(spec["task_id"])
        end = monotonic()
        tail = [indexing(n) for n in range(1, 5)]
        # Final physical oracle includes every source and never counts a mere notification.
        with kernel.db.uow(write=False) as repo:
            assert repo.conn.scalar(
                select(func.count()).where(tables["tasks"].c.task_id.in_(created))
            ) == len(created)
            assert (
                repo.conn.scalar(
                    text("SELECT count(*) FROM nf_inbox WHERE consumer=:consumer"),
                    {"consumer": CONSUMER},
                )
                == 5
            )
            marks = repo.many("index_watermarks", {"branch_id": ref["branch_id"]})
            assert len(marks) == 2 and all(
                mark["world_revision_id"] == ref["world_revision_id"] for mark in marks
            )
        stop.set()
        assert client.poll(12)
        counts = client.recv()
        child.join(10)
        assert child.exitcode == 0
        window = [s for t, s in counts["sql"] if begin <= t <= end]
        return {
            "revision": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
            "tasks": len(created),
            "noise_seconds": end - begin,
            "parent_sql_while_submitting": len(window),
            "compute_hint_reads": sum(
                s.lstrip().startswith("SELECT") and "nf_processor_hints" in s for s in window
            ),
            "parent_cpu_s_including_warmup": counts["cpu_s"],
            "new_work_seconds": tail,
            "verified_index_receipts": 5,
            "sql": counts["sql"],
        }
    finally:
        stop.set()
        child.join(10)
        if child.is_alive():
            child.terminate()
            child.join()
        client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=1)
    args = parser.parse_args()
    rows = []
    for _ in range(args.repeats):
        with (
            TemporaryDirectory() as tmp,
            isolated_kernel(os.environ["NF_TEST_DATABASE_URL"], Path(tmp)) as (kernel, url),
        ):
            rows.append(trial(kernel, url))
        with gzip.open(args.output, "wt") as out:
            json.dump(rows, out, separators=(",", ":"))
        print({k: v for k, v in rows[-1].items() if k != "sql"}, flush=True)
