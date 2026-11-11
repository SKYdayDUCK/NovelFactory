"""Real HTTP + scoped PG actor delivery; the upstream model text is a fixture."""
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.runtime_tasks import RuntimeTaskService
from novel_factory.application.simulation import WorldSchedulerService
from novel_factory.application.supervisor import RuntimeSupervisor
from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import load_fixture, point
from novel_factory.infrastructure.database import tables
from novel_factory.infrastructure.repository import KernelRepository
from test_p5_supervisor import backend
from test_simulation import process_definition, register, selected

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


@pytest.mark.parametrize("invalid_time", [False, True], ids=["seal-and-recover", "reject-invalid-action"])
def test_supervised_sealed_slots_wake_waiting_actor_and_survive_lost_handoff(kernel_env, monkeypatch, invalid_time):
    kernel = kernel_env["kernel"]
    base = load_fixture(kernel, "white_bridge")
    process = process_definition(kernel, base)
    world, _ = register(kernel, base, process)
    wr, admin = world["world_ref"], world["principal"]
    work, branch = wr["work_id"], wr["branch_id"]
    actors = process["required_actor_ids"]
    choices = dict(zip(actors, ["今晚不赴宴，我留守粮仓。", "只在收到消息后出发。", "核对库单。"], strict=True))
    with backend() as (http, requests, provider):
        gateway = AgentGateway(kernel, {"test": http})
        runtime, supervisor = RuntimeTaskService(kernel, gateway), RuntimeSupervisor(kernel, gateway)
        scheduler = WorldSchedulerService(kernel)
        jobs = [runtime.prepare(admin, wr, role_id="character_actor", task_kind="character_action",
                 story_time=point(30), subject_ids=[actor], predicates=["location"], actor_id=actor,
                 assignment="只依照自己的认知与目标作出行动。", adapter_id="test",
                 queue_state=("BLOCKED" if idx == 1 else "READY")) for idx, actor in enumerate(actors)]
        interaction = scheduler.prepare_interaction(admin, wr, point(30), process["scope_ids"],
                        {actor: job.principal.grant_id for actor, job in zip(actors, jobs)}, "supervised-banquet")
        iid = interaction["interaction_id"]
        budget = kernel.tasks.create_budget(work, "3")
        for actor, job in zip(actors, jobs):
            opts = dict(manifest_ref=job.manifest_ref, input_ref=job.agent_input["input_ref"],
                        knowledge_cut={"plane": "BELIEF", "actor_id": actor, "known_at": point(30)},
                        budget_id=budget, reserve_amount="1", context_tokens=20000, token_reservation=100000)
            with pytest.raises(DomainError, match="exact slot"):
                supervisor.enroll(admin, job.task["task_id"], seal_interaction_id=str(uuid4()), **opts)
            supervisor.enroll(admin, job.task["task_id"], seal_interaction_id=iid, **opts)
        supervisor.workflow.wait(jobs[1].task["task_id"], 0, [jobs[0].task["task_id"]], mode="AND",
            work_id=work, branch_id=branch,
            authorize_in_tx=lambda c: kernel.authorize(KernelRepository(c, tables), admin, work, "ADMIN", branch))

        def choose(ctx):
            return {"schema_version":"2.0", "proposal_id":str(uuid4()),
                    "task_id":ctx["task_id"], "generation":ctx["generation"], "context_bundle_id":ctx["context_bundle_id"],
                    "world_ref":wr, "actor_id":ctx["actor_id"], "intent":choices[ctx["actor_id"]], "targets":[],
                    "story_interval":{"start":point(29 if invalid_time and ctx["actor_id"] == actors[0] else 30),"end":point(31),"bounds":"[)"},
                    "resource_requests":[], "preconditions":[], "evidence_refs":[],
                    "dependency_fingerprint":ctx["dependency_fingerprint"], "idempotency_key":"personal-action"}
        provider.candidate = choose
        first = supervisor.run_once(admin, work, branch)
        assert first["task_id"] == jobs[0].task["task_id"] and first["status"] == "RESULT_STORED"

        if invalid_time:
            assert supervisor.run_once(admin, work, branch)["task_id"] == jobs[2].task["task_id"]
            assert supervisor.run_once(admin, work, branch)["status"] == "IDLE"
            assert kernel.tasks.get(jobs[0].task["task_id"])["state"] == "WAITING_REVIEW"
            assert kernel.tasks.get(jobs[1].task["task_id"])["state"] == "BLOCKED"
            assert not scheduler.interaction_status(jobs[0].principal, iid)["own_input_stored"]
            with kernel.db.uow(write=False) as repo:
                notice = repo.one("outbox", {"dedupe_key": f"handoff:{jobs[0].task['task_id']}:1:blocked"})
                assert notice["payload"]["reason"] == "TASK_REBIND_REQUIRED"
            assert len(requests) == 2
            return

        def crash(*args):
            raise RuntimeError("crash after intent insert before acceptance")
        with monkeypatch.context() as patch:
            patch.setattr(kernel.tasks, "accept_in_transaction", crash)
            with pytest.raises(RuntimeError, match="after intent insert"):
                supervisor.run_once(admin, work, branch)
        assert scheduler.interaction_status(jobs[0].principal, iid)["own_input_stored"] is False
        assert kernel.tasks.get(jobs[1].task["task_id"])["state"] == "BLOCKED"
        assert len(requests) == 1
        supervisor = RuntimeSupervisor(kernel, gateway)
        for _ in range(2):
            assert supervisor.run_once(admin, work, branch)["status"] == "RESULT_STORED"
        assert supervisor.run_once(admin, work, branch)["status"] == "IDLE"
        assert all(kernel.tasks.get(job.task["task_id"])["state"] == "ACCEPTED" for job in jobs)
        assert len(requests) == 3
        for request in requests:
            packet = json.loads(request["messages"][1]["content"])
            assert packet["read_cut"]["knowledge_cut"]["plane"] == "BELIEF"
            assert not any(intent in request["messages"][1]["content"] for intent in choices.values())
        assert all(scheduler.interaction_status(job.principal, iid) == {
            "interaction_id": iid, "own_input_stored": True, "status": "OWN_INPUT_STORED"} for job in jobs)
        assert scheduler.interaction_status(admin, iid)["status"] == "READY_FOR_ADJUDICATION"
        decision_ref, _ = selected(kernel, world, process, scheduler, interaction)
        with kernel.db.uow(write=False) as repo:
            stored = repo.many("simulation_intents", {"interaction_id": iid})
            assert {r["actor_id"]: r["payload"]["intent"] for r in stored} == choices
            assert kernel.head(repo, work, branch) == wr
            assert kernel.artifact(repo, decision_ref, work, kind="SIMULATION_OUTCOME")
        assert supervisor.run_once(admin, work, branch)["status"] == "IDLE" and len(requests) == 3
        record = os.environ.get("NF_P5_ACTOR_HANDOFF_EVIDENCE")
        if record:
            Path(record).write_text(json.dumps({"provider": "synthetic text over actual loopback HTTP",
                "accepted_base_world": wr, "sealed_intents": len(stored), "actors": [
                    {"task_id": job.task["task_id"], "actor_id": actor, "intent": choices[actor],
                     "plane": "BELIEF", "state": "ACCEPTED"} for actor,job in zip(actors,jobs)],
                "provider_requests": len(requests), "waiting_actor_requests_before_wake": 0,
                "duplicate_provider_requests_after_loss": 0, "joint_candidate_sha256": decision_ref["sha256"],
                "tests_of_formal_world_acceptance_in_this_trace": 0}, ensure_ascii=False, indent=2) + "\n")
