"""Real PG and two different local HTTP fixtures, no paid requests."""
from threading import Barrier, Event
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import text

from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.model_routing import activate, preview
from novel_factory.application.supervisor import RuntimeSupervisor
from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import load_fixture
from test_p5_supervisor import _job, backend

pytestmark = [pytest.mark.postgres, pytest.mark.integration, pytest.mark.concurrency]


def policy(world, profile="one", revision=1):
    wr = world["world_ref"]
    return {"work_id": wr["work_id"], "branch_id": wr["branch_id"], "org_id": "shared",
            "installation_id": "local", "revision": revision,
            "rules": [{"id": "default", "when": {}, "profile": profile}]}


def test_project_authority_freeze_and_unknown_fence(kernel_env):
    kernel = kernel_env["kernel"]
    a, b = load_fixture(kernel), load_fixture(kernel)
    wr, owner = a["world_ref"], a["principal"]
    with backend() as (one, sent_one, service), backend() as (two, sent_two, _):
        adapters = {"one": one, "two": two}
        gateway = AgentGateway(kernel, adapters)
        sup = RuntimeSupervisor(kernel, gateway)
        budgets = [kernel.tasks.create_budget(w["world_ref"]["work_id"], "2") for w in (a, b)]
        first = policy(a)
        activate(kernel, owner, wr, first, adapters, expected_revision=0)
        with pytest.raises(DomainError):
            activate(kernel, b["principal"], wr, first, adapters, expected_revision=0)
        with pytest.raises(DomainError):
            preview(kernel, b["principal"], wr, {}, adapters)
        old = _job(kernel, gateway, sup, a, budgets[0], "READY", adapter_id="@policy")
        with pytest.raises(DomainError):
            _job(kernel, gateway, sup, a, budgets[1], "READY", adapter_id="@policy")
        service.lose_response = True
        args = (owner, wr["work_id"], wr["branch_id"])
        sup.run_once(*args)
        updated = policy(a, "two", 2)
        activate(kernel, owner, wr, updated, adapters, expected_revision=1)
        assert preview(kernel, owner, wr, {}, adapters)["matched_rule"] == "default"
        with kernel.db.uow(write=False) as repo:
            rows = repo.many("call_attempts", {"task_id": old.task["task_id"]})
            assert len(rows) == 1 and rows[0]["state"] == "UNKNOWN"
            assert kernel.artifact(repo, old.agent_input["input_ref"], wr["work_id"])["payload"]["adapter_id"] == "one"
        new = _job(kernel, gateway, sup, a, budgets[0], "READY", adapter_id="@policy")
        assert sup.run_once(*args)["task_id"] == new.task["task_id"]
        assert sup.run_once(*args)["status"] == "IDLE"
        assert len(sent_one) == len(sent_two) == 1
        for packet in sent_one + sent_two:
            assert "policy_sha256" not in str(packet["messages"]) and "routing_decision" not in str(packet["messages"])


def test_unsent_reservation_rebinds_generation_without_recharging(kernel_env):
    from novel_factory.application.model_routing import rebind
    kernel = kernel_env["kernel"]
    world = load_fixture(kernel)
    owner, wr = world["principal"], world["world_ref"]
    with backend() as (one, old_calls, _), backend() as (two, new_calls, _):
        adapters = {"one": one, "two": two}
        gateway = AgentGateway(kernel, adapters)
        sup = RuntimeSupervisor(kernel, gateway)
        activate(kernel, owner, wr, policy(world), adapters, expected_revision=0)
        budget = kernel.tasks.create_budget(wr["work_id"], "4")
        job = _job(kernel, gateway, sup, world, budget, "READY", adapter_id="@policy")
        task = job.task["task_id"]
        # Crash at the real reserve->send boundary, while the old lease is still valid.
        reached = []
        def crash(principal, binding, attempt, worker, authorize):
            reached.append(attempt)
            raise RuntimeError("injected before send")
        gateway._mark_sent = crash
        args = (owner, wr["work_id"], wr["branch_id"])
        with pytest.raises(RuntimeError, match="before send"):
            sup.run_once(*args)
        del gateway._mark_sent
        activate(kernel, owner, wr, policy(world, "two", 2), adapters, expected_revision=1)
        rebound = rebind(kernel, owner, wr, task, 1, adapters, "rebind")
        assert rebind(kernel, owner, wr, task, 1, adapters, "rebind") == rebound
        assert rebound["generation"] == 2 and rebound["released_unsent"] == 1
        assert sup.run_once(*args)["status"] == "RESULT_STORED"
        assert not old_calls and len(new_calls) == 1
        with kernel.db.uow(write=False) as repo:
            old = repo.one("call_attempts", {"attempt_id": reached[0]})
            assert old["state"] == "FAILED" and old["actual_amount"] == old["actual_tokens"] == 0
            assert repo.one("task_inputs", {"task_id": task, "generation": 0})["spec"]["input_artifacts"] != (
                repo.one("task_inputs", {"task_id": task, "generation": 2})["spec"]["input_artifacts"])


def test_one_scene_dispatches_two_private_actor_inputs_concurrently(kernel_env):
    from novel_factory.application.planning import PlanningService
    from novel_factory.application.scene_runtime import register_scene
    from novel_factory.fixtures import point
    from test_p5_autonomous_banquet import _candidate
    from test_planning import make_plan
    from test_simulation import process_definition, register

    kernel = kernel_env["kernel"]
    world = load_fixture(kernel, "white_bridge")
    ids = world["ids"]
    scopes = [ids["warehouse"], ids["south"]]
    process = process_definition(kernel, world, name="private-actors", scopes=scopes)
    actors = process["required_actor_ids"][:2]
    process["required_actor_ids"] = actors
    world, _ = register(kernel, world, process)
    owner, wr = world["principal"], world["world_ref"]
    planning = PlanningService(kernel)
    plan, _ = make_plan(planning, world)
    ref = planning.save_plan(owner, plan, "adoptable")
    planning.adopt_plan(owner, wr, ref, None, "adopt")
    account = kernel.tasks.create_budget(wr["work_id"], "5")
    with backend() as (one, first, first_server), backend() as (two, second, second_server):
        adapters = {"one": one, "two": two}
        gateway = AgentGateway(kernel, adapters)
        selected = policy(world)
        selected["rules"].append({"id": "actor-override", "when": {"instance_id": actors[1]}, "profile": "two"})
        activate(kernel, owner, wr, selected, adapters, expected_revision=0)
        registration = register_scene(kernel, gateway, owner, world_ref=wr, story_time=point(30), scope_ids=scopes,
            volume_id=plan["volume_id"], scene_limit=2, participants=[{"actor_id": a} for a in actors],
            subject_id=actors[0], destination_id=ids["south"], budget_id=account, adapter_id="@policy",
            reserve_amount="1", token_reservation=100000, idempotency_key="two-profiles")
        sup = RuntimeSupervisor(kernel, gateway)
        args = (owner, wr["work_id"], wr["branch_id"])
        gate = Barrier(2)
        def answer(packet):
            gate.wait(10)
            return _candidate(packet, dict.fromkeys(actors, "独立核对证据。"), scopes)
        first_server.packet_candidate = second_server.packet_candidate = answer
        with ThreadPoolExecutor(2) as pool:
            results = [sup.admit_model(*args, executor=pool) for _ in range(2)]
            assert all(r.result()["status"] == "RESULT_STORED" for r in results)
        import json
        for actor, requests in zip(actors, (first, second), strict=True):
            assert len(requests) == 1
            packet = json.loads(requests[0]["messages"][1]["content"])
            assert packet["context"]["actor_id"] == actor
            assert packet["context"]["task_id"] == registration["actor_tasks"][actor]
            assert "actor-override" not in str(packet) and "policy_sha256" not in str(packet)
