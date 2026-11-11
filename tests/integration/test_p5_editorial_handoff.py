"""Executable editorial worker integration with an explicitly synthetic HTTP model."""
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.planning import PlanningService
from novel_factory.application.rolling_plans import RollingPlanning
from novel_factory.application.runtime_tasks import RuntimeTaskService
from novel_factory.application.supervisor import RuntimeSupervisor
from novel_factory.application.simulation import outcome_command
from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import load_fixture, point, prepare_command, run_command
from novel_factory.infrastructure.database import tables
from novel_factory.infrastructure.repository import KernelRepository
from test_p5_supervisor import backend
from test_planning import make_plan
from test_simulation import group, process_definition, register, rules, selected

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


@pytest.mark.parametrize("decision", ["APPROVED", "RETURNED", "UNRESOLVED", "REVOKED"])
def test_model_candidate_drives_change_window_and_editor(kernel_env, monkeypatch, decision):
    kernel = kernel_env["kernel"]
    base_world = load_fixture(kernel, "white_bridge")
    ids = base_world["ids"]
    if decision == "APPROVED":
        past = run_command(kernel, base_world["principal"], base_world["world_ref"], "SET_RELATION",
            {"subject_id":ids["protagonist"], "predicate":"vital_state",
             "object_value":{"kind":"STRING","value":"UNRELATED_PAST_EVENT_CANARY"}, "at":point(1,2)})
        base_world["world_ref"] = past["world_ref"]
    process = process_definition(kernel, base_world)
    world, _ = register(kernel, base_world, process)
    wr, admin = world["world_ref"], world["principal"]
    work, branch = wr["work_id"], wr["branch_id"]
    world_service, interaction, _, _, _ = group(kernel, world, process)
    if decision == "APPROVED":
        commands = [
            {"template_id":"RESOURCE_TRANSFER", "args":{"from_account":ids["warehouse"], "to_account":ids["relief"],
                "resource_id":ids["grain"], "unit":"bag", "quantity":"30", "at":point(30)}},
            {"template_id":"RECORD_COGNITION", "args":{"actor_id":ids["protagonist"], "statement":"PRIVATE_BELIEF_CANARY",
                "record_kind":"BELIEF", "epistemic_mode":"INFERRED", "evidence_refs":rules(kernel, wr), "at":point(30)}},
            {"template_id":"SET_RELATION", "args":{"subject_id":ids["protagonist"], "predicate":"ability_state",
                "object_value":{"kind":"STRING","value":"EXCLUDED_PREDICATE_CANARY"}, "at":point(30)}},
            {"template_id":"SET_RELATION", "args":{"subject_id":ids["relief"], "predicate":"vital_state",
                "object_value":{"kind":"STRING","value":"EXCLUDED_SUBJECT_CANARY"}, "at":point(30)}},
            {"template_id":"SET_RELATION", "args":{"subject_id":ids["protagonist"], "predicate":"located_in",
                "object_value":{"kind":"ENTITY","entity_id":ids["south"]}, "at":point(30)}},
        ]
        output = world_service.choose_outcome(admin, interaction["interaction_id"], commands=commands,
            process_outcomes=[{"process_id":process["process_id"],"outcome":"FIRED","world_stage":"COMPLETED","next_due":None}],
            reasoning_summary="PRIVATE_ARBITRATION_CANARY", rule_evidence_refs=rules(kernel, wr), source_kind="FIXTURE_SELECTED")
        with kernel.db.uow(write=False) as repo:
            row = repo.one("simulation_interactions", {"interaction_id":interaction["interaction_id"]})
            cmd = outcome_command(row["artifact_ref"], row["payload"], output, kernel.artifact(repo, output, work)["payload"])
    else:
        _, cmd = selected(kernel, world, process, world_service, interaction)
    planning = PlanningService(kernel)
    plan, _ = make_plan(planning, world)
    for i, node in enumerate(plan["nodes"]):
        node["target_time"] = {"kind":"EXACT","at":point(50+i)}
    reference = planning.save_plan(admin, plan, "proposal")
    adopted = planning.adopt_plan(admin, wr, reference, None, "initial")
    volume = plan["volume_id"]
    anchor = RollingPlanning(kernel).enable(admin, wr, volume, scenes=2, idempotency_key="enable")
    joint = prepare_command(kernel, admin, wr, "JOINT_INTERACTION", cmd)
    budget = kernel.tasks.create_budget(work, "3")

    with backend() as (http, requests, server):
        gateway = AgentGateway(kernel, {"test": http})
        runtime, sup = RuntimeTaskService(kernel, gateway), RuntimeSupervisor(kernel, gateway)
        cut = adopted["knowledge_cut"]
        def task(world_ref, role, mode, skill, route, *, initial="READY", auto=False):
            refs = [v for k,v in route.items() if k.endswith("_ref")]
            job = runtime.prepare(admin, world_ref, role_id=role, task_kind=mode, skill_name=skill,
                knowledge_cut=cut, story_time=point(31), subject_ids=[*process["required_actor_ids"], ids["grain"], ids["warehouse"]],
                predicates=["location", "vital_state", f"resource:{ids['grain']}:bag"],
                assignment="仅使用已绑定的编辑资料完成本阶段", adapter_id="test", queue_state=initial,
                input_artifacts=[cut["plan_manifest_ref"], *refs])
            opts = dict(knowledge_cut=cut, context_tokens=20000, manifest_ref=job.manifest_ref,
                        input_ref=job.agent_input["input_ref"], budget_id=budget,
                        reserve_amount="1", token_reservation=190000)
            if route["operation"] == "request_change":
                with pytest.raises(DomainError):
                    sup.enroll(admin, job.task["task_id"], editorial={**route, "prior_plan_ref": plan["volume_contract_ref"]}, **opts)
            sup.enroll(admin, job.task["task_id"], editorial=route, continue_editorial=auto, **opts)
            return job

        director = task(wr, "volume_director", "volume_review", "volume-director-review",
                  {"operation":"request_change", "volume_id":volume, "prior_plan_ref":reference,
                   "reference_timeline_ref":anchor}, initial="BLOCKED", auto=decision=="APPROVED")
        sup.workflow.wait(director.task["task_id"], 0, [joint["task"]["task_id"]], mode="AND",
            work_id=work, branch_id=branch, authorize_in_tx=lambda c:
            kernel.authorize(KernelRepository(c,tables), admin, work, "ADMIN", branch))
        assert sup.run_once(admin, work, branch)["status"] == "IDLE" and not requests
        receipt = kernel.accept(admin, joint["request"])
        now = receipt["world_ref"]

        def candidate(ctx):
            packet = json.loads(requests[-1]["messages"][1]["content"])["editorial"]
            materials = packet["materials"]
            if packet["operation"] == "request_change":
                return {"text":"实际调度改变了物资安排，应更改接下来的会面。", "important_deltas":["会面延期"],
                        "event_refs":packet["accepted_event_refs"], "evidence_refs":[]}
            if packet["operation"] == "propose_window":
                cp = materials["change_point_ref"]["content"]
                return {"schema_version":"2.0","window_plan_id":str(uuid4()),
                        "volume_contract_revision_id":materials["volume_contract_ref"]["content"]["contract_revision_id"],
                        "input_world_ref":ctx["world_ref"], "story_interval":{"start":point(50),"end":point(51,1),"bounds":"[)"},
                        "scope_ids":process["scope_ids"], "process_ids":[], "focus_actor_ids":process["required_actor_ids"],
                        "resolution_strategy":"MICRO_INTERACTION","candidate_goals":["通过书信联系","核实送达凭证"],
                        "next_decision_reason":"等待回信后再判断。","assumptions":[],"adopted_event_refs":cp["event_refs"],
                        "expected_outputs":["SCENE_CONTRACT"],"dependency_fingerprint":ctx["dependency_fingerprint"],"status":"DRAFT"}
            return {"decision":"RETURNED" if decision == "RETURNED" else "APPROVED","reason":"已核实当前波及场次与证据。",
                    "deferred_questions":["该动机是否符合作者保留的取舍？"] if decision == "UNRESOLVED" else [], "evidence_refs":[]}
        server.candidate = candidate
        assert sup.run_once(admin, work, branch)["status"] == "RESULT_STORED"
        body = json.loads(requests[0]["messages"][1]["content"])
        projection = body["editorial"]["accepted_effects"]
        assert projection == [{"event_ref":receipt["accepted_event_refs"][0],"occurrence_time":{"kind":"EXACT","at":point(30)},
            "effects":[{"kind":"RESOURCE_DELTA","account_id":ids["warehouse"],"resource_id":ids["grain"],"unit":"bag","quantity":"-30"}]}]
        transmitted = json.dumps(requests[0], ensure_ascii=False)
        assert all(marker not in transmitted for marker in ("PRIVATE_BELIEF_CANARY", "EXCLUDED_PREDICATE_CANARY",
            "EXCLUDED_SUBJECT_CANARY", "PRIVATE_ARBITRATION_CANARY", "UNRELATED_PAST_EVENT_CANARY", "不知道其他人的密封意图"))
        if decision == "APPROVED":
            assert past["receipt"]["accepted_event_refs"][0] not in body["editorial"]["accepted_event_refs"]
        director_delivery = sup.run_once(admin, work, branch)
        cp = director_delivery["change_point_ref"]
        if decision == "APPROVED":
            middle_id = director_delivery["successor_task_id"]
        else:
            middle_id = task(now, "volume_director", "replan", "roll-window", {"operation":"propose_window",
                "volume_id":volume, "change_point_ref":cp, "prior_plan_ref":reference,
                "reference_timeline_ref":anchor, "volume_contract_ref":plan["volume_contract_ref"]}).task["task_id"]
        first = sup.run_once(admin, work, branch)
        assert first["task_id"] == middle_id and first["validation_status"] == "VALIDATED_CANDIDATE", first
        proposal = sup.run_once(admin, work, branch)
        assert proposal["status"] == "EDITORIAL_DELIVERED", proposal
        next_plan, window = proposal["plan_ref"], proposal["window_ref"]
        if decision == "APPROVED":
            editor_id = proposal["successor_task_id"]
        else:
            editor_id = task(now, "chief_editor", "editorial_decision", "review-change-point",
                {"operation":"review_and_adopt", "volume_id":volume, "change_point_ref":cp, "plan_ref":next_plan,
                 "window_ref":window, "reference_timeline_ref":anchor}).task["task_id"]
        assert sup.run_once(admin, work, branch)["task_id"] == editor_id
        for request in requests[1:]:
            other = json.loads(request["messages"][1]["content"])["editorial"]
            assert "accepted_effects" not in other and "accepted_event_refs" not in other
        if decision == "APPROVED":
            original = PlanningService.adopt_plan
            def fault(*args, **kwargs):
                original(*args, **kwargs)
                raise RuntimeError("lost process after native adoption")
            with monkeypatch.context() as patch:
                patch.setattr(PlanningService, "adopt_plan", fault)
                with pytest.raises(RuntimeError, match="after native adoption"):
                    sup.run_once(admin, work, branch)
            with kernel.db.uow(write=False) as repo:
                assert repo.one("selections", {"work_id":work,"selection_kind":planning.selection_key(volume)})["artifact_ref"] == reference
        if decision == "REVOKED":
            with kernel.db.uow(write=False) as repo:
                executing_grant = repo.one("tasks", {"task_id": editor_id})["grant_id"]
            kernel.revoke_grant(admin, executing_grant)
        completed = RuntimeSupervisor(kernel, gateway).run_once(admin, work, branch)
        if decision in {"UNRESOLVED","REVOKED"}:
            assert completed["status"] == "WAITING_REVIEW" and completed["reason"] in {"REVIEW_REQUIRED","VIEW_DENIED"}
            with kernel.db.uow(write=False) as repo:
                assert repo.one("selections", {"work_id":work,"selection_kind":planning.selection_key(volume)})["artifact_ref"] == reference
                assert kernel.head(repo, work, branch) == now
            assert sup.run_once(admin, work, branch)["status"] == "IDLE" and len(requests) == 3
            return
        assert completed["decision"] == decision and completed["status"] == "EDITORIAL_DELIVERED"
        assert sup.run_once(admin, work, branch)["status"] == "IDLE" and len(requests) == 3
        with kernel.db.uow(write=False) as repo:
            chosen = repo.one("selections", {"work_id":work,"selection_kind":planning.selection_key(volume)})["artifact_ref"]
            assert chosen == (next_plan if decision == "APPROVED" else reference)
            assert kernel.head(repo, work, branch) == now
            assert kernel.artifact(repo, reference, work)["payload"] == plan
            assert kernel.artifact(repo, next_plan, work)["payload"]["nodes"][2] == plan["nodes"][2]
            events = repo.many("outbox", {"topic":"supervisor.editorial.delivery"})
            traces = [event["payload"] for event in events if event["aggregate_id"] in
                      {director.task["task_id"],middle_id,editor_id}]
            assert len(traces) == 3
            report_dir = os.environ.get("NF_P5_EDITORIAL_EVIDENCE_DIR")
            if report_dir:
                Path(report_dir).mkdir(exist_ok=True, parents=True)
                Path(report_dir, decision.lower() + ".json").write_text(json.dumps({
                    "upstream":"synthetic-loopback-http", "provider_requests":len(requests),
                    "enrolled_warehouse_effect":projection, "director_only_projection":True,
                    "author_interventions":0, "before_wake_requests":0, "head_after_joint_accept":now,
                    "head_after_planning":now, "initial_plan_ref":reference,"selected_plan_ref":chosen,
                    "decision":decision,"auto_provisioned_successors":2 if decision=="APPROVED" else 0,"outbox_receipts":traces}, ensure_ascii=False, indent=2) + "\n")
