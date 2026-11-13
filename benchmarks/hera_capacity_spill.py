"""Four-scope PG17 quota experiment with exact limits and recorded spill destinations.

A task records one physical bucket per scope. Writers acquire scopes in a fixed order.
Use a disposable DB URL. No product tables are changed.
"""
import argparse
from concurrent.futures import ProcessPoolExecutor
from contextlib import closing
import gzip
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
from time import monotonic_ns
from uuid import uuid4

import psycopg
from sqlalchemy.engine import make_url

SCOPES = ("total", "work", "principal", "resource")
PICK = """
    SELECT bucket FROM slots WHERE scope=%s AND subject=%s AND used<ceiling
    ORDER BY bucket<>%s,bucket LIMIT 1 FOR UPDATE
"""
UPDATE = """
    UPDATE slots SET used=used+1 WHERE scope=%s AND subject=%s AND bucket=%s
    AND used<ceiling RETURNING bucket
"""


class Full(Exception):
    pass


def run_lane(dsn, lane, nclients, count, buckets, mode, skew):
    rows = []
    with psycopg.connect(dsn, autocommit=True) as db:
        for i in range(lane, count, nclients):
            preferred = 0 if skew else i % buckets
            scopes = list(zip(SCOPES, ("*", f"w{i % 2}", f"p{i % 8}", "MODEL")))
            start = monotonic_ns()
            assigned = []
            try:
                with db.transaction():
                    for scope, subject in scopes:
                        bucket = preferred
                        if mode == "spill":
                            row = db.execute(PICK + " SKIP LOCKED", (scope, subject, preferred)).fetchone()
                            if row is None:
                                # Occupied locks alone must never cause an under-capacity rejection.
                                row = db.execute(PICK, (scope, subject, preferred)).fetchone()
                            if row is None:
                                raise Full
                            bucket = row[0]
                        if not db.execute(UPDATE, (scope, subject, bucket)).fetchone():
                            raise Full
                        assigned.append((scope, subject, bucket))
                    with db.cursor() as cur:
                        cur.executemany("INSERT INTO allocations VALUES (%s,%s,%s,%s)",
                                        [(i, *destination) for destination in assigned])
                accepted = True
            except Full:
                accepted = False
            rows.append((i, start, monotonic_ns(), accepted))
    return rows


def arm(db, dsn, *, mode, buckets, skew, count, capacity, clients):
    db.execute("TRUNCATE slots, allocations")
    for scope, subjects in zip(SCOPES, (["*"], ["w0", "w1"], [f"p{i}" for i in range(8)], ["MODEL"])):
        for subject in subjects:
            db.execute("""INSERT INTO slots SELECT %s,%s,n,0,%s::int/%s::int+(n<%s::int)::int
                          FROM generate_series(0,%s::int-1) n""",
                       (scope, subject, capacity, buckets, capacity % buckets, buckets))
    with ProcessPoolExecutor(clients, mp_context=multiprocessing.get_context("spawn")) as pool:
        jobs = [pool.submit(run_lane, dsn, i, clients, count, buckets, mode, skew) for i in range(clients)]
        rows = [r for job in jobs for r in job.result()]
    ok = sum(row[3] for row in rows)
    mismatch = db.execute("""SELECT count(*) FROM slots s LEFT JOIN (
        SELECT scope,subject,bucket,count(*) n FROM allocations GROUP BY scope,subject,bucket) a
        USING(scope,subject,bucket) WHERE s.used<>coalesce(a.n,0)""").fetchone()[0]
    receipts = db.execute("SELECT count(*) FROM allocations").fetchone()[0]
    # Release from the recorded physical bucket even if that writer no longer exists.
    with db.transaction():
        db.execute("""WITH released AS (DELETE FROM allocations RETURNING scope,subject,bucket),
            counts AS (SELECT scope,subject,bucket,count(*) n FROM released GROUP BY scope,subject,bucket)
            UPDATE slots s SET used=used-c.n FROM counts c
            WHERE (s.scope,s.subject,s.bucket)=(c.scope,c.subject,c.bucket)""")
    left = db.execute("SELECT sum(used) FROM slots").fetchone()[0]
    assert (receipts, mismatch, left) == (4*ok, 0, 0)
    assert ok <= capacity
    if mode == "spill" or buckets == 1:
        assert ok == min(capacity, count)
    seconds = (max(r[2] for r in rows)-min(r[1] for r in rows))/1e9
    return dict(mode=mode, buckets=buckets, skew=skew, seconds=seconds, accepted=ok,
                correct_admissions_s=ok/seconds, receipts=receipts, mismatched=mismatch, after_release=int(left),
                rows=sorted(rows))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--capacity", type=int, default=128)
    parser.add_argument("--attempts", type=int, default=160)
    args = parser.parse_args()
    if args.capacity < 8 or args.attempts < args.capacity:
        parser.error("Use a positive capacity of at least eight and enough attempts to fill it")
    url = make_url(os.environ["NF_TEST_DATABASE_URL"]).set(drivername="postgresql")
    name = "nf_hera_spill_" + uuid4().hex
    dsn = url.set(database=name).render_as_string(hide_password=False)
    report = dict(source=subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
                  clients=4, capacity=args.capacity, attempts=args.attempts, affinity=sorted(os.sched_getaffinity(0)), arms=[])
    with psycopg.connect(url.set(database="postgres").render_as_string(hide_password=False), autocommit=True) as admin:
        admin.execute("CREATE DATABASE " + name)
        try:
            with closing(psycopg.connect(dsn, autocommit=True)) as db:
                report["postgres"] = db.execute("SHOW server_version").fetchone()[0]
                db.execute("""CREATE TABLE slots(scope text,subject text,bucket int,used int,ceiling int,
                    PRIMARY KEY(scope,subject,bucket),CHECK(used BETWEEN 0 AND ceiling));
                    CREATE TABLE allocations(task int,scope text,subject text,bucket int,PRIMARY KEY(task,scope))""")
                for skew in (False, True):
                    for mode, buckets in [("fixed",1),("fixed",8),("spill",8),("spill",8),("fixed",8),("fixed",1)]:
                        result = arm(db, dsn, mode=mode, buckets=buckets, skew=skew, count=args.attempts,
                                     capacity=args.capacity, clients=4)
                        report["arms"].append(result)
                        with gzip.open(args.output,"wt") as f:
                            json.dump(report,f,separators=(",",":"))
                        print(mode, buckets, skew, result["accepted"], result["correct_admissions_s"], flush=True)
        finally:
            admin.execute("DROP DATABASE " + name + " WITH (FORCE)")


if __name__ == "__main__":
    main()
