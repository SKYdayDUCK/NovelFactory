import pytest

from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.supervisor import RuntimeSupervisor
from novel_factory.fixtures import load_fixture
from novel_factory.infrastructure.capacity import ResourceMonitor
from novel_factory.orchestration.processors import TaskDispatcher

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def test_capacity_sampling_real_sql(kernel_env):
    kernel = kernel_env["kernel"]
    world = load_fixture(kernel)
    wr = world["world_ref"]
    dispatcher = TaskDispatcher(RuntimeSupervisor(kernel, AgentGateway(kernel, {})), world["principal"], wr["work_id"], wr["branch_id"])
    dispatcher.switch("redis")
    monitor = ResourceMonitor()
    for group in ("model", "world", "compute", "statistics"):
        sample = monitor.sample(kernel.db, group, wr["work_id"], wr["branch_id"])
        assert sample["available"], sample
        assert sample["pool_wait_ms"] >= 0 and sample["active_connections"] >= 1
        if group == "model":
            assert sample["in_flight"] == 0 and not sample["budget_blocked"]


def test_compute_and_model_share_database_ceiling_and_recover_lost_reservation(kernel_env):
    from sqlalchemy import text
    from test_p5_supervisor import _job, backend

    from novel_factory.application.derived_worker import DerivedWorldWorker
    from novel_factory.domain.errors import DomainError

    kernel = kernel_env["kernel"]
    world = load_fixture(kernel)
    wr = world["world_ref"]
    args = world["principal"], wr["work_id"], wr["branch_id"]
    with backend() as (route, requests, _):
        gateway = AgentGateway(kernel, {"test": route})
        supervisor = RuntimeSupervisor(kernel, gateway)
        dispatcher = TaskDispatcher(supervisor, *args)
        dispatcher.switch("redis")
        _job(kernel, gateway, supervisor, world, kernel.tasks.create_budget(wr["work_id"], "2"), "READY")
        worker = DerivedWorldWorker(kernel)
        with kernel.db.uow() as repo:
            for index in range(3):
                repo.outbox("world.accepted", "revision", wr["world_revision_id"], {"world_ref": wr},
                            f"capacity-fence:{wr['work_id']}:{index}")
        first, second = worker.reserve_index(*args), worker.reserve_index(*args)
        assert first and second and first["outbox_id"] != second["outbox_id"]
        assert worker.reserve_index(*args) is None
        assert dispatcher.run_once("model")["status"] == "IDLE" and not requests
        assert dispatcher.run_once("compute")["status"] == "IDLE"
        with kernel.db.uow() as repo:
            repo.conn.execute(text("UPDATE nf_index_leases SET expires_at=now()-interval '2 seconds' WHERE event_id=:id"),
                              {"id": first["outbox_id"]})
        recovered = worker.reserve_index(*args)
        assert recovered["outbox_id"] == first["outbox_id"] and recovered["token"] != first["token"]
        with pytest.raises(DomainError) as rejected:
            worker.process_reserved(*args, first)
        assert rejected.value.code == "LEASE_EXPIRED"
        worker.process_reserved(*args, recovered)
        worker.process_reserved(*args, second)
        assert dispatcher.run_once("model")["status"] == "RESULT_STORED" and len(requests) == 1
        assert dispatcher.run_once("compute")["status"] == "INDEXED"


def test_global_consumer_backlog_does_not_scale_idle_project(kernel_env):
    from sqlalchemy import text
    from test_p5_supervisor import _job, backend

    from novel_factory.infrastructure.capacity import CapacityPolicy

    kernel = kernel_env["kernel"]
    idle, busy = load_fixture(kernel), load_fixture(kernel)
    idle_wr, busy_wr = idle["world_ref"], busy["world_ref"]
    with backend() as (route, requests, _):
        gw = AgentGateway(kernel, {"test": route})
        sup = RuntimeSupervisor(kernel, gw)
        budget = kernel.tasks.create_budget(busy_wr["work_id"], "2")
        for _ in range(3):
            job = _job(kernel, gw, sup, busy, budget, "READY")
            with kernel.db.uow() as repo:
                # Same source rows and subscriber keys the relay publishes for a real enrollment.
                repo.conn.execute(text("""INSERT INTO nf_bus_subscriptions(event_id,consumer)
                    SELECT outbox_id,'model' FROM nf_outbox WHERE topic='supervisor.enrolled' AND aggregate_id=:task"""),
                    {"task": job.task["task_id"]})
        monitor = ResourceMonitor()
        idle_sample = monitor.sample(kernel.db, "model", idle_wr["work_id"], idle_wr["branch_id"])
        global_sample = monitor.sample(kernel.db, "model")
        busy_sample = monitor.sample(kernel.db, "model", busy_wr["work_id"], busy_wr["branch_id"])
        assert idle_sample["available"] and idle_sample["backlog"] == 0
        assert "queue_backlog" not in idle_sample
        assert global_sample["backlog"] >= 3 and busy_sample["backlog"] == 3
        assert CapacityPolicy(4, "model").decide(idle_sample, 0) == (1, "hold")
        assert not requests
