"""Real PG notifications, process-wide slots, weighted scope limits and FIFO eligibility."""
from threading import Barrier, Event
from uuid import uuid4

import pytest
from sqlalchemy import text

from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.supervisor import RuntimeSupervisor
from novel_factory.fixtures import load_fixture
from novel_factory.infrastructure.supervisor_wake import wake_listener, wait_for_change
from test_p5_supervisor import _child, _job, backend

pytestmark = [pytest.mark.postgres, pytest.mark.integration, pytest.mark.concurrency]


def test_committed_hint_only_and_duplicate_reconciliation(kernel_env):
    kernel = kernel_env["kernel"]
    work = load_fixture(kernel)
    wr = work["world_ref"]
    with backend() as (route, requests, _), wake_listener(kernel.db.engine) as listener:
        gateway = AgentGateway(kernel, {"test": route})
        sup = RuntimeSupervisor(kernel, gateway)
        args = (work["principal"], wr["work_id"], wr["branch_id"])
        assert sup.run_once(*args)["status"] == "IDLE"
        with pytest.raises(RuntimeError, match="abort"):
            with kernel.db.uow() as repo:
                repo.outbox("task.changed", "task", str(uuid4()), {}, "aborted-notification")
                assert not wait_for_change(listener, .05)
                raise RuntimeError("abort")
        assert not wait_for_change(listener, .05)
        with kernel.db.uow(write=False) as repo:
            assert repo.one("outbox", {"dedupe_key": "aborted-notification"}) is None
            assert repo.conn.scalar(text("""
                SELECT count(*) FROM pg_stat_activity
                WHERE datname=current_database() AND application_name='nf_supervisor_listener'
                  AND state='idle' AND xact_start IS NULL
            """)) == 1
        job = _job(kernel, gateway, sup, work, kernel.tasks.create_budget(wr["work_id"], "2"), "READY")
        assert wait_for_change(listener, 2)
        assert sup.run_once(*args)["task_id"] == job.task["task_id"]
        assert wait_for_change(listener, 2)
        for _ in range(2):
            with kernel.db.uow() as repo:
                repo.conn.exec_driver_sql("NOTIFY nf_supervisor_wake")
            assert wait_for_change(listener, 2)
            assert sup.run_once(*args)["status"] == "IDLE"
        assert len(requests) == 1


def test_two_process_slots_fence_third_until_completion(kernel_env):
    kernel = kernel_env["kernel"]
    work = load_fixture(kernel)
    wr = work["world_ref"]
    with backend(concurrent=True) as (route, requests, server):
        gateway = AgentGateway(kernel, {"test": route})
        sup = RuntimeSupervisor(kernel, gateway)
        budget = kernel.tasks.create_budget(wr["work_id"], "2")
        jobs = [_job(kernel, gateway, sup, work, budget, "READY") for _ in range(3)]
        entered, release = Barrier(3), Event()

        def hold(packet):
            entered.wait(20)
            assert release.wait(30)
            return {"text": "Synthetic summary", "event_refs": [], "important_deltas": [], "evidence_refs": []}

        server.packet_candidate = hold
        children = [_child(kernel_env, work, route) for _ in range(2)]
        try:
            entered.wait(25)
            assert sup.run_once(work["principal"], wr["work_id"], wr["branch_id"])["status"] == "IDLE"
            with kernel.db.uow(write=False) as repo:
                slots = repo.many("dispatch_slots")
                assert len(slots) == len({s["task_id"] for s in slots}) == 2
                assert len(requests) == 2
                assert repo.conn.scalar(text("SELECT count(*) FROM nf_call_attempts WHERE state='SENT'")) == 2
            release.set()
            for p in children:
                assert p.wait(timeout=10) == 0, p.stderr.read()
        finally:
            release.set()
            for p in children:
                if p.poll() is None:
                    p.kill()
                    p.wait(timeout=5)
        del server.packet_candidate
        assert sup.run_once(work["principal"], wr["work_id"], wr["branch_id"])["status"] == "RESULT_STORED"
        with kernel.db.uow(write=False) as repo:
            assert repo.many("dispatch_slots") == []
            ids = [job.task["task_id"] for job in jobs]
            calls = repo.many("call_attempts", {"task_id": ids})
            assert len(calls) == len({r["task_id"] for r in calls}) == len(ids) == len(requests)
            assert kernel.head(repo, wr["work_id"], wr["branch_id"]) == wr


def test_oldest_eligible_project_first_with_work_and_role_overrides(kernel_env):
    kernel = kernel_env["kernel"]
    first, second = load_fixture(kernel), load_fixture(kernel)
    with backend() as (route, requests, _):
        gw = AgentGateway(kernel, {"test": route})
        sup = RuntimeSupervisor(kernel, gw)
        for w in (first, second):
            budget = kernel.tasks.create_budget(w["world_ref"]["work_id"], "2")
            for _ in range(2):
                _job(kernel, gw, sup, w, budget, "READY")

        def run(w):
            return sup.run_once(w["principal"], w["world_ref"]["work_id"], w["world_ref"]["branch_id"])["status"]

        with kernel.db.uow() as repo:
            repo.add("dispatch_limits", {"scope": "ROLE", "subject": "evidence_maintainer", "slots": 0})
        try:
            assert run(first) == run(second) == "IDLE" and not requests
            with kernel.db.uow() as repo:
                repo.change("dispatch_limits", {"scope": "ROLE", "subject": "evidence_maintainer"}, {"slots": 1})
            # Later arrivals cannot race ahead of a registered eligible queue.
            assert run(second) == "IDLE"
            assert run(first) == "RESULT_STORED"
            assert run(first) == "IDLE"
            assert run(second) == "RESULT_STORED"
            with kernel.db.uow() as repo:
                repo.add("dispatch_limits", {"scope": "WORK", "subject": first["world_ref"]["work_id"], "slots": 0})
            assert run(first) == "IDLE"
            assert run(second) == "RESULT_STORED"
        finally:
            with kernel.db.uow() as repo:
                repo.conn.exec_driver_sql("DELETE FROM nf_dispatch_limits WHERE subject <> '*'")
        assert run(first) == "RESULT_STORED"
        assert len(requests) == 4


def test_idle_service_wakes_before_periodic_reconciliation(kernel_env, tmp_path):
    import json
    import os
    from pathlib import Path
    import subprocess
    import sys
    import time

    from test_p5_process_resume import ROOT, _https
    from novel_factory.infrastructure.model_gateway import OpenAICompatibleAdapter

    kernel = kernel_env["kernel"]
    world = load_fixture(kernel)
    wr = world["world_ref"]
    budget = kernel.tasks.create_budget(wr["work_id"], "2")
    access = tmp_path / "supervisor.json"
    access.write_text(json.dumps({"token": world["token"], "work_id": wr["work_id"], "branch_id": wr["branch_id"]}))
    access.chmod(0o600)
    label = "nf-idle-" + str(uuid4())

    def activity():
        with kernel.db.uow(write=False) as repo:
            return repo.conn.execute(text("""
                SELECT pid, state, query_start, xact_start FROM pg_stat_activity
                WHERE application_name=:label ORDER BY pid
            """), {"label": label}).all()

    with backend() as (_, requests, server):
        cert = _https(server, tmp_path)
        route = OpenAICompatibleAdapter(endpoint=f"https://127.0.0.1:{server.server_port}/v1/chat/completions",
            model="deterministic-idle-fixture", api_key="fixture", timeout_seconds=60.0)
        gateway = AgentGateway(kernel, {"external": route})
        sup = RuntimeSupervisor(kernel, gateway)
        env = {**os.environ, "NF_DATABASE_URL": kernel_env["url"], "PGAPPNAME": label,
               "NF_OBJECT_ROOT": str(kernel_env["store"].root), "NF_MODEL_API_KEY": "fixture",
               "NF_MODEL_NAME": route.model, "NF_MODEL_ENDPOINT": route.endpoint,
               "NF_MODEL_TIMEOUT_SECONDS": "60", "NF_MODEL_MAX_OUTPUT_TOKENS": "2048",
               "SSL_CERT_FILE": str(cert), "NO_PROXY": "127.0.0.1"}
        with subprocess.Popen([sys.executable, "scripts/run_supervisor.py", "--access-file", str(access)],
                              cwd=ROOT, env=env) as child:
            try:
                previous = None
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    sample = activity()
                    if sample and all(row.state == "idle" and row.xact_start is None for row in sample):
                        if previous == sample:
                            break
                        previous = sample
                    time.sleep(.1)
                else:
                    pytest.fail("Dispatcher did not reach an idle connection pool")
                idle_seconds = float(os.environ.get("NF_P5_IDLE_GATE_SECONDS", "1.5"))
                assert 1.1 <= idle_seconds <= 55  # test stays within one default 60s fallback
                time.sleep(idle_seconds)
                assert activity() == previous
                assert not requests and child.poll() is None
                with kernel.db.uow(write=False) as repo:
                    assert repo.many("tasks", {"work_id": wr["work_id"]}) == []
                started = time.monotonic()
                job = _job(kernel, gateway, sup, world, budget, "READY", adapter_id="external")
                deadline = started + 12
                while time.monotonic() < deadline:
                    if kernel.tasks.get(job.task["task_id"])["state"] == "RESULT_STORED":
                        break
                    time.sleep(.05)
                else:
                    pytest.fail("Enrolled job did not wake before periodic reconciliation")
                elapsed = time.monotonic() - started
                assert len(requests) == 1
                with kernel.db.uow(write=False) as repo:
                    assert repo.conn.scalar(text("""
                        SELECT pg_terminate_backend(pid) FROM pg_stat_activity
                        WHERE datname=current_database() AND application_name='nf_supervisor_listener'
                    """))
                disconnected_at = time.monotonic()
                second = _job(kernel, gateway, sup, world, budget, "READY", adapter_id="external")
                while time.monotonic() - disconnected_at < 14:
                    if kernel.tasks.get(second.task["task_id"])["state"] == "RESULT_STORED":
                        break
                    time.sleep(.05)
                else:
                    pytest.fail("Service did not recover its listener and dispatch the committed job")
                assert len(requests) == 2 and child.poll() is None
                if output := os.environ.get("NF_P5_IDLE_EVIDENCE"):
                    Path(output).write_text(json.dumps({"provider": "deterministic HTTPS loopback",
                        "idle_seconds": idle_seconds, "idle_requests": 0, "idle_tasks_created": 0,
                        "pool_sql_during_observation": 0, "idle_open_transactions": 0,
                        "fallback_seconds": 60, "enroll_start_to_result_s": elapsed,
                        "listener_terminated_to_second_result_s": time.monotonic() - disconnected_at,
                        "total_provider_requests": len(requests)}, indent=2) + "\n")
            finally:
                child.terminate()
                child.wait(timeout=10)


def test_unavailable_first_task_does_not_hide_ready_sibling(kernel_env):
    kernel = kernel_env["kernel"]
    world = load_fixture(kernel)
    wr = world["world_ref"]
    with backend() as (route, requests, _):
        gateway = AgentGateway(kernel, {"test": route})
        supervisor = RuntimeSupervisor(kernel, gateway)
        budget = kernel.tasks.create_budget(wr["work_id"], "1")
        first, second = [_job(kernel, gateway, supervisor, world, budget, "READY") for _ in range(2)]
        with kernel.db.uow() as locker:
            locker.one("tasks", {"task_id": first.task["task_id"]}, lock="update")
            result = supervisor.run_once(world["principal"], wr["work_id"], wr["branch_id"])
            assert result["status"] == "RESULT_STORED" and result["task_id"] == second.task["task_id"]
        assert len(requests) == 1
