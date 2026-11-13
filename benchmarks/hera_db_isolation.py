"""Two durable, authorized index workers and MCP/REST readers on the same PG pool."""
import argparse
import asyncio
import gzip
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Lock
from time import perf_counter

from sqlalchemy import text

from benchmarks.hera_mixed import population, trial
from benchmarks.p2_behavioral_load import hardware, isolated_kernel
from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.derived_worker import DerivedWorldWorker
from novel_factory.application.kernel import Kernel
from novel_factory.application.supervisor import RuntimeSupervisor
from novel_factory.fixtures import load_fixture, point, run_command
from novel_factory.infrastructure.database import Database
from novel_factory.orchestration.processors import TaskDispatcher


def measured(kernel, actors, loaded, *, slots=None, mcp_percent=50):
    if not loaded:
        return {"loaded": False, "read": asyncio.run(trial(kernel, actors, 100, mcp_percent, True, "read"))}
    book = load_fixture(kernel)
    principal, wr, ids = book["principal"], book["world_ref"], book["ids"]
    for i in range(100):
        wr = run_command(kernel, principal, wr, "RESOURCE_TRANSFER", {
            "from_account": ids["warehouse"], "to_account": ids["relief"], "resource_id": ids["grain"],
            "unit": "bag", "quantity": "1", "at": point(i+30)})["world_ref"]
    scope = principal, wr["work_id"], wr["branch_id"]
    TaskDispatcher(RuntimeSupervisor(kernel, AgentGateway(kernel, {})), *scope).switch("redis")
    if slots is not None:
        with kernel.db.uow() as repo:
            repo.conn.execute(text("INSERT INTO nf_dispatch_limits VALUES ('RESOURCE','CPU_COMPUTE',:slots) "
                                   "ON CONFLICT(scope,subject) DO UPDATE SET slots=EXCLUDED.slots"), {"slots": slots})
    go = Event()
    done = Event()
    count = 0
    counter_lock = Lock()
    measured_start = 0.0

    def begin(t0):
        nonlocal measured_start
        measured_start = t0
        go.set()

    def worker():
        nonlocal count
        rows = []
        if not go.wait(120):
            raise TimeoutError("Read trial failed to initialize")
        service = DerivedWorldWorker(kernel)
        while not done.is_set():
            if perf_counter() - measured_start > 120:
                raise TimeoutError("Index drain stalled")
            started = perf_counter()
            receipt = service.rebuild_one(*scope)
            if not receipt:
                if slots is None:
                    return rows
                done.wait(.05)
                continue
            rows.append([receipt["message_id"],started-measured_start,perf_counter()-measured_start])
            with counter_lock:
                count += 1
                if count == 100:
                    done.set()
        return rows

    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(worker) for _ in range(2)]
        try:
            reads = asyncio.run(trial(kernel, actors, 100, mcp_percent, True, "read", on_start=begin))
        finally:
            go.set()
        rows = [row for f in futures for row in f.result()]
    assert len(set(row[0] for row in rows)) == len(rows) == 100
    with kernel.db.uow(write=False) as repo:
        water = repo.one("index_watermarks", {"branch_id": wr["branch_id"], "index_name":"timeline"})
        assert str(water["world_revision_id"]) == wr["world_revision_id"]
    last = reads["seconds"]
    return {"loaded": True, "index_slots": slots or 2, "index_drain_s": max(row[2] for row in rows), "read": reads, "worker_count": 2, "initially_ready": 100, "indexes": rows,
            "completed_during_reads":sum(row[2] <= last for row in rows),
            "watermark":water["world_revision_id"]}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--slot-comparison", action="store_true")
    args = p.parse_args()
    logging.getLogger("mcp").setLevel(logging.WARNING)
    with TemporaryDirectory() as tmp, isolated_kernel(os.environ["NF_TEST_DATABASE_URL"], Path(tmp)) as (setup,url):
        setup.db.close()
        db = Database(url, pool_size=4)
        try:
            kernel = Kernel(db, setup.objects)
            actors = population(kernel, 10)
            data = {"environment":hardware(), "db_limit":4, "model_calls":0, "runs":[]}
            cases = [(loaded, None, 10) for loaded in (False, True, True, False)]
            if args.slot_comparison:
                cases = [(True, slots, clients) for clients in (1,10) for slots in (2,1,1,2)]
            for loaded, slots, clients in cases:
                result = measured(kernel, actors[:clients], loaded, slots=slots, mcp_percent=50 if clients>1 else 0)
                data["runs"].append(result)
                with gzip.open(args.output,"wt",encoding="utf8") as out:
                    json.dump(data,out,separators=(",",":"))
                assert result["read"]["verified_responses"] == 100
                print(loaded,result["read"]["goodput_s"],result["read"]["latency"],flush=True)
        finally:
            db.close()


if __name__ == "__main__":
    main()
