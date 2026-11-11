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
