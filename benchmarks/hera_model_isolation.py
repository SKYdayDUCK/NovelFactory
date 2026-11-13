"""Compare mixed ingress status reads during two live, enrolled simulated model executions."""
import argparse
import asyncio
import gzip
import json
import logging
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Lock

from benchmarks.hera_mixed import population, trial
from benchmarks.p2_behavioral_load import hardware, isolated_kernel
from benchmarks.p59_workflow_load import FixtureAdapter
from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.kernel import Kernel
from novel_factory.application.runtime_tasks import RuntimeTaskService
from novel_factory.application.supervisor import RuntimeSupervisor
from novel_factory.fixtures import load_fixture, point
from novel_factory.infrastructure.database import Database


class WaitingFixture(FixtureAdapter):
    def __init__(self):
        super().__init__()
        self.arrived, self.release = Event(), Event()
        self.entered, self.lock_entered = 0, Lock()

    def generate(self, request):
        with self.lock_entered:
            self.entered += 1
            if self.entered == 2:
                self.arrived.set()
        if not self.release.wait(90):
            raise RuntimeError("Timed out waiting for measurement")
        return super().generate(request)


def measure(kernel, actors, loaded):
    if not loaded:
        return {"loaded": False, "read": asyncio.run(trial(kernel, actors, 100, 50, True, "read"))}
    book = load_fixture(kernel)
    owner, wr = book["principal"], book["world_ref"]
    scope = owner, wr["work_id"], wr["branch_id"]
    route = WaitingFixture()
    gateway = AgentGateway(kernel, {"fixture": route})
    supervisor = RuntimeSupervisor(kernel, gateway)
    runtime = RuntimeTaskService(kernel, gateway)
    budget = kernel.tasks.create_budget(wr["work_id"], "2")
    ids = []
    for n in range(4):
        job = runtime.prepare(owner, wr, role_id="evidence_maintainer", task_kind="summary",
            story_time=point(1), subject_ids=[book["ids"]["sword"]], predicates=["owner"],
            assignment="Summarize visible evidence.", adapter_id="fixture", queue_state="READY")
        key = job.task["task_id"]
        ids.append(key)
        route.jobs[key] = n
        supervisor.enroll(owner, key, manifest_ref=job.manifest_ref, knowledge_cut={"plane": "WORLD"},
            context_tokens=20000, input_ref=job.agent_input["input_ref"], budget_id=budget,
            reserve_amount="0.1", token_reservation=100000)
    with ThreadPoolExecutor(2) as executor:
        futures = [supervisor.admit_model(*scope, executor=executor) for _ in range(2)]
        try:
            assert route.arrived.wait(20)
            occupied = kernel.db.engine.pool.checkedout()
            assert occupied == 0
            with kernel.db.uow(write=False) as repo:
                states = [repo.one("tasks", {"task_id": key})["state"] for key in ids]
                sent = repo.many("call_attempts", {"task_id": ids, "state": "SENT"})
            assert states == ["RUNNING", "RUNNING", "READY", "READY"] and len(sent) == 2
            data = asyncio.run(trial(kernel, actors, 100, 50, True, "read"))
        finally:
            route.release.set()
        for f in futures:
            assert f.result()["status"] == "RESULT_STORED"
    for _ in range(2):
        assert supervisor.admit_model(*scope)["status"] == "RESULT_STORED"
    with kernel.db.uow(write=False) as repo:
        final = [repo.one("tasks", {"task_id": key})["state"] for key in ids]
        attempts = repo.many("call_attempts", {"task_id": ids})
    assert final == ["RESULT_STORED"] * 4 and len(attempts) == 4 and set(route.calls.values()) == {1}
    return {"loaded": True, "checked_out_in_model": occupied, "tasks_before": states,
            "model_calls_sent_during_reads": len(sent), "tasks_after": list(zip(ids, final)), "attempts": len(attempts),
            "read": data}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", required=True, type=Path)
    args = p.parse_args()
    logging.getLogger("mcp").setLevel(logging.WARNING)
    with TemporaryDirectory() as tmp, isolated_kernel(os.environ["NF_TEST_DATABASE_URL"], Path(tmp)) as (setup,url):
        setup.db.close()
        db = Database(url, pool_size=4)
        try:
            kernel = Kernel(db, setup.objects)
            actors = population(kernel, 10)
            runs = [measure(kernel, actors, loaded) for loaded in (False,True,True,False)]
            data = {"environment": hardware(), "source_sha": subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], text=True).strip(),
                    "db_limit":4,"model_limit":2,"provider":"in-process fixture, zero external calls","runs":runs}
            with gzip.open(args.output, "wt", encoding="utf8") as out:
                json.dump(data,out,separators=(",",":"))
            assert all(run["read"]["verified_responses"] == 100 for run in runs)
            for run in runs:
                print(run["loaded"], run["read"]["goodput_s"], run["read"]["latency"])
        finally:
            db.close()


if __name__ == "__main__":
    main()
