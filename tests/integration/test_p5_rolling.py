"""A sealed banquet refusal, joint acceptance and independently reviewed next scenes."""

import json
import os
from copy import deepcopy
from pathlib import Path
from time import monotonic
from uuid import uuid4

import pytest
from test_p5_supervisor import backend
from test_planning import make_plan
from test_simulation import process_definition, register, rules

from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.mcp_runtime import McpRuntime, TaskBinding
from novel_factory.application.planning import PlanningService
from novel_factory.application.queries import WorldQueryService
from novel_factory.application.rolling_plans import RollingPlanning
from novel_factory.application.runtime_tasks import RuntimeTaskService
from novel_factory.application.simulation import WorldSchedulerService, outcome_command
from novel_factory.application.supervisor import RuntimeSupervisor
from novel_factory.domain.canonical import fingerprint
from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import create_bound_task, load_fixture, point, prepare_command
from novel_factory.infrastructure.database import tables
from novel_factory.infrastructure.repository import KernelRepository

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


@pytest.fixture
def scene_backend():
    with backend() as route:
        yield route


def compile_woken(kernel, job, wr, story_time, cut, role_recipe, worker):
    claimed = kernel.claim_task(
        job.principal, worker, task_id=job.task["task_id"], lease_seconds=600
    )
    assert claimed["world_ref"] == wr
    context = WorldQueryService(kernel).context_compile(
        job.principal,
        {
            "schema_version": "2.1",
            "task_id": claimed["task_id"],
            "generation": claimed["generation"],
            "recipe_id": role_recipe,
            "input_manifest_ref": job.manifest_ref,
            "read_contract": {
                "revision": {"mode": "FIXED", "world_ref": wr},
                "process_policy": "ALLOW_PARTIAL",
                "index_policy": "AUTHORITATIVE",
                "max_wait_ms": 0,
            },
            "knowledge_cut": cut,
            "subject_time": {"kind": "POINT", "at": story_time},
            "token_budget": 20000,
            "idempotency_key": "wake:" + claimed["task_id"],
        },
    )
    assert context["status"] == "READY"
    return claimed


def test_banquet_absence_pins_reference_and_limits_replanning(kernel_env, scene_backend):
    k = kernel_env["kernel"]
    original = load_fixture(k, "white_bridge")
    process = process_definition(k, original)
    world, _ = register(k, original, process)
    wr, controller = world["world_ref"], world["principal"]
    wid, bid = wr["work_id"], wr["branch_id"]
    actors = process["required_actor_ids"]
    planning, rolling = PlanningService(k), RollingPlanning(k)
    plan, _ = make_plan(planning, world)
    for idx, (node, goal) in enumerate(
        zip(plan["nodes"], ["赴宴交换口信", "赴约后当面结盟", "下卷整理仓册"])
    ):
        node.update(goal=goal, target_time={"kind": "EXACT", "at": point(50 + idx)})
    base = planning.save_plan(controller, plan, "banquet-reference")
    current = planning.adopt_plan(controller, wr, base, None, "adopt")
    volume = plan["volume_id"]
    anchor = rolling.enable(controller, wr, volume, scenes=2, idempotency_key="freeze")
    assert anchor == rolling.enable(controller, wr, volume, scenes=2, idempotency_key="freeze")
    with pytest.raises(DomainError, match="already frozen"):
        rolling.enable(controller, wr, volume, scenes=3, idempotency_key="replace")
    service = WorldSchedulerService(k)
    http, requests, _server = scene_backend
    gateway = AgentGateway(k, {"fixture": http})
    runtime = RuntimeTaskService(k, gateway)
    supervisor = RuntimeSupervisor(k, gateway)
    workflow = supervisor.workflow
    opts = {
        "work_id": wid,
        "branch_id": bid,
        "authorize_in_tx": lambda c: k.authorize(
            KernelRepository(c, tables), controller, wid, "ADMIN", bid
        ),
    }
    notification = runtime.prepare(
        controller,
        wr,
        role_id="evidence_maintainer",
        task_kind="summary",
        story_time=point(30),
        subject_ids=[actors[1]],
        predicates=["located_in"],
        adapter_id="fixture",
        assignment="记下信使已经送来约定的通知。",
        queue_state="READY",
    )
    supervisor.enroll(
        controller,
        notification.task["task_id"],
        manifest_ref=notification.manifest_ref,
        knowledge_cut={"plane": "WORLD"},
        context_tokens=20000,
        input_ref=notification.agent_input["input_ref"],
        budget_id=k.tasks.create_budget(wid, "1"),
        reserve_amount="0.1",
        token_reservation=100000,
        accept_summary=True,
    )
    b_cut = {"plane": "BELIEF", "actor_id": actors[1], "known_at": point(30)}
    waiting_actor = runtime.prepare(
        controller,
        wr,
        role_id="character_actor",
        task_kind="reaction",
        story_time=point(30),
        subject_ids=[actors[1]],
        predicates=["located_in"],
        actor_id=actors[1],
        adapter_id="fixture",
        knowledge_cut=b_cut,
        assignment="等信使到达。",
        queue_state="BLOCKED",
    )
    workflow.wait(
        waiting_actor.task["task_id"], 0, [notification.task["task_id"]], mode="AND", **opts
    )
    grants = [
        waiting_actor.principal
        if actor == actors[1]
        else k.issue_grant(
            controller,
            wid,
            role_id="character_actor",
            actor_id=actor,
            capabilities=["READ", "COMPILE", "TASK_CREATE", "TASK_CLAIM", "ARTIFACT_WRITE"],
            planes=["BELIEF"],
            max_story_time=point(100),
        )["principal"]
        for actor in actors
    ]
    interaction = service.prepare_interaction(
        controller,
        wr,
        point(30),
        process["scope_ids"],
        {actor: g.grant_id for actor, g in zip(actors, grants)},
        "banquet-scene",
    )
    actions = ["我留守仓库，今晚不赴宴。", "我等信使回来再出发。", "我先到场核对库单。"]
    for actor, grant, intent in zip(actors, grants, actions):
        if actor == actors[1]:
            assert not requests and supervisor.workflow.resume_project(**opts)["awakened"] == 0
            assert k.claim_task(grant, "premature", task_id=waiting_actor.task["task_id"]) is None
            assert supervisor.run_once(controller, wid, bid)["status"] == "RESULT_STORED"
            assert supervisor.run_once(controller, wid, bid)["status"] == "IDLE"
            assert (
                len(requests) == 1
                and k.tasks.get(waiting_actor.task["task_id"])["state"] == "READY"
            )
            task = compile_woken(
                k, waiting_actor, wr, point(30), b_cut, "ACTOR", "messenger-arrived"
            )
        else:
            task = create_bound_task(
                k, grant, wr, point(30), [actor], ["location"], plane="BELIEF", actor_id=actor
            )
        with k.db.uow(write=False) as r:
            ctx = r.one("contexts", {"task_id": task["task_id"], "generation": task["generation"]})
        payload = {
            "schema_version": "2.0",
            "proposal_id": str(uuid4()),
            "task_id": task["task_id"],
            "generation": task["generation"],
            "context_bundle_id": ctx["context_bundle_id"],
            "world_ref": wr,
            "actor_id": actor,
            "intent": intent,
            "targets": [],
            "story_interval": {"start": point(30), "end": point(31), "bounds": "[)"},
            "resource_requests": [],
            "preconditions": [],
            "evidence_refs": [],
            "dependency_fingerprint": ctx["dependency_fingerprint"],
            "idempotency_key": "act",
        }
        service.submit_action(grant, interaction["interaction_id"], payload)
        own = service.interaction_status(grant, interaction["interaction_id"])
        assert own["own_input_stored"] and "intent_refs" not in own
    out = service.choose_outcome(
        controller,
        interaction["interaction_id"],
        commands=[
            {
                "template_id": "SET_RELATION",
                "args": {
                    "subject_id": actors[0],
                    "predicate": "located_in",
                    "object_value": {"kind": "ENTITY", "entity_id": world["ids"]["south"]},
                    "at": point(30),
                },
            }
        ],
        process_outcomes=[
            {
                "process_id": process["process_id"],
                "outcome": "FIRED",
                "world_stage": "COMPLETED",
                "next_due": None,
            }
        ],
        reasoning_summary="宴席在北区，A 留守南区粮仓。",
        rule_evidence_refs=rules(k, wr),
        source_kind="FIXTURE_SELECTED",
    )
    with k.db.uow(write=False) as r:
        row = r.one("simulation_interactions", {"interaction_id": interaction["interaction_id"]})
        candidate = outcome_command(
            row["artifact_ref"], row["payload"], out, k.artifact(r, out, wid)["payload"]
        )
        assert k.head(r, wid, bid) == wr
    prepared = prepare_command(k, controller, wr, "JOINT_INTERACTION", candidate)
    # The gate carries no future facts. The review itself gets a new, accepted WorldRef.
    gate = runtime.prepare(
        controller,
        wr,
        role_id="volume_director",
        task_kind="volume_review",
        story_time=point(31),
        skill_name="volume-director-review",
        knowledge_cut=current["knowledge_cut"],
        input_artifacts=[base, anchor, current["plan_manifest_ref"]],
        assignment="等联合裁决正式接受",
        subject_ids=[actors[0]],
        predicates=["location"],
        adapter_id="fixture",
        queue_state="BLOCKED",
    )
    workflow.wait(gate.task["task_id"], 0, [prepared["task"]["task_id"]], mode="AND", **opts)
    assert workflow.resume_project(**opts)["awakened"] == 0
    start = monotonic()
    receipt = k.accept(controller, prepared["request"])
    assert k.accept(controller, prepared["request"]) == receipt
    assert workflow.resume_project(**opts)["awakened"] == 1
    wake_ms = (monotonic() - start) * 1000
    assert workflow.resume_project(**opts)["awakened"] == 0
    now = receipt["world_ref"]

    def editorial(role, kind, skill, refs):
        job = runtime.prepare(
            controller,
            now,
            role_id=role,
            task_kind=kind,
            skill_name=skill,
            story_time=point(31),
            knowledge_cut=current["knowledge_cut"],
            assignment="按固定输入复核本轮。",
            subject_ids=[actors[0]],
            predicates=["located_in"],
            adapter_id="fixture",
            input_artifacts=[current["plan_manifest_ref"], *refs],
        )
        return job.principal, {
            "task_id": job.task["task_id"],
            "generation": job.task["generation"],
            "worker_id": job.worker_id,
        }

    director = gate.principal
    claimed = compile_woken(
        k, gate, now, point(31), current["knowledge_cut"], "DIRECTOR", "volume-review"
    )
    with k.db.uow(write=False) as r:
        assert (
            r.one("task_inputs", {"task_id": gate.task["task_id"], "generation": 0})["spec"][
                "world_ref"
            ]
            == wr
        )
    bind = {
        "task_id": claimed["task_id"],
        "generation": claimed["generation"],
        "worker_id": "volume-review",
    }
    args = dict(
        volume=volume,
        prior_plan_ref=base,
        event_refs=receipt["accepted_event_refs"],
        reason="原拟当面结盟，A 选择守仓，改用书信联络。",
        idempotency_key="change",
        **bind,
    )
    with pytest.raises(DomainError):
        rolling.request_change(grants[0], now, **args)
    d_mcp = McpRuntime(k, director, TaskBinding(**bind))
    assert (
        "nf_change_point_request" in d_mcp.available_tools()
        and "nf_change_point_review" not in d_mcp.available_tools()
    )
    cp = d_mcp.request_change(**{key: value for key, value in args.items() if key not in bind})
    assert cp == rolling.request_change(director, now, **args)
    revised = deepcopy(plan)
    revised.update(input_world_ref=now, prior_plan_ref=base, plan_revision_id=str(uuid4()))
    revised["nodes"][0]["goal"] = "派信使去仓库"
    revised["nodes"][1]["goal"] = "收到回信后再决定是否结盟"
    illegal = deepcopy(revised)
    illegal["nodes"][2]["goal"] = "偷改远期节点"
    outside = planning.save_plan(controller, illegal, "outside")
    with k.db.uow(write=False) as r:
        contract = k.artifact(r, plan["volume_contract_ref"], wid)["payload"]
    window = {
        "schema_version": "2.0",
        "window_plan_id": str(uuid4()),
        "volume_contract_revision_id": contract["contract_revision_id"],
        "input_world_ref": now,
        "story_interval": {"start": point(50), "end": point(51, 1), "bounds": "[)"},
        "scope_ids": process["scope_ids"],
        "process_ids": [],
        "focus_actor_ids": actors,
        "resolution_strategy": "MICRO_INTERACTION",
        "candidate_goals": [n["goal"] for n in revised["nodes"][:2]],
        "next_decision_reason": "信使带回实际回应再重新判断。",
        "assumptions": [],
        "adopted_event_refs": receipt["accepted_event_refs"],
        "expected_outputs": ["SCENE_CONTRACT"],
        "dependency_fingerprint": fingerprint(now),
        "status": "DRAFT",
    }
    planner, planner_bind = editorial(
        "volume_director", "replan", "roll-window", [anchor, cp, base]
    )
    planner_mcp = McpRuntime(k, planner, TaskBinding(**planner_bind))
    submitted = planner_mcp.submit_window(
        change_point_ref=cp, window=window, idempotency_key="window"
    )
    assert submitted == planner_mcp.submit_window(
        change_point_ref=cp, window=window, idempotency_key="window"
    )
    candidate_plan, window_ref = submitted["plan_ref"], submitted["window_ref"]
    editor, approval = editorial(
        "chief_editor",
        "editorial_decision",
        "review-change-point",
        [cp, anchor, candidate_plan, window_ref, outside],
    )
    with pytest.raises(DomainError, match="named future scenes"):
        rolling.review(
            editor,
            now,
            change_point_ref=cp,
            plan_ref=outside,
            window_ref=window_ref,
            decision="APPROVED",
            reason="扩大窗口",
            idempotency_key="bad",
            **approval,
        )
    with pytest.raises(DomainError, match="upper planning decision"):
        planning.adopt_plan(controller, now, candidate_plan, base, "bypass")
    e_mcp = McpRuntime(k, editor, TaskBinding(**approval))
    assert (
        "nf_change_point_review" in e_mcp.available_tools()
        and "nf_change_point_request" not in e_mcp.available_tools()
    )
    seal = e_mcp.review_change(
        change_point_ref=cp,
        plan_ref=candidate_plan,
        window_ref=window_ref,
        decision="APPROVED",
        reason="符合角色动机，信使可以恢复联络。",
        idempotency_key="review",
    )
    returned = e_mcp.review_change(
        change_point_ref=cp,
        plan_ref=candidate_plan,
        window_ref=window_ref,
        decision="RETURNED",
        reason="暂缓，重新审视送信代价。",
        idempotency_key="return",
    )
    with pytest.raises(DomainError, match="does not approve"):
        planning.adopt_plan(
            controller, now, candidate_plan, base, "stale-approval", rolling_review_ref=seal
        )
    with pytest.raises(DomainError, match="does not approve"):
        planning.adopt_plan(
            controller, now, candidate_plan, base, "returned", rolling_review_ref=returned
        )
    seal = e_mcp.review_change(
        change_point_ref=cp,
        plan_ref=candidate_plan,
        window_ref=window_ref,
        decision="APPROVED",
        reason="复核后确认，送信保留一个场次的延迟。",
        idempotency_key="final",
    )
    applied = planning.adopt_plan(
        controller, now, candidate_plan, base, "adopt-future", rolling_review_ref=seal
    )
    assert applied == planning.adopt_plan(
        controller, now, candidate_plan, base, "adopt-future", rolling_review_ref=seal
    )
    assert applied["initial_plan_ref"] == base and set(applied["changed_node_ids"]) == {
        n["node_id"] for n in plan["nodes"][:2]
    }
    assert (
        planning.get_plan(controller, wid, base) == plan and revised["nodes"][2] == plan["nodes"][2]
    )
    with k.db.uow(write=False) as r:
        assert k.head(r, wid, bid) == now and len(r.many("events", {"work_id": wid})) == 1
        assert not r.many("call_attempts", {"task_id": gate.task["task_id"]})
        assert not r.many("call_attempts", {"task_id": waiting_actor.task["task_id"]})
        if path := os.environ.get("NF_P5_STORY_EVIDENCE"):
            Path(path).write_text(
                json.dumps(
                    {
                        "scenario": "deterministic editorial fixture",
                        "work_id": wid,
                        "sealed_intents": len(
                            r.many(
                                "simulation_intents",
                                {"interaction_id": interaction["interaction_id"]},
                            )
                        ),
                        "accepted_events": len(receipt["accepted_event_refs"]),
                        "reference": base,
                        "rolling_plan": candidate_plan,
                        "window": window_ref,
                        "change_point": cp,
                        "upper_review": seal,
                        "accepted_world": now,
                        "accept_to_wake_ms": round(wake_ms, 3),
                        "waiting_model_calls": 0,
                        "notification_provider_requests": len(requests),
                        "real_model_tokens": None,
                        "preauthorized_world_approvals": len(r.many("approvals", {"work_id": wid})),
                        "human_prompts": 0,
                        "counts": workflow.status(**opts)["counts"],
                    },
                    indent=2,
                )
                + "\n"
            )
