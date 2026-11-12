"""Drain two real PostgreSQL work queues with distinct role grants and a metered fixture adapter.

Each workflow has one durable task and one principal. The first task of each
book is an accepted prerequisite. Every fifth subsequent task sleeps on it.
Each survivor must produce an authorized, stored result or exactly one injected
UNKNOWN. Run in a disposable database with NF_TEST_DATABASE_URL.
"""
import argparse
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import platform
import resource
import subprocess
import tempfile
import threading
import time

from sqlalchemy import event, text

from agent_runtime.protocol import ModelCallFailure, ModelResponse
from benchmarks.http_load import percentiles
from benchmarks.p2_behavioral_load import isolated_kernel
from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.runtime_tasks import RuntimeTaskService
from novel_factory.application.supervisor import RuntimeSupervisor
from novel_factory.fixtures import load_fixture, point
from novel_factory.infrastructure.database import tables
from novel_factory.infrastructure.model_gateway import OpenAICompatibleAdapter
from novel_factory.infrastructure.repository import KernelRepository


class FixtureAdapter(OpenAICompatibleAdapter):
    def __init__(self):
        super().__init__(endpoint="http://127.0.0.1:1/v1/chat/completions", model="fixture",
                         api_key="fixture-only", allow_loopback_http=True)
        self.jobs = {}
        self.lock = threading.Lock()
        self.calls = Counter()
        self.active = self.peak = 0

    @property
    def configuration(self):
        return {**super().configuration, "execution_kind": "TEST_FIXTURE"}

    def generate(self, request):
        ctx = json.loads(request.messages[1]["content"])["context"]
        key = ctx["task_id"]
        with self.lock:
            self.calls[key] += 1
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            index = self.jobs[key]
            time.sleep(.1 if index % 7 == 0 else .005)
            if index % 100 == 37:
                raise ModelCallFailure("MODEL_RESPONSE_UNKNOWN", sent=True)
            envelope = {field: ctx[field] for field in (
                "role_id", "actor_id", "task_id", "generation", "world_ref", "context_bundle_id")}
            envelope.update(schema_version="2.1", status="SUCCEEDED", unresolved_questions=[],
                            payload={"text": "Fixture summary", "event_refs": [],
                                     "important_deltas": [], "evidence_refs": []})
            return ModelResponse(json.dumps(envelope), provider_receipt="fixture-" + key,
                                 input_tokens=11, output_tokens=7, actual_amount=Decimal("0"),
                                 currency="USD", request_sha256=request.request_sha256)
        finally:
            with self.lock:
                self.active -= 1


def trial(kernel, population, *, dispatch_profile=None):
    adapter = FixtureAdapter()
    gateway = AgentGateway(kernel, {"fixture": adapter})
    supervisor = RuntimeSupervisor(kernel, gateway)
    runtime = RuntimeTaskService(kernel, gateway)
    books = [load_fixture(kernel) for _ in range(2)]
    budgets = [kernel.tasks.create_budget(b["world_ref"]["work_id"], str(population)) for b in books]
    predecessors = {}
    jobs = {}
    checkout_peak = 0
    def checked_out(*_):
        nonlocal checkout_peak
        with adapter.lock:
            checkout_peak = max(checkout_peak, kernel.db.engine.pool.checkedout())
    event.listen(kernel.db.engine, "checkout", checked_out)
    cpu_start = time.process_time()
    started = time.monotonic()

    def provision(index):
        book = books[index % 2]
        owner, wr = book["principal"], book["world_ref"]
        waiting = index > 1 and index % 5 == 0
        begin = time.monotonic()
        job = runtime.prepare(owner, wr, role_id="evidence_maintainer", task_kind="summary",
                              story_time=point(1), subject_ids=[book["ids"]["sword"]],
                              predicates=["owner"], assignment="Summarize the visible evidence.",
                              adapter_id="fixture", queue_state="BLOCKED" if waiting else "READY",
                              deadline_seconds=14400)
        key = job.task["task_id"]
        supervisor.enroll(owner, key, manifest_ref=job.manifest_ref, knowledge_cut={"plane": "WORLD"},
                          context_tokens=20000, input_ref=job.agent_input["input_ref"],
                          budget_id=budgets[index % 2], reserve_amount="0.10",
                          token_reservation=100000, accept_summary=index < 2)
        if waiting:
            def authorize(conn):
                kernel.authorize(KernelRepository(conn, tables), owner, wr["work_id"], "ADMIN", wr["branch_id"])
            supervisor.workflow.wait(key, 0, [predecessors[index % 2]], mode="AND",
                                     work_id=wr["work_id"], branch_id=wr["branch_id"], authorize_in_tx=authorize)
        if index % 100 == 83:
            kernel.cancel_task(job.principal, key, 0, "fixture-cancel-" + key)
        return key, {"index": index, "book": index % 2, "wait": waiting,
                     "create_s": time.monotonic() - begin, "enrolled_s": time.monotonic() - started}

    for i in range(2):
        key, row = provision(i)
        predecessors[i] = key
        jobs[key] = row
    with ThreadPoolExecutor(max_workers=4) as provisioning:
        jobs.update(provisioning.map(provision, range(2, population)))
    adapter.jobs = {key: row["index"] for key, row in jobs.items()}
    # Reconstruct the service from PG. No in-flight provider request crosses this restart.
    supervisor = RuntimeSupervisor(kernel, gateway)
    args = [(b["principal"], b["world_ref"]["work_id"], b["world_ref"]["branch_id"]) for b in books]
    if dispatch_profile:
        from benchmarks.p59_dispatch_plan import compare
        return compare(kernel, args[0][2], dispatch_profile)
    pending = {}
    initial = []
    for scope in args:
        initial.append(supervisor.recover_project(*scope)["counts"])
    drain = time.monotonic()
    expected = sum(r["index"] % 100 != 83 for r in jobs.values())
    completed = 0
    for scope in args:
        first = supervisor.run_once(*scope)
        if first.get("task_id") not in predecessors.values() or first["status"] != "RESULT_STORED":
            raise RuntimeError(f"Prerequisite execution failed: {first.get('status')} {first.get('reason')}")
        row = jobs[first["task_id"]]
        row.update(status=first["status"], ended_s=time.monotonic() - started)
        completed += 1
        supervisor.advance_world(*scope)
    awakened = 0
    for scope in args:
        while True:
            summary = supervisor.recover_project(*scope)
            awakened += summary["awakened"]
            if summary["awakened"] == 0:
                break
    last_progress = time.monotonic()
    turn = 0
    peak_pending = 0
    with ThreadPoolExecutor(max_workers=2) as workers:
        while completed < expected:
            for _ in range(2):
                if len(pending) >= 2:
                    break
                scope = args[turn % 2]
                turn += 1
                before = time.monotonic()
                response = supervisor.admit_model(*scope, executor=workers)
                if not isinstance(response, dict):
                    pending[response] = before
                    peak_pending = max(peak_pending, len(pending))
            if not pending:
                if time.monotonic() - last_progress > 30:
                    break
                time.sleep(.01)
                continue
            done, _ = wait(pending, timeout=30, return_when=FIRST_COMPLETED)
            if not done:
                raise TimeoutError("Admitted fixture execution made no progress")
            for future in done:
                before = pending.pop(future)
                result = future.result()
                key = result["task_id"]
                if "ended_s" in jobs[key]:
                    raise RuntimeError("Duplicate task completion")
                jobs[key].update(status=result["status"], reason=result.get("reason"),
                                 service_s=time.monotonic() - before, ended_s=time.monotonic() - started)
                completed += 1
                last_progress = time.monotonic()
    duration = time.monotonic() - drain
    with kernel.db.uow(write=False) as repo:
        rows = repo.conn.execute(text("""
            SELECT t.task_id::text, t.state, t.principal_id, t.generation,
                   (SELECT count(*) FROM nf_task_executions e WHERE e.task_id=t.task_id) claims,
                   EXISTS(SELECT FROM nf_task_results r WHERE r.result_id=t.current_result_id
                          AND r.task_id=t.task_id AND r.generation=t.generation AND r.disposition='CURRENT'
                          AND r.payload->>'status'='SUCCEEDED'
                          AND r.payload->>'context_bundle_id'=t.context_bundle_id::text) valid_result,
                   (SELECT count(*) FROM nf_inbox i WHERE i.consumer=concat_ws(':','workflow',t.task_id,0)) wake_receipts,
                   (SELECT count(*) FROM nf_call_attempts c WHERE c.task_id=t.task_id) attempts,
                   (SELECT min(c.state) FROM nf_call_attempts c WHERE c.task_id=t.task_id) call_state,
                   (SELECT extract(epoch FROM min(x.created_at)-t.created_at)
                      FROM nf_outbox x WHERE x.aggregate_id=t.task_id::text
                      AND x.topic='task.changed' AND x.payload->>'state'='RUNNING') queue_s
            FROM nf_tasks t
        """)).mappings().all()
        slots = repo.conn.scalar(text("SELECT count(*) FROM nf_dispatch_slots"))
        head_unchanged = all(kernel.head(repo, *scope[1:]) == b["world_ref"] for scope, b in zip(args, books))
    errors = []
    states = Counter()
    owners = set()
    for r in rows:
        key = r["task_id"]
        if key not in jobs:
            continue
        job = jobs[key]
        index = job["index"]
        cancel = index % 100 == 83
        lost = index % 100 == 37
        state = "CANCELLED" if cancel else "PAUSED" if lost else "ACCEPTED" if index < 2 else "RESULT_STORED"
        owners.add(r["principal_id"])
        states[r["state"]] += 1
        job.update(task_id=key, generation=r["generation"], claims=r["claims"], valid_result=r["valid_result"],
                   wake_receipts=r["wake_receipts"], state=r["state"], model_attempts=r["attempts"], ledger=r["call_state"],
                   queue_s=float(r["queue_s"]) if r["queue_s"] is not None else None)
        expected_call = None if cancel else "UNKNOWN" if lost else "SUCCEEDED"
        if (r["state"] != state or r["attempts"] != int(not cancel)
                or r["claims"] != int(not cancel) or r["generation"] != int(not cancel)
                or r["wake_receipts"] != int(job["wait"]) or r["valid_result"] != (not cancel and not lost)
                or adapter.calls[key] != int(not cancel) or r["call_state"] != expected_call):
            errors.append(index)
    successes = states["ACCEPTED"] + states["RESULT_STORED"]
    records = sorted(jobs.values(), key=lambda j: j["index"])
    elapsed = time.monotonic() - started
    return {
        "population": population, "distinct_principals": len(owners), "initial_states_per_book": initial,
        "fixture": "two books, two production admission slots, in-process model adapter, PostgreSQL 17",
        "postgres_pool_limit": 8, "postgres_checkout_peak": checkout_peak, "execution_futures_peak": peak_pending,
        "model_inflight_peak": adapter.peak, "model_fixture_calls": sum(adapter.calls.values()),
        "commercial_calls": 0, "transport_requests": 0, "awakened": awakened, "states": dict(states),
        "driver_and_service_cpu_s": time.process_time() - cpu_start,
        "driver_and_service_peak_rss_kib_linux": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "successful_results": successes, "drain_s": duration, "total_s": elapsed,
        "successful_results_per_drain_s": successes / duration, "successful_results_per_total_s": successes / elapsed,
        "provision_latency": percentiles([r["create_s"] for r in records]),
        "creation_to_claim": percentiles([r["queue_s"] for r in records if r.get("queue_s") is not None]),
        "admission_to_result": percentiles([r["service_s"] for r in records if "service_s" in r]),
        "completion_latency_from_enrollment": percentiles([r["ended_s"] - r["enrolled_s"] for r in records if "ended_s" in r]),
        "incorrect_indices": errors, "remaining_slots": slots, "head_unchanged": head_unchanged,
        "gate": "PASS" if not errors and slots == 0 and head_unchanged and len(owners) == population
                    and awakened == sum(r["wait"] for r in records) and completed == expected else "FAILED",
        "records": records,
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--agents", required=True, type=int, choices=[10, 50, 100, 200, 500, 1000, 2000])
    p.add_argument("--output", required=True, type=Path)
    p.add_argument("--dispatch-profile", type=Path,
                   help="Run the frozen and current PG selector on the prepared queue instead of draining it")
    args = p.parse_args()
    root = Path(__file__).resolve().parents[1]
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    with tempfile.TemporaryDirectory() as directory:
        with isolated_kernel(os.environ["NF_TEST_DATABASE_URL"], Path(directory)) as (kernel, _):
            result = trial(kernel, args.agents, dispatch_profile=args.dispatch_profile)
    result.update(source_commit=commit, benchmark_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                  python=platform.python_version(), platform=platform.platform())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "records"}), flush=True)
    return int(result["gate"] not in {"PASS", "PROFILED"})


if __name__ == "__main__":
    raise SystemExit(main())
