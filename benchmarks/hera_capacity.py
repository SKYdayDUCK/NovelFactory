"""PostgreSQL 17 capacity ablation at fixed backlog on disposable databases.

Setup uses task rows and admission receipts with the same shape as occupied slots.
The timed operations are authorized Kernel creates with their real task inputs,
command records and Outbox. Each arm is reconstructed from an identical seed set.
"""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import gzip
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from threading import Event, Thread, local
from time import perf_counter

from sqlalchemy import create_engine, event, text

from benchmarks.hera_mixed import population, task_spec
from benchmarks.p2_behavioral_load import isolated_kernel
from novel_factory.application.kernel import Kernel
from novel_factory.infrastructure.database import Database

ORDER = ("scan", "exact", "exact", "scan") * 2


def setup_rows(kernel, actors, count):
    # Keep historical row/receipt distribution fixed in both arms. Timed creates
    # have their complete normal input/command/outbox fanout.
    with kernel.db.uow() as r:
        conn = r.conn
        for i, (identity, wr) in enumerate(actors):
            n = (count + len(actors) - 1 - i) // len(actors)
            conn.execute(text("""
                WITH fixtures AS (
                  INSERT INTO nf_tasks(task_id,work_id,branch_id,principal_id,grant_id,
                                       role_id,task_kind,state,deadline)
                   SELECT gen_random_uuid(),:work,:branch,:principal,:grant,'writer','hera_load',
                          'READY',clock_timestamp()+interval '2 hours' FROM generate_series(1,:n)
                   RETURNING task_id)
                INSERT INTO nf_durable_admissions(task_id,resource_class,queue_deadline)
                SELECT task_id,'MODEL',clock_timestamp()+interval '1 hour' FROM fixtures
            """), dict(work=wr["work_id"], branch=wr["branch_id"], principal=identity["principal"].principal_id,
                       grant=identity["principal"].grant_id, n=n))
        conn.exec_driver_sql("ANALYZE nf_tasks")
        conn.exec_driver_sql("ANALYZE nf_durable_admissions")


def attach_timings(engine):
    traces = local()

    @event.listens_for(engine, "before_cursor_execute")
    def start(conn, cursor, statement, parameters, context, executemany):
        context.capacity_started = perf_counter()

    @event.listens_for(engine, "after_cursor_execute")
    def end(conn, cursor, statement, parameters, context, executemany):
        if getattr(traces, "sql", None) is not None:
            traces.sql.append((" ".join(statement.split()), (perf_counter()-context.capacity_started)*1000))

    return traces


def observe(engine, stop, waits):
    with engine.connect() as c:
        while not stop.is_set():
            rows = c.exec_driver_sql("""SELECT wait_event_type,wait_event, count(*),
                (array_agg(left(query,100)))[1] FROM pg_stat_activity
                WHERE datname=current_database() AND pid<>pg_backend_pid() AND state='active'
                GROUP BY wait_event_type,wait_event""").all()
            waits.append((perf_counter(), [tuple(row) for row in rows]))
            stop.wait(.02)


def snapshot(engine):
    with engine.connect() as c:
        pgss = [dict(row) for row in c.exec_driver_sql("""SELECT query, toplevel, calls,total_exec_time,
            rows, shared_blks_hit,shared_blks_read,wal_bytes FROM pg_stat_statements
            WHERE dbid=(SELECT oid FROM pg_database WHERE datname=current_database()) AND calls>0
            ORDER BY total_exec_time DESC""").mappings()]
        functions = [dict(row) for row in c.exec_driver_sql("""SELECT funcname,calls,total_time,self_time
            FROM pg_stat_user_functions ORDER BY funcname""").mappings()]
        return {"sql": pgss, "functions": functions,
                "wal": c.exec_driver_sql("SELECT pg_current_wal_insert_lsn()::text").scalar_one()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--count", type=int, default=40)
    parser.add_argument("--depth", nargs="+", type=int, default=[0,5000])
    args = parser.parse_args()
    output = {"commit": subprocess.check_output(["git","rev-parse","HEAD"], text=True).strip(),
              "affinity": sorted(os.sched_getaffinity(0)), "pool": 4, "observer_connections": 1,
              "order": ORDER, "count": args.count, "schema": "0027_hera_capacity", "runs": []}
    for depth in args.depth:
        with ExitStack() as stack:
            directory = Path(stack.enter_context(TemporaryDirectory()))
            systems = {}
            for mode in ("scan", "exact"):
                k, url = stack.enter_context(isolated_kernel(os.environ["NF_TEST_DATABASE_URL"], directory/mode))
                k.db.close()
                db = Database(url, pool_size=4)
                stack.callback(db.close)
                k = Kernel(db,k.objects)
                actors = population(k, 8)
                setup_rows(k, actors, depth)
                monitor = create_engine(url, isolation_level="AUTOCOMMIT", pool_size=1,max_overflow=0)
                stack.callback(monitor.dispose)
                with db.uow() as r:
                    r.conn.exec_driver_sql("CREATE EXTENSION pg_stat_statements")
                    r.conn.exec_driver_sql("UPDATE nf_durable_admission_policy SET enabled=true,work_limit=10000,principal_limit=10000")
                    r.conn.execute(text("SELECT nf_hera_set_capacity_mode(:on)"), {"on": mode=="exact"})
                    output["postgres_version"] = r.conn.exec_driver_sql("SHOW server_version").scalar_one()
                systems[mode] = k, actors, monitor, attach_timings(db.engine)
            cycles = Counter()
            for mode in ORDER:
                k, actors, monitor, traces = systems[mode]
                specs = [(actors[i%8][0]["principal"],task_spec(actors[i%8][0]["principal"],actors[i%8][1]))
                         for i in range(args.count)]
                def submit(pair):
                    who, spec = pair
                    traces.sql = []
                    start = perf_counter()
                    receipt = k.create_task(who, spec, spec["task_id"])
                    end = perf_counter()
                    assert receipt["task_id"] == spec["task_id"] and receipt["state"] == "READY"
                    return {"task": receipt["task_id"], "start": start, "end": end, "sql": traces.sql}

                waits = []
                stop = Event()
                thread = Thread(target=observe,args=(monitor,stop,waits))
                before = snapshot(monitor)
                thread.start()
                try:
                    start = perf_counter()
                    with ThreadPoolExecutor(4) as pool:
                        requests = list(pool.map(submit,specs))
                    elapsed = perf_counter()-start
                finally:
                    stop.set()
                    thread.join()
                for who,spec in specs:
                    k.cancel_task(who,spec["task_id"],0,"cancel:"+spec["task_id"])
                with k.db.uow(write=False) as r:
                    assert r.conn.exec_driver_sql("SELECT count(*) FROM nf_tasks WHERE state='READY'").scalar_one() == depth
                    if mode == "exact":
                        n = r.conn.exec_driver_sql("SELECT used FROM nf_hera_capacity WHERE scope='total'").scalar_one()
                        assert n == depth
                after = snapshot(monitor)
                output["runs"].append({"mode": mode,"depth": depth,"round": cycles[mode],
                    "elapsed_s": elapsed,"requests": requests,"backend_waits": waits,
                    "stats_before": before, "stats_after_including_release": after})
                cycles[mode] += 1
                with gzip.open(args.output,"wt") as f:
                    json.dump(output,f,default=str,separators=(",",":"))
                print(mode,depth,round(args.count/elapsed,2),flush=True)


if __name__ == "__main__":
    main()
