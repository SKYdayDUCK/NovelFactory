"""A live, two-slot supervisor observes two simultaneous model jobs and then complete idleness."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from threading import Event

import pytest
from test_p5_process_resume import ROOT, _https
from test_p5_supervisor import _job, backend

from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.supervisor import RuntimeSupervisor
from novel_factory.fixtures import load_fixture
from novel_factory.infrastructure.diagnostics import inspect
from novel_factory.infrastructure.model_gateway import OpenAICompatibleAdapter
from novel_factory.infrastructure.private_files import create_private_file

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def test_actual_worker_windows_are_distinct_from_scale_decisions(kernel_env, tmp_path):
    kernel = kernel_env["kernel"]
    world = load_fixture(kernel)
    wr = world["world_ref"]
    access = tmp_path / "scope.json"
    with os.fdopen(create_private_file(access), "w", encoding="utf-8") as target:
        json.dump({"token": world["token"], "work_id": wr["work_id"], "branch_id": wr["branch_id"]}, target)
    assert not inspect(kernel.db, 32, component="model")["worker_utilization_windows"]
    with backend(concurrent=True) as (_, requests, server):
        cert = _https(server, tmp_path)
        route = OpenAICompatibleAdapter(endpoint=f"https://127.0.0.1:{server.server_port}/v1/chat/completions",
            model="deterministic-pool-fixture", api_key="fixture", timeout_seconds=60.0)
        gateway = AgentGateway(kernel, {"external": route})
        supervisor = RuntimeSupervisor(kernel, gateway)
        budget = kernel.tasks.create_budget(wr["work_id"], "2")
        jobs = [_job(kernel, gateway, supervisor, world, budget, "READY", adapter_id="external") for _ in range(2)]
        server.started, server.release = Event(), Event()
        env = {**os.environ, "NF_DATABASE_URL": kernel_env["url"], "NF_OBJECT_ROOT": str(kernel_env["store"].root),
               "NF_MODEL_API_KEY": "fixture", "NF_MODEL_NAME": route.model, "NF_MODEL_ENDPOINT": route.endpoint,
               "NF_MODEL_TIMEOUT_SECONDS": "60", "NF_MODEL_MAX_OUTPUT_TOKENS": "2048",
               "SSL_CERT_FILE": str(cert), "NO_PROXY": "127.0.0.1"}
        with subprocess.Popen([sys.executable, "scripts/run_supervisor.py", "--access-file", str(access),
                               "--workers", "2", "--interval", "4", "--observation-seconds", ".5"],
                              cwd=ROOT, env=env) as child:
            def observe(predicate):
                deadline = time.monotonic() + 12
                while time.monotonic() < deadline:
                    report = inspect(kernel.db, 32, component="model")
                    assert child.poll() is None and not report["scaling_decisions"]
                    for row in report["worker_utilization_windows"]:
                        assert str(row["work_id"]) == wr["work_id"] and row["branch_id"] == wr["branch_id"]
                        if predicate(row):
                            return row
                    time.sleep(.05)
                pytest.fail("Timed out waiting for measured pool occupancy")
            try:
                busy = observe(lambda row: len(requests) == 2 and row["worker_utilization_pct"] > 99.9)
                assert busy["busy_slot_seconds"] > .95
                assert busy["available_slot_seconds"] == pytest.approx(busy["window_seconds"] * 2)
                server.release.set()
                idle = observe(lambda row: row["window_end"] > busy["window_end"] and row["busy_slot_seconds"] == 0)
                assert idle["worker_utilization_pct"] == 0 and idle["scope"] == "project_executor"
                assert all(kernel.tasks.get(job.task["task_id"])["state"] == "RESULT_STORED" for job in jobs)
                if destination := os.getenv("NF_P55_OCCUPANCY_EVIDENCE"):
                    fields = ("scope", "window_seconds", "busy_slot_seconds", "available_slot_seconds", "worker_utilization_pct")
                    Path(destination).write_text(json.dumps({"source":"two actual supervised jobs with HTTPS loopback model",
                        "observations": [{k: record[k] for k in fields} for record in (busy,idle)],
                        "provider_calls": len(requests), "real_inference": False}, indent=2) + "\n")
            finally:
                server.release.set()
                child.terminate()
                child.wait(timeout=10)


def test_spawned_index_worker_records_actual_child_time(kernel_env):
    import multiprocessing
    from concurrent.futures import ProcessPoolExecutor

    from novel_factory.application.derived_worker import DerivedWorldWorker
    from novel_factory.infrastructure.capacity import PoolOccupancy
    from novel_factory.orchestration.processors import (
        TaskDispatcher,
        initialize_index_worker,
        process_reserved_index,
    )

    kernel = kernel_env["kernel"]
    world = load_fixture(kernel)
    wr = world["world_ref"]
    args = world["principal"], wr["work_id"], wr["branch_id"]
    supervisor = RuntimeSupervisor(kernel, AgentGateway(kernel, {}))
    TaskDispatcher(supervisor, *args).switch("redis")
    with kernel.db.uow() as repo:
        repo.outbox("world.accepted", "revision", wr["world_revision_id"], {"world_ref": wr},
                    "occupancy-index:" + wr["world_revision_id"])
    lease = DerivedWorldWorker(kernel).reserve_index(*args)
    assert lease
    meter = PoolOccupancy(2)
    with ProcessPoolExecutor(max_workers=2, mp_context=multiprocessing.get_context("spawn"),
                             initializer=initialize_index_worker,
                             initargs=(kernel_env["url"], str(kernel.objects.root), meter)) as pool:
        deadline = time.monotonic() + 1
        initial = meter.sample()
        while initial is None and time.monotonic() < deadline:
            time.sleep(.005)
            initial = meter.sample()
        assert initial and initial["busy_slot_seconds"] == 0
        assert pool.submit(process_reserved_index, *args, lease).result(timeout=12)["status"] == "INDEXED"
        measured = meter.sample()
    assert 0 < measured["busy_slot_seconds"] <= measured["window_seconds"]
    assert measured["available_slot_seconds"] == pytest.approx(2 * measured["window_seconds"])
