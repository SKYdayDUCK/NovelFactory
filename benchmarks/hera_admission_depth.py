"""Profile committed task creation, queue storage and occupancy scans on dedicated PG databases."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import gzip
import json
import os
from pathlib import Path
import resource
import subprocess
from tempfile import TemporaryDirectory
from time import perf_counter

from sqlalchemy import text

from benchmarks.hera_mixed import population, task_spec
from benchmarks.http_load import percentiles
from benchmarks.p2_behavioral_load import isolated_kernel
from novel_factory.application.kernel import Kernel
from novel_factory.infrastructure.database import Database
from novel_factory.orchestration.durable_admission import _OCCUPANCY


DEPTHS = (0, 50, 200, 500, 1000, 2000, 5000)


def memory(pid):
    path = Path(f"/proc/{pid}/status")
    return {line.split(":")[0]: int(line.split()[1])
            for line in path.read_text().splitlines() if line.startswith(("VmRSS:", "VmHWM:"))}


def snapshot(kernel, owner, wr, enabled):
    with kernel.db.uow(write=False) as repo:
        conn = repo.conn
        relations = {name: {"total": total, "indexes": indexes} for name, total, indexes in conn.execute(text("""
            SELECT relname, pg_total_relation_size(oid), pg_indexes_size(oid)
            FROM pg_class WHERE relkind='r' AND relnamespace='public'::regnamespace
        """))}
        backend_memory = {str(pid): memory(pid) for pid in conn.scalars(text("""
            SELECT pid FROM pg_stat_activity WHERE datname=current_database() AND backend_type='client backend'
        """))}
        sizes = conn.execute(text("""
            SELECT pg_database_size(current_database()),count(*),
                count(*) FILTER(WHERE state='READY'),current_setting('server_version') FROM nf_tasks
        """)).one()
        plan = (conn.scalar(text("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + _OCCUPANCY.text),
                            {"work": wr["work_id"], "principal": owner.principal_id, "resource": "MODEL"})
                if enabled else None)
    return {"relations": relations, "database_bytes": sizes[0], "task_rows": sizes[1], "ready_rows": sizes[2],
            "postgres_version": sizes[3], "backends_kib": backend_memory, "harness_kib": memory(os.getpid()),
            "harness_maxrss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss, "occupancy_plan": plan}


def submit(kernel, principals, i):
    identity, wr = principals[i % len(principals)]
    owner = identity["principal"]
    spec = task_spec(owner, wr)
    task_id = spec["task_id"]
    begin = perf_counter()
    receipt = kernel.create_task(owner, spec, task_id)
    duration = perf_counter() - begin
    assert (receipt["task_id"], receipt["generation"], receipt["state"]) == (task_id, 0, "READY")
    return {"task": task_id, "principal": owner.principal_id, "work": wr["work_id"],
            "create_ms": duration * 1000, "state": receipt["state"]}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    result = {"sha": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
              "pool_per_arm": 4, "fill_workers": 4, "active_fills": 1,
              "probe_order": "ABBA x4 per checkpoint, A disabled/B enabled", "checkpoints": {}}
    with TemporaryDirectory() as directory, ExitStack() as stack:
        kernels, actors = {}, {}
        for arm in ("disabled", "enabled"):
            setup, url = stack.enter_context(isolated_kernel(os.environ["NF_TEST_DATABASE_URL"], Path(directory)/arm))
            setup.db.close()
            db = Database(url, pool_size=4)
            stack.callback(db.close)
            kernel = Kernel(db, setup.objects)
            principals = population(kernel, 16)
            kernels[arm], actors[arm] = kernel, principals
            with db.uow() as repo:
                repo.conn.execute(text("""UPDATE nf_durable_admission_policy SET enabled=:on,
                    work_limit=8000,principal_limit=8000"""), {"on": arm == "enabled"})
        last_depth = 0
        for depth in DEPTHS:
            checkpoint = {}
            for arm in kernels:
                k, principals = kernels[arm], actors[arm]
                begin = perf_counter()
                with ThreadPoolExecutor(4) as pool:
                    raw = list(pool.map(lambda i: submit(k, principals, i), range(last_depth, depth)))
                elapsed = perf_counter() - begin
                checkpoint[arm] = {"fill": raw, "fill_s": elapsed, "probes": [],
                                   "fill_latency": percentiles([r["create_ms"] / 1000 for r in raw]) if raw else {}}
            for probe, arm in enumerate(("disabled", "enabled", "enabled", "disabled") * 4):
                k = kernels[arm]
                owner_index = 0
                row = submit(k, actors[arm], owner_index)
                k.cancel_task(actors[arm][owner_index][0]["principal"], row["task"], 0, row["task"] + "-cancel")
                checkpoint[arm]["probes"].append(row)
            for arm in kernels:
                identity, wr = actors[arm][0]
                state = snapshot(kernels[arm], identity["principal"], wr, arm == "enabled")
                assert state["ready_rows"] == depth
                checkpoint[arm]["snapshot"] = state
            result["checkpoints"][depth] = checkpoint
            tmp = args.output.with_suffix(".partial")
            with gzip.open(tmp, "wt", encoding="utf8") as handle:
                json.dump(result, handle, separators=(",", ":"))
            tmp.replace(args.output)
            print("depth", depth, "ready A/B", *(checkpoint[a]["snapshot"]["ready_rows"] for a in kernels),
                  "DB bytes A/B", *(checkpoint[a]["snapshot"]["database_bytes"] for a in kernels), flush=True)
            last_depth = depth


if __name__ == "__main__":
    main()
