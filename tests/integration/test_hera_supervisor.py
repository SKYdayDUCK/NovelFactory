"""Authoritative resource budgets and control completion while a real provider connection is busy."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import monotonic, sleep

import pytest
from sqlalchemy import text

from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.derived_worker import DerivedWorldWorker
from novel_factory.application.supervisor import RuntimeSupervisor
from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import load_fixture
from novel_factory.orchestration.processors import TaskDispatcher
from scripts.run_supervisor import serve
from test_p5_supervisor import _job, backend

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def test_model_and_compute_limits_share_global_leases(kernel_env):
    kernel = kernel_env["kernel"]
    book = load_fixture(kernel)
    ref = book["world_ref"]
    scope = book["principal"], ref["work_id"], ref["branch_id"]
    with backend() as (adapter, requests, server), ThreadPoolExecutor(2) as pool:
        server.started, server.release = Event(), Event()
        gateway = AgentGateway(kernel, {"test": adapter})
        supervisor = RuntimeSupervisor(kernel, gateway)
        dispatcher = TaskDispatcher(supervisor, *scope)
        dispatcher.switch("redis")
        budget = kernel.tasks.create_budget(ref["work_id"], "2")
        for _ in range(2):
            _job(kernel, gateway, supervisor, book, budget, "READY")
        with kernel.db.uow() as repo:
            repo.conn.execute(text("INSERT INTO nf_dispatch_limits VALUES "
                                   "('RESOURCE','MODEL',1),('RESOURCE','CPU_COMPUTE',0)"))
            repo.outbox("world.accepted", "revision", ref["world_revision_id"], {"world_ref": ref},
                        "hera:index:" + ref["world_revision_id"])
        first = dispatcher.run_once("model", executor=pool, reconcile=True)
        try:
            assert server.started.wait(8)
            assert dispatcher.run_once("model", executor=pool, reconcile=True)["status"] == "IDLE"
            assert dispatcher.run_once("compute", reconcile=True)["status"] == "IDLE"
            legacy = DerivedWorldWorker(kernel)
            assert legacy.run_once("standalone") is None
            with pytest.raises(DomainError, match="Invalid outbox lease"):
                legacy.run_once("")
            with kernel.db.uow() as repo:
                repo.conn.execute(text("UPDATE nf_dispatch_limits SET slots=1 WHERE subject='CPU_COMPUTE'"))
            lease = legacy.reserve_index(*scope)
            assert lease and legacy.run_once("standalone") is None
            assert legacy.process_reserved(*scope, lease)["consumed"]
            # The legacy worker still acknowledges its durable delivery after the
            # scoped worker built the exact same index. No duplicate formal effect.
            assert legacy.run_once("standalone")["consumed"] is False
            assert not first.done() and len(requests) == 1
        finally:
            server.release.set()
        assert first.result(timeout=10)["status"] == "RESULT_STORED"
        assert dispatcher.run_once("model", reconcile=True)["status"] == "RESULT_STORED"
        assert len(requests) == 2
        with kernel.db.uow() as repo:
            repo.conn.execute(text("DELETE FROM nf_dispatch_limits WHERE scope='RESOURCE'"))


def test_control_retires_deadline_while_model_slot_is_busy(kernel_env):
    kernel = kernel_env["kernel"]
    book = load_fixture(kernel)
    ref = book["world_ref"]
    scope = book["principal"], ref["work_id"], ref["branch_id"]
    with backend() as (adapter, requests, server), ThreadPoolExecutor(1) as driver:
        server.started, server.release, stop = Event(), Event(), Event()
        gateway = AgentGateway(kernel, {"test": adapter})
        supervisor = RuntimeSupervisor(kernel, gateway)
        budget = kernel.tasks.create_budget(ref["work_id"], "1")
        running = _job(kernel, gateway, supervisor, book, budget, "READY").task["task_id"]
        daemon = driver.submit(serve, supervisor, *scope, workers=1, interval=.5,
                               control_while_busy=True, stop_event=stop)
        try:
            assert server.started.wait(8)
            # The long call owns no DB connection during this period. A second
            # task's expiration can commit while its model cannot be admitted.
            expiring = _job(kernel, gateway, supervisor, book, budget, "BLOCKED").task["task_id"]
            with kernel.db.uow() as repo:
                repo.conn.execute(text("UPDATE nf_tasks SET state='READY',deadline=now() "
                                       "WHERE task_id=:task"), {"task": expiring})
                kernel.tasks._emit(repo.conn, expiring, 0, "READY")
            deadline = monotonic() + 6
            while monotonic() < deadline and kernel.tasks.get(expiring)["state"] != "FAILED":
                if daemon.done():
                    daemon.result()
                sleep(.05)
            assert kernel.tasks.get(expiring)["state"] == "FAILED"
            assert kernel.tasks.get(running)["state"] == "RUNNING"
            assert len(requests) == 1 and not daemon.done()
        finally:
            stop.set()
            server.release.set()
        daemon.result(timeout=10)
