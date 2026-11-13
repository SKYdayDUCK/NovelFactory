"""PostgreSQL mechanism test for fixed stripes under strict multidimensional quotas.

Each scope's capacity is distributed across non-transferable buckets. This bounds
the sum exactly but can strand capacity. No product changes or transport claims.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import gzip
import json
import os
from pathlib import Path
import subprocess
from threading import Barrier
from time import perf_counter

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

STRICT_STRIPES = text("""
    UPDATE slots SET used=used+1 WHERE bucket=:bucket AND used<ceiling AND
      (scope,subject) IN (('total','*'),('work',:work),('principal',:principal),('resource','MODEL'))
    RETURNING scope
""")


def run(db, stripes, skew, total, requests):
    with db.begin() as c:
        c.exec_driver_sql("TRUNCATE slots")
        for scope, subjects, ceiling in (("total", ["*"],total),("work",["w0","w1"],total),
                                         ("principal",[f"p{i}" for i in range(8)],total),
                                         ("resource",["MODEL"],total)):
            for subject in subjects:
                c.execute(text("""INSERT INTO slots(scope,subject,bucket,used,ceiling)
                    SELECT :scope,:subject,i,0,:limit/:n+(i<:remainder)::int FROM generate_series(0,:n-1) i"""),
                    dict(scope=scope,subject=subject,limit=ceiling,n=stripes,remainder=ceiling%stripes))
    barrier = Barrier(4)
    def worker(lane):
        barrier.wait(5)
        out = []
        for i in range(lane,requests,4):
            start = perf_counter()
            with db.connect() as c:
                # Explicit ordering avoids cross-scope deadlocks in the mechanism
                # prototype while testing the cost of a fixed quota partition.
                bucket = (0 if skew else i) % stripes
                keys = dict(bucket=bucket,work=f"w{i%2}",principal=f"p{i%8}")
                c.execute(text("""SELECT scope FROM slots WHERE scope='total' AND subject='*'
                    AND bucket=:bucket FOR UPDATE"""), keys)
                n = len(c.execute(STRICT_STRIPES, keys).all())
                if n == 4:
                    c.commit()
                else:
                    c.rollback()
            out.append({"i":i,"bucket":bucket,"accepted":n==4,"elapsed_ms":(perf_counter()-start)*1000})
        return out
    t = perf_counter()
    with ThreadPoolExecutor(4) as p:
        raw = [row for lane in p.map(worker,range(4)) for row in lane]
    elapsed = perf_counter()-t
    with db.connect() as c:
        counts = [list(r) for r in c.exec_driver_sql("""SELECT scope,subject,sum(used)::int FROM slots
                    GROUP BY scope,subject ORDER BY scope,subject""")]
    accepted = sum(row["accepted"] for row in raw)
    assert accepted == next(n for scope,subject,n in counts if scope=="total")
    assert all(n<=total for _,_,n in counts)
    return dict(stripes=stripes,skew=skew,capacity=total,attempts=requests,accepted=accepted,
                elapsed_s=elapsed, counts=counts,raw=raw)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path,required=True)
    a = p.parse_args()
    # Dedicated PostgreSQL database per invocation. No writes to application data.
    from uuid import uuid4
    url = make_url(os.environ["NF_TEST_DATABASE_URL"])
    admin = create_engine(url.set(database="postgres"),isolation_level="AUTOCOMMIT")
    name = "nf_stripes_"+uuid4().hex[:16]
    with admin.connect() as c:
        c.exec_driver_sql(f'CREATE DATABASE "{name}"')
    db = create_engine(url.set(database=name),pool_size=4,max_overflow=0)
    try:
        with db.begin() as c:
            c.exec_driver_sql("""CREATE TABLE slots(scope text,subject text,bucket int,used int,
                ceiling int,PRIMARY KEY(scope,subject,bucket),CHECK(used BETWEEN 0 AND ceiling))""")
            version = c.exec_driver_sql("SHOW server_version").scalar_one()
        cases = [run(db,n,skew,128,160) for skew in (False,True) for n in (1,8,8,1)]
        with gzip.open(a.output,"wt") as f:
            json.dump({"commit":subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip(),
                "postgres":version,"affinity":sorted(os.sched_getaffinity(0)),"runs":cases},f)
    finally:
        db.dispose()
        with admin.connect() as c:
            c.exec_driver_sql(f'DROP DATABASE "{name}" WITH (FORCE)')
        admin.dispose()


if __name__=="__main__":
    main()
