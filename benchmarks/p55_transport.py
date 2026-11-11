"""Bounded two-phase Redis/PG transport benchmark with independent processes.

Use scripts/with_postgres.py and a disposable NF_TEST_REDIS_URL. The benchmark
creates its own database and uniquely named Redis keys, then removes only these.
"""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from uuid import uuid4

from redis import Redis
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.event_streams import STREAMS, Streams
from novel_factory.infrastructure.eventhub import EventHub

_PROCESS = {}


def namespace(prefix):
    for key in STREAMS:
        STREAMS[key] = f"nf:test:{prefix}:{key}:v1"


def open_worker(url, redis_url, prefix, gate):
    namespace(prefix)
    _PROCESS.update(
        hub=EventHub(
            Database(url, pool_size=1),
            Redis.from_url(redis_url, decode_responses=True, max_connections=1),
        ),
        name="load-" + str(os.getpid()),
        gate=gate,
    )


def record(conn, event):
    conn.execute(text("INSERT INTO nf_load_effects(event_id) VALUES(:id)"), {"id": event.event_id})


def run_stage(stage):
    hub = _PROCESS["hub"]
    _PROCESS["gate"].wait(timeout=30)
    start = time.perf_counter()
    n = 0
    while True:
        if stage == "relay":
            batch = hub.relay_once(32)
        elif stage == "consume":
            batch = hub.consume_once("statistics", _PROCESS["name"], record, limit=32)
        else:
            batch = 0
        if not batch:
            return (start, time.perf_counter(), n)
        n += batch


def phase(pool, procs, stage, client):
    before = client.info("cpu")
    jobs = [pool.submit(run_stage, stage) for _ in range(procs)]
    workers = [job.result() for job in jobs]
    after = client.info("cpu")
    duration = max(end for _, end, _ in workers) - min(start for start, _, _ in workers)
    n = sum(count for _, _, count in workers)
    return {
        "phase_seconds": round(duration, 5),
        "observed_entries": n,
        "entries_per_s": round(n / duration, 2),
        "per_process_entries": [count for _, _, count in workers],
        "redis_cpu_ms": round(
            1000 * sum(after[k] - before[k] for k in ("used_cpu_sys", "used_cpu_user")), 3
        ),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    base = make_url(os.environ["NF_TEST_DATABASE_URL"])
    redis_url = os.environ["NF_TEST_REDIS_URL"]
    admin = create_engine(base.set(database="postgres"), isolation_level="AUTOCOMMIT")
    prefix = uuid4().hex[:16]
    name = "nf_load_" + prefix
    url = base.set(database=name).render_as_string(hide_password=False)
    client = Redis.from_url(redis_url, decode_responses=True)
    namespace(prefix)
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    db = Database(url, pool_size=1)
    procs = 4
    context = mp.get_context("spawn")
    try:
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            check=True,
            env={**os.environ, "NF_DATABASE_URL": url},
        )
        with db.uow() as repo:
            repo.conn.execute(
                text(
                    "CREATE TABLE nf_load_effects(event_id uuid NOT NULL,finished timestamptz DEFAULT clock_timestamp())"
                )
            )
            pg_version = repo.conn.scalar(text("SHOW server_version"))
        stream = STREAMS["observability"]
        Streams(client).register("observability", "statistics")
        results = []
        with ProcessPoolExecutor(
            max_workers=procs,
            mp_context=context,
            initializer=open_worker,
            initargs=(url, redis_url, prefix, context.Barrier(procs)),
        ) as pool:
            phase(pool, procs, "warm", client)
            for n in (200, 500, 1000):
                with db.uow() as repo:
                    ids = []
                    for _ in range(n):
                        ident = str(uuid4())
                        ids.append(
                            str(repo.outbox("audit.recorded", "audit", ident, {}, "load:" + ident))
                        )
                with db.uow(write=False) as repo:
                    start = repo.conn.scalar(text("SELECT clock_timestamp()"))
                relay = phase(pool, procs, "relay", client)
                lag_before = client.xinfo_groups(stream)[0]["lag"]
                consume = phase(pool, procs, "consume", client)
                with db.uow(write=False) as repo:
                    effects = (
                        repo.conn.execute(
                            text("""
                        SELECT count(*) n,count(distinct event_id) distinct_n,
                            percentile_cont(.95) WITHIN GROUP(ORDER BY EXTRACT(EPOCH FROM finished-CAST(:start AS timestamptz))*1000) p95,
                            percentile_cont(.99) WITHIN GROUP(ORDER BY EXTRACT(EPOCH FROM finished-CAST(:start AS timestamptz))*1000) p99
                        FROM nf_load_effects WHERE event_id=ANY(CAST(:ids AS uuid[]))
                    """),
                            {"ids": ids, "start": start},
                        )
                        .mappings()
                        .one()
                    )
                group = client.xinfo_groups(stream)[0]
                if (
                    relay["observed_entries"],
                    consume["observed_entries"],
                    effects["n"],
                    effects["distinct_n"],
                    group["pending"],
                    group["lag"],
                ) != (n, n, n, n, 0, 0):
                    raise RuntimeError("Transport lost or duplicated effects, or failed to drain")
                results.append(
                    {
                        "initial_committed_events": n,
                        "relay": relay,
                        "consumer": consume,
                        "peak_group_lag": lag_before,
                        "end_pending": group["pending"],
                        "end_lag": group["lag"],
                        "distinct_sql_effects": effects["distinct_n"],
                        "duplicate_effects": effects["n"] - effects["distinct_n"],
                        "batch_to_effect_p95_ms": round(effects["p95"], 3),
                        "batch_to_effect_p99_ms": round(effects["p99"], 3),
                    }
                )
        report = {
            "method": "one precommitted burst at each size; sequential relay/drain phases; warm processes",
            "postgres": pg_version,
            "redis": client.info("server")["redis_version"],
            "workers_per_phase": procs,
            "per_worker_pg_pool": 1,
            "redis_batch": 32,
            "traffic": "synthetic audit.recorded reference + one SQL witness",
            "world_accepts": 0,
            "model_calls": 0,
            "capacity_gate": "NOT_MET (existing Agent workload, not evaluated here)",
            "results": results,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report))
    finally:
        db.close()
        client.delete(*STREAMS.values())
        client.close()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        admin.dispose()


if __name__ == "__main__":
    main()
