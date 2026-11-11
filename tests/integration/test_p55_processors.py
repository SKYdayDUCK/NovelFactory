from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text

from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.supervisor import RuntimeSupervisor
from novel_factory.fixtures import load_fixture
from novel_factory.orchestration.processors import PROCESSORS, TaskDispatcher, handler_for
from test_p5_supervisor import _job, backend

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def test_transport_switch_redelivery_scoping_and_unknown_registry(kernel_env):
    kernel = kernel_env["kernel"]
    first, second = load_fixture(kernel), load_fixture(kernel)
    a, b = first["world_ref"], second["world_ref"]
    with backend() as (route, requests, _):
        gw = AgentGateway(kernel, {"test": route})
        sup = RuntimeSupervisor(kernel, gw)
        scoped = TaskDispatcher(sup, first["principal"], a["work_id"], a["branch_id"])
        other = TaskDispatcher(sup, second["principal"], b["work_id"], b["branch_id"])
        jobs = [_job(kernel, gw, sup, w, kernel.tasks.create_budget(w["world_ref"]["work_id"], "2"), "READY")
                for w in (first, second)]
        scoped.switch("redis")
        assert sup.run_once(first["principal"], a["work_id"], a["branch_id"])["status"] == "IDLE"
        assert other.run_once("model", reconcile=True)["status"] == "IDLE"
        assert not requests
        with kernel.db.uow() as repo:
            repo.conn.execute(text("DELETE FROM nf_processor_hints WHERE branch_id=:b"), {"b": a["branch_id"]})
            event = SimpleNamespace(work_id=b["work_id"], branch_id=b["branch_id"], event_id=str(uuid4()))
            handler_for("model")(repo.conn, event)
        assert scoped.run_once("model")["status"] == "IDLE"
        with kernel.db.uow() as repo:
            event = SimpleNamespace(work_id=a["work_id"], branch_id=a["branch_id"], event_id=str(uuid4()))
            handler_for("model")(repo.conn, event)
            handler_for("model")(repo.conn, event)
        assert scoped.run_once("model")["task_id"] == jobs[0].task["task_id"]
        assert scoped.run_once("model")["status"] == "IDLE"
        assert scoped.run_once("recovery", reconcile=True)["status"] == "IDLE"
        assert len(requests) == 1
        scoped.switch("postgres")
        assert sup.run_once(second["principal"], b["work_id"], b["branch_id"])["task_id"] == jobs[1].task["task_id"]
        assert len(requests) == 2
    with pytest.raises(ValueError):
        handler_for("unregistered.module")
    assert set(PROCESSORS) == {"model", "world", "compute", "log", "statistics", "recovery"}


def test_compute_only_rebuilds_authorized_scope_without_model_calls(kernel_env):
    kernel = kernel_env["kernel"]
    world = load_fixture(kernel)
    wr = world["world_ref"]
    principal = world["principal"]
    with backend() as (route, requests, _):
        supervisor = RuntimeSupervisor(kernel, AgentGateway(kernel, {"test": route}))
        dispatcher = TaskDispatcher(supervisor, principal, wr["work_id"], wr["branch_id"])
        dispatcher.switch("redis")
        with kernel.db.uow() as repo:
            repo.outbox("world.accepted", "revision", wr["world_revision_id"], {"world_ref": wr},
                        "p55:compute:" + wr["world_revision_id"])
        assert dispatcher.run_once("compute", reconcile=True)["status"] == "INDEXED"
        assert dispatcher.run_once("compute", reconcile=True)["status"] == "IDLE"
        assert not requests
        with kernel.db.uow(write=False) as repo:
            assert kernel.head(repo, wr["work_id"], wr["branch_id"]) == wr
            assert repo.one("index_watermarks", {"branch_id": wr["branch_id"], "index_name": "timeline"})
