"""Measure a 2 -> 1 -> 2 operator limit change with existing index leases and task renewals."""

import argparse
import gzip
import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from time import perf_counter_ns

from sqlalchemy import text

from benchmarks.hera_mixed import task_spec
from benchmarks.p2_behavioral_load import isolated_kernel
from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.derived_worker import DerivedWorldWorker
from novel_factory.application.supervisor import RuntimeSupervisor
from novel_factory.fixtures import load_fixture, point, run_command
from novel_factory.orchestration.processors import TaskDispatcher


def run(kernel):
    book = load_fixture(kernel)
    principal, wr, ids = book["principal"], book["world_ref"], book["ids"]
    for i in range(8):
        wr = run_command(
            kernel,
            principal,
            wr,
            "RESOURCE_TRANSFER",
            {
                "from_account": ids["warehouse"],
                "to_account": ids["relief"],
                "resource_id": ids["grain"],
                "unit": "bag",
                "quantity": "1",
                "at": point(i + 30),
            },
        )["world_ref"]
    scope = principal, wr["work_id"], wr["branch_id"]
    TaskDispatcher(RuntimeSupervisor(kernel, AgentGateway(kernel, {})), *scope).switch("redis")
    writer = kernel.issue_grant(principal, wr["work_id"], role_id="writer")["principal"]
    spec = task_spec(writer, wr)
    kernel.create_task(writer, spec, "renew-resize")
    running = kernel.claim_task(writer, "probe", task_id=spec["task_id"], lease_seconds=10)
    assert running["generation"] == 1

    def limit(n):
        began = perf_counter_ns()
        with kernel.db.uow() as repo:
            repo.conn.execute(
                text("""INSERT INTO nf_dispatch_limits VALUES('RESOURCE','CPU_COMPUTE',:slots)
              ON CONFLICT(scope,subject) DO UPDATE SET slots=EXCLUDED.slots"""),
                {"slots": n},
            )
        return [n, began, perf_counter_ns()]

    def depth():
        with kernel.db.uow(write=False) as repo:
            return repo.conn.scalar(
                text("SELECT count(*) FROM nf_index_leases WHERE expires_at>statement_timestamp()")
            )

    svc = DerivedWorldWorker(kernel)
    limit(2)
    first = svc.reserve_index(*scope)
    second = svc.reserve_index(*scope)
    assert first and second and depth() == 2
    assert svc.reserve_index(*scope) is None
    gate_a, gate_b, started_a, started_b, stop = Event(), Event(), Event(), Event(), Event()
    renewals = []

    def worker(lease, enter, release):
        enter.set()
        assert release.wait(30)
        begin = perf_counter_ns()
        res = svc.process_reserved(*scope, lease)
        end = perf_counter_ns()
        assert res["consumed"] and res["message_id"] == lease["outbox_id"]
        return [res["message_id"], begin, end]

    def renew():
        while not stop.is_set():
            start = perf_counter_ns()
            expiry = kernel.tasks.renew(spec["task_id"], 1, "probe", lease_seconds=10)
            renewals.append([start, perf_counter_ns(), expiry.isoformat()])
            stop.wait(0.001)

    transfers = []
    with ThreadPoolExecutor(3) as threads:
        a = threads.submit(worker, first, started_a, gate_a)
        b = threads.submit(worker, second, started_b, gate_b)
        future = threads.submit(renew)
        try:
            assert started_a.wait(30) and started_b.wait(30)
            lower = limit(1)
            assert depth() == 2 and svc.reserve_index(*scope) is None
            # Release one actual nonpreemptible lease. Keep the other live so
            # the one-slot boundary can be observed deterministically.
            gate_a.set()
            transfers.append(a.result(timeout=30))
            assert depth() == 1 and svc.reserve_index(*scope) is None
            gate_b.set()
            transfers.append(b.result(timeout=30))
            assert depth() == 0
            claim_start = perf_counter_ns()
            third = svc.reserve_index(*scope)
            claim_end = perf_counter_ns()
            assert third and depth() == 1 and svc.reserve_index(*scope) is None
            upper = limit(2)
            fourth = svc.reserve_index(*scope)
            second_available = perf_counter_ns()
            assert fourth and depth() == 2 and svc.reserve_index(*scope) is None
        finally:
            stop.set()
            gate_a.set()
            gate_b.set()
        future.result()
    assert renewals
    for lease in (third, fourth):
        start = perf_counter_ns()
        res = svc.process_reserved(*scope, lease)
        transfers.append([res["message_id"], start, perf_counter_ns()])
    for _ in range(4):
        start = perf_counter_ns()
        res = svc.rebuild_one(*scope)
        transfers.append([res["message_id"], start, perf_counter_ns()])
    assert depth() == 0 and len({r[0] for r in transfers}) == 8
    with kernel.db.uow(write=False) as repo:
        water = repo.one(
            "index_watermarks", {"branch_id": wr["branch_id"], "index_name": "timeline"}
        )
        assert str(water["world_revision_id"]) == wr["world_revision_id"]
    return {
        "lower": lower,
        "upper": upper,
        "index_receipts": transfers,
        "renewals": renewals,
        "verified_watermark": str(water["world_revision_id"]),
        "after_lower_first_lease_done_ms": (transfers[0][2] - lower[1]) / 1e6,
        "after_lower_second_lease_done_ms": (transfers[1][2] - lower[1]) / 1e6,
        "after_lower_new_permit_ms": (claim_end - lower[1]) / 1e6,
        "after_upper_extra_permit_ms": (second_available - upper[1]) / 1e6,
        "reacquire_call_ms": (claim_end - claim_start) / 1e6,
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    out = {
        "source": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "cpus": sorted(os.sched_getaffinity(0)),
        "runs": [],
    }
    for _ in range(3):
        with (
            TemporaryDirectory() as root,
            isolated_kernel(os.environ["NF_TEST_DATABASE_URL"], Path(root)) as (kernel, _),
        ):
            res = run(kernel)
        out["runs"].append(res)
        with gzip.open(args.output, "wt") as f:
            json.dump(out, f, separators=(",", ":"))
        print(
            res["after_lower_first_lease_done_ms"],
            res["after_lower_new_permit_ms"],
            res["after_upper_extra_permit_ms"],
            len(res["renewals"]),
            flush=True,
        )


if __name__ == "__main__":
    main()
