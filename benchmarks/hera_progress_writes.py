"""Same-branch PG17 projection A/B tests in a disposable migrated database."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import gzip
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from threading import Barrier, Event
import time
from uuid import UUID

import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url

from benchmarks.hera_mixed import task_spec
from benchmarks.http_load import percentiles
from benchmarks.p2_behavioral_load import isolated_kernel
from novel_factory.fixtures import load_fixture

SQL_DIR = Path(__file__).with_name("sql")
TRIGGER = "nf_hera_progress_delta"
WRITE = "UPDATE nf_tasks SET state=%s WHERE task_id=%s RETURNING state"
SOURCE = """SELECT count(*) FILTER (WHERE state='READY'),count(*) FILTER (WHERE state='PAUSED')
            FROM nf_tasks WHERE work_id=%s AND branch_id=%s"""
CHECKPOINT = (
    """SELECT ready, paused FROM bench_progress_checkpoint WHERE work_id=%s AND branch_id=%s"""
)
# Both SELECT arms use one statement snapshot. A watermark alone is insufficient:
# sequences allocate before commit, so an older id may appear after the fold.
SAFE = """WITH cfg AS (SELECT %s::uuid AS w, %s::uuid AS b),
    dirty AS MATERIALIZED (SELECT EXISTS (SELECT FROM bench_progress_outbox, cfg
            WHERE work_id=w AND branch_id=b) AS pending)
    SELECT ready,paused FROM bench_progress_checkpoint,cfg,dirty
        WHERE work_id=w AND branch_id=b AND NOT pending
    UNION ALL
    SELECT (SELECT count(*) FROM nf_tasks,cfg WHERE work_id=w AND branch_id=b AND state='READY'),
           (SELECT count(*) FROM nf_tasks,cfg WHERE work_id=w AND branch_id=b AND state='PAUSED')
        FROM dirty WHERE pending"""
FOLD = """WITH picked AS MATERIALIZED (
    SELECT id FROM bench_progress_outbox WHERE work_id=%s AND branch_id=%s
    ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 128
), popped AS (
    DELETE FROM bench_progress_outbox q USING picked p WHERE q.id=p.id
    RETURNING q.ready_delta,q.id
), stats AS (SELECT sum(ready_delta) AS d, max(id) AS upto, count(*) AS n FROM popped)
UPDATE bench_progress_checkpoint SET ready=ready+d,paused=paused-d,
    last_folded_id=greatest(last_folded_id,upto)
FROM stats WHERE work_id=%s AND branch_id=%s AND stats.n>0 RETURNING n"""
READ = {
    "raw": SOURCE,
    "disabled": SOURCE,
    "suspended": SOURCE,
    "fused_empty": SOURCE,
    "sync": """SELECT coalesce((counts->>'READY')::bigint,0),coalesce((counts->>'PAUSED')::bigint,0)
               FROM nf_hera_progress WHERE work_id=%s AND branch_id=%s""",
    "shard": """SELECT sum(ready),sum(paused) FROM bench_progress_shard
                WHERE work_id=%s AND branch_id=%s""",
    "queue": SAFE,
}


def trigger(conn, function):
    conn.execute(sql.SQL("DROP TRIGGER IF EXISTS {} ON nf_tasks").format(sql.Identifier(TRIGGER)))
    if function:
        conn.execute(
            sql.SQL("""CREATE TRIGGER {} AFTER INSERT OR UPDATE OF state,work_id,branch_id
                    OR DELETE ON nf_tasks FOR EACH ROW EXECUTE FUNCTION {}()""").format(
                sql.Identifier(TRIGGER), sql.Identifier(function)
            )
        )


def prepare(conn, mode, original, branch, db):
    from novel_factory.orchestration.progress import set_progress_maintenance

    with db.uow() as repo:
        set_progress_maintenance(repo.conn, suspended=False)

    trigger(conn, None)
    conn.execute(
        "TRUNCATE bench_progress_outbox,bench_progress_shard,bench_progress_checkpoint,nf_hera_progress"
    )
    conn.execute("UPDATE nf_tasks SET state='READY' WHERE state='PAUSED'")
    n = conn.execute(SOURCE, branch).fetchone()[0]
    if mode in ("sync", "fused", "disabled", "fused_empty", "suspended"):
        if mode in ("sync", "fused"):
            conn.execute(
                "INSERT INTO nf_hera_progress(work_id,branch_id,counts) VALUES(%s,%s,jsonb_build_object('READY',%s::int))",
                (*branch, n),
            )
        conn.execute(
            (SQL_DIR / "hera_progress_fused.sql").read_text()
            if mode.startswith("fused")
            else original
        )
        trigger(conn, "nf_hera_count_transition")
        if mode == "suspended":
            with db.uow() as repo:
                set_progress_maintenance(repo.conn, suspended=True)

    elif mode == "shard":
        conn.execute("""INSERT INTO bench_progress_shard SELECT work_id,branch_id,bench_task_shard(task_id),
                     count(*),0 FROM nf_tasks GROUP BY 1,2,3""")
        trigger(conn, "bench_sharded_delta")
    elif mode == "queue":
        conn.execute("INSERT INTO bench_progress_checkpoint VALUES(%s,%s,%s,0,0)", (*branch, n))
        trigger(conn, "bench_enqueue_delta")
    conn.execute("VACUUM ANALYZE nf_tasks")


def run_arm(dsn, conn, mode, branch, ids, workers, operations):
    # Each application connection owns disjoint tasks. Thus lock waits between
    # writers arise from shared index/counter resources rather than one task id.
    barrier = Barrier(workers + 1)
    start = Event()
    tasks = [ids[i::workers] for i in range(workers)]

    def lane(i):
        rows = []
        with psycopg.connect(dsn) as client:
            barrier.wait()
            start.wait()
            for seq in range(operations):
                task = tasks[i][seq % len(tasks[i])]
                state = "PAUSED" if seq // len(tasks[i]) % 2 == 0 else "READY"
                begin = time.perf_counter_ns()
                assert client.execute(WRITE, (state, task)).fetchone() == (state,)
                updated = time.perf_counter_ns()
                client.commit()
                rows.append([i, seq, str(task), state, begin, updated, time.perf_counter_ns()])
        return rows

    read = READ["sync" if mode == "fused" else mode]

    def validate():
        assert conn.execute(read, branch).fetchone() == conn.execute(SOURCE, branch).fetchone()

    validate()
    conn.execute("SELECT pg_stat_statements_reset()")
    conn.execute("SELECT pg_stat_reset()")
    lsn = conn.execute("SELECT pg_current_wal_insert_lsn()").fetchone()[0]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        jobs = [pool.submit(lane, i) for i in range(workers)]
        barrier.wait()
        begin = time.perf_counter_ns()
        start.set()
        samples = []
        while not all(job.done() for job in jobs):
            waits = conn.execute("""SELECT pid,wait_event_type,wait_event FROM pg_stat_activity
                           WHERE datname=current_database() AND state='active' AND pid<>pg_backend_pid()""").fetchall()
            samples.append([time.perf_counter_ns(), waits])
            time.sleep(0.005)
        rows = [row for job in jobs for row in job.result()]
        elapsed = (max(r[-1] for r in rows) - begin) * 1e-9
    wal = int(
        conn.execute("SELECT pg_wal_lsn_diff(pg_current_wal_insert_lsn(), %s)", (lsn,)).fetchone()[
            0
        ]
    )
    validate()
    dirty_read = conn.execute("EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) " + read, branch).fetchone()[0]
    folded = []
    if mode == "queue":
        while True:
            t = time.perf_counter_ns()
            chunk = conn.execute(FOLD, branch * 2).fetchone()
            if not chunk:
                break
            folded.append([chunk[0], time.perf_counter_ns() - t])
        validate()
    clean_read = conn.execute("EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) " + read, branch).fetchone()[0]
    functions = conn.execute("""SELECT funcname,calls,total_time,self_time FROM pg_stat_user_functions
        WHERE funcname IN ('nf_hera_count_transition','bench_enqueue_delta','bench_sharded_delta')""").fetchall()
    statements = conn.execute("""SELECT query,calls,total_exec_time,wal_bytes FROM pg_stat_statements
        WHERE dbid=(SELECT oid FROM pg_database WHERE datname=current_database()) AND calls>0
        ORDER BY total_exec_time DESC""").fetchall()
    sample_id = ids[0]
    plan = conn.execute(
        """EXPLAIN (ANALYZE,BUFFERS,WAL,FORMAT JSON)
        UPDATE nf_tasks SET state=CASE state WHEN 'PAUSED' THEN 'READY' ELSE 'PAUSED' END
        WHERE task_id=%s""",
        (sample_id,),
    ).fetchone()[0]
    return dict(
        mode=mode,
        writes=len(rows),
        tps=len(rows) / elapsed,
        latency=percentiles([(r[-1] - r[4]) * 1e-9 for r in rows]),
        wal_bytes=wal,
        functions=functions,
        sql=statements,
        transactions=rows,
        wait_samples=samples,
        folded=folded,
        write_plan=plan,
        pre_fold_read_plan=dirty_read,
        clean_read_plan=clean_read,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--order",
        default="raw,disabled,fused_empty,sync,fused,shard,queue,queue,shard,fused,sync,fused_empty,disabled,raw",
    )
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--tasks", type=int, default=48)
    parser.add_argument("--operations", type=int, default=90)
    args = parser.parse_args()
    if not 1 <= args.workers <= 32 or not args.workers <= args.tasks <= 5000 or args.operations < 1:
        parser.error("invalid size")
    modes = args.order.split(",")
    if set(modes) - set(READ) - {"fused"}:
        parser.error("unknown arm")
    with (
        TemporaryDirectory() as tmp,
        isolated_kernel(os.environ["NF_TEST_DATABASE_URL"], Path(tmp)) as (kernel, url),
    ):
        book = load_fixture(kernel)
        wr = book["world_ref"]
        branch = (wr["work_id"], wr["branch_id"])
        writer = kernel.issue_grant(book["principal"], branch[0], role_id="writer")["principal"]
        ids = []
        for i in range(args.tasks):
            spec = task_spec(writer, wr)
            spec["task_id"] = str(UUID(int=i + 1))
            ids.append(spec["task_id"])
            kernel.create_task(writer, spec, f"projection-{i}")
        dsn = make_url(url).set(drivername="postgresql").render_as_string(hide_password=False)
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS pg_stat_statements")
            # Test DB is isolated. Enable nested SQL and function timing for connections
            # created subsequently, including the four independent writers.
            conn.execute(
                sql.SQL("ALTER DATABASE {} SET track_functions TO 'pl'").format(
                    sql.Identifier(conn.info.dbname)
                )
            )
            conn.execute(
                sql.SQL("ALTER DATABASE {} SET pg_stat_statements.track TO 'all'").format(
                    sql.Identifier(conn.info.dbname)
                )
            )
            original = conn.execute(
                "SELECT pg_get_functiondef('nf_hera_count_transition()'::regprocedure)"
            ).fetchone()[0]
            conn.execute((SQL_DIR / "hera_progress_alternatives.sql").read_text())
            out = dict(
                revision=subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
                cpu_affinity=sorted(os.sched_getaffinity(0)),
                server=conn.execute("SELECT version()").fetchone()[0],
                workers=args.workers,
                tasks=args.tasks,
                operations_per_worker=args.operations,
                order=modes,
                arms=[],
            )
            for mode in modes:
                prepare(conn, mode, original, branch, kernel.db)
                arm = run_arm(dsn, conn, mode, branch, ids, args.workers, args.operations)
                out["arms"].append(arm)
                print(mode, round(arm["tps"], 2), arm["latency"], arm["functions"], flush=True)
                with gzip.open(args.output, "wt") as out_file:
                    json.dump(out, out_file, default=str, separators=(",", ":"))


if __name__ == "__main__":
    main()
