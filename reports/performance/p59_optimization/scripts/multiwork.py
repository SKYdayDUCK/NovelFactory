"""Exercise the normal NF supervisor over loopback fixture providers on isolated PG17."""
import argparse
import json
import os
import resource
import subprocess
import sys
import tempfile
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.kernel import Kernel
from novel_factory.application.model_routing import activate
from novel_factory.application.supervisor import RuntimeSupervisor
from novel_factory.fixtures import load_fixture
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.objects import ObjectStore

sys.path.insert(0, str(Path.cwd() / "tests/integration"))
from test_p5_supervisor import _job, backend
from test_p58_routing import policy


def timed_call(action, *args):
    start = time.perf_counter_ns()
    result = action(*args)
    return result, (time.perf_counter_ns() - start) / 1e6


def run_case(url, count, scenario, directory):
    db = Database(url, pool_size=8)
    k = Kernel(db, ObjectStore(directory))
    observations = {"projects": count, "scenario": scenario, "jobs_per_project": 2,
                    "enroll_fixture_ms": [], "rounds": [], "by_work": [], "provider_sends": []}
    try:
        with backend(concurrent=True) as (a, sent_a, server_a), backend(concurrent=True) as (b, sent_b, _):
            a.provider_key = a.account_key = "fixture-a"
            b.provider_key = b.account_key = "fixture-b"
            server_a.lose_response = scenario == "outage"
            g = AgentGateway(k, {"one": a, "two": b})
            s = RuntimeSupervisor(k, g)
            works = []
            for index in range(count):
                w = load_fixture(k)
                wr = w["world_ref"]
                routes = ["one" if scenario == "fixed" or (index+offset) % 2 == 0 else "two" for offset in range(2)]
                activate(k, w["principal"], wr, policy(w, profile=routes[0]), g.adapters, expected_revision=0)
                budget = k.tasks.create_budget(wr["work_id"], "2")
                jobs = []
                for ordinal in range(2):
                    if ordinal and routes[0] != routes[1]:
                        activate(k, w["principal"], wr, policy(w, profile=routes[1], revision=2), g.adapters, expected_revision=1)
                    job, duration = timed_call(lambda: _job(k, g, s, w, budget, "READY", adapter_id="@policy"))
                    jobs.append(job.task["task_id"])
                    observations["enroll_fixture_ms"].append(duration)
                works.append((w, jobs, routes))
            with db.uow() as repo:
                repo.conn.execute(text("""INSERT INTO nf_runtime_limits VALUES
                    ('account','fixture-a',1,NULL,NULL), ('account','fixture-b',1,NULL,NULL)"""))
            def tick(index):
                w = works[index][0]
                wr = w["world_ref"]
                result, ms = timed_call(s.run_once, w["principal"], wr["work_id"], wr["branch_id"])
                return {"work_index": index, "status": result["status"], "reason": result.get("reason"), "ms": ms}
            before = time.perf_counter()
            cpu = time.process_time()
            with ThreadPoolExecutor(max_workers=count) as pool:
                for _ in range(16):
                    steps = list(pool.map(tick, range(count)))
                    observations["rounds"].append(steps)
                    if len(sent_a) + len(sent_b) >= 2*count:
                        break
            duration = time.perf_counter()-before
            observations["elapsed_seconds"] = duration
            observations["worker_process_cpu_seconds"] = time.process_time()-cpu
            observations["process_peak_rss_kib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            with db.uow(write=False) as repo:
                for index, (_, tasks, routes) in enumerate(works):
                    calls = repo.many("call_attempts", {"task_id": tasks})
                    states = dict(Counter(c["state"] for c in calls))
                    observations["by_work"].append({"work_index":index, "profiles_by_task": routes, "states":states,
                        "task_states":[repo.one("tasks", {"task_id": task})["state"] for task in tasks]})
                observations["connections_end"] = repo.conn.scalar(text(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database()"))
            observations["provider_sends"] = [len(sent_a), len(sent_b)]
            observations["successful_business_tasks"] = sum(w["task_states"].count("RESULT_STORED") for w in observations["by_work"])
            observations["successful_business_tasks_per_second"] = observations["successful_business_tasks"]/duration
            observations["duplicate_sends"] = len(sent_a) + len(sent_b) - len({json.loads(p["messages"][1]["content"])["context"]["task_id"] for p in sent_a + sent_b})
            assert observations["duplicate_sends"] == 0
            assert len(sent_a) + len(sent_b) == 2*count
            target = count if scenario == "outage" else 2*count
            assert target == observations["successful_business_tasks"], observations["by_work"]
    finally:
        db.close()
    return observations


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    admin_url = make_url(os.environ["NF_DATABASE_URL"])
    admin = create_engine(admin_url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    results = {"sha":subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip(),
               "kind":"postgresql_17_supervisor_synthetic_loopback_http", "real_provider_calls":0,
               "protocol":"Each scenario clones the empty, migrated disposable template. Two tasks per book. One slot/account. No Redis. All roles summarize fixed fixture inputs.",
               "runs":[]}
    with tempfile.TemporaryDirectory() as directory:
        for count in (1,2,4):
            for scenario in ("fixed","mixed","outage"):
                name = f"nf_fixture_{scenario}_{count}"
                with admin.connect() as conn:
                    conn.exec_driver_sql(f'CREATE DATABASE "{name}" TEMPLATE nf_test')
                url = admin_url.set(database=name).render_as_string(hide_password=False)
                results["runs"].append(run_case(url, count, scenario, Path(directory)/name))
                args.output.write_text(json.dumps(results, indent=2)+"\n")
    admin.dispose()


if __name__ == "__main__":
    main()
