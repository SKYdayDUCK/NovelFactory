"""Persisted P2-07 production rehearsal through existing application services.

The supplied scenario and adapter are explicitly TEST_FIXTURE. This coordinator
does not implement alternate world rules, write tables, forge approval records,
or claim that deterministic prose demonstrates model/literary acceptance.
It exposes each durable handoff so failures can be inspected and isolated
service commands can be retried with their original identities.
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from threading import Barrier, Lock
from typing import ClassVar
from uuid import uuid4

from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.editorial import ReviewService, memory_dependency
from novel_factory.application.narrative import NarrativeService
from novel_factory.application.planning import PlanningService, validate_task_plan_binding
from novel_factory.application.release import ReleaseService
from novel_factory.application.runtime_tasks import RuntimeTaskService
from novel_factory.application.simulation import WorldSchedulerService, outcome_command
from novel_factory.domain.canonical import fingerprint
from novel_factory.domain.errors import DomainError
from novel_factory.domain.propositions import encode_proposition
from novel_factory.domain.reconciliation import span_for
from novel_factory.fixtures import (
    entity_value,
    fixture_definition,
    point,
    run_command,
)
from novel_factory.infrastructure.model_gateway import ModelResponse
from novel_factory.orchestration.short_volume_extraction import extract_fixture_assertions
from novel_factory.orchestration.short_volume_fixture import (
    INITIAL_FIRST_SCENE,
    MOTIVES,
    NAMES,
    REVISION_NEEDLE,
    TEXTS,
    TITLE,
    TITLES,
)


def uid():
    return str(uuid4())


class ShortVolumeFixtureAdapter:
    """Finite deterministic role outputs, charged as zero-cost test calls only."""

    configuration: ClassVar[dict] = {
        "adapter_kind": "DETERMINISTIC_TEST", "model": "tide-gate-fixture-v1",
        "max_output_tokens": 12000, "isolation": "STATELESS_TEXT_ONLY",
        "execution_kind": "TEST_FIXTURE",
    }

    def __init__(self, *, writer_text_overrides=None):
        self.calls = []
        self._lock = Lock()
        self.writer_barrier = None
        # Explicit negative fixture vectors; the Gateway still stores the exact
        # response and NarrativeService must independently accept or reject it.
        self.writer_text_overrides = dict(writer_text_overrides or {})

    def check_available(self):
        return None

    def generate(self, request):
        packet = json.loads(request.messages[1]["content"])
        context = packet["context"]
        assignment = json.loads(packet["assignment"])
        identity = {key: context[key] for key in (
            "task_id", "generation", "context_bundle_id", "world_ref", "role_id", "actor_id")}
        if context["role_id"] == "character_actor":
            payload = {
                "schema_version": "2.0", "proposal_id": uid(),
                **{key: identity[key] for key in (
                    "task_id", "generation", "context_bundle_id", "world_ref", "actor_id")},
                "intent": assignment["intent"], "targets": [],
                "story_interval": {"start": context["story_time"], "end": point(5), "bounds": "[)"},
                "resource_requests": [], "preconditions": [],
                "evidence_refs": context["permitted_evidence_refs"],
                "dependency_fingerprint": context["dependency_fingerprint"],
                "idempotency_key": "sealed:" + context["task_id"],
            }
        elif context["role_id"] == "writer":
            cards = [json.loads(m["content"]) for m in packet["authorized_materials"]]
            card = next(value for value in cards if "scene_contract" in value)
            index = card["scene_contract"]["reader_cut_ordinal"]
            if assignment.get("parallel_first_pair") and self.writer_barrier is not None:
                self.writer_barrier.wait(timeout=60)
            originals = [value for value in cards if "draft_ref" in value and "text" in value]
            expected_originals = len(assignment.get("predecessor_draft_ids", [])) + int(bool(assignment.get("revision")))
            if len(originals) != expected_originals:
                raise ValueError("Writer did not receive its exact predecessor/revision originals")
            if assignment.get("revision"):
                original = next(value for value in originals
                                if value["draft_ref"]["artifact_revision_id"] == assignment["revision_draft_id"])
                if REVISION_NEEDLE not in original["text"]:
                    raise ValueError("Targeted revision has no matching original evidence")
                prose = original["text"].replace(REVISION_NEEDLE, "印在手心里沉。", 1)
            else:
                prose = self.writer_text_overrides.get(index, INITIAL_FIRST_SCENE if index == 0 else TEXTS[index])
            payload = {"text": prose, "event_refs": card["required_event_refs"],
                       "important_deltas": [], "evidence_refs": []}
        elif context["role_id"] == "reviewer":
            # These are schema/coverage fixtures, not independent semantic judgments.
            issues = []
            if assignment.get("dimension") == "literary":
                for material in packet["authorized_materials"]:
                    original = json.loads(material["content"])
                    text = original["text"]
                    if REVISION_NEEDLE in text:
                        start = text.index(REVISION_NEEDLE)
                        issues.append({"issue_id": uid(), "category": "LITERARY", "severity": "SUGGESTION",
                            "subject_ref": original["draft_ref"], "description": "同一句完整重复，未给此次犹豫增加具体信息。",
                            "evidence_refs": [{"kind": "ARTIFACT", "artifact_ref": original["text_ref"],
                                               "span": span_for(text, start, start + len(REVISION_NEEDLE))}],
                            "suggested_change": "删去一次重复，保留印与袖口的后文回声。", "disposition": "OPEN", "issue_kind": "LITERARY_CHOICE"})
            payload = {"summary": "TEST_FIXTURE：已交付精确原文；本判断只验证审核交接，不证明文学质量。",
                       "issues": issues, "counterinterpretations": [
                           "短句重复可能服务人物犹豫，最终取舍必须保留具体上下文。"], "evidence_refs": []}
        else:
            raise ValueError("Role is outside this finite fixture")
        envelope = {"schema_version": "2.1", **identity, "status": "SUCCEEDED",
                    "unresolved_questions": [], "payload": payload}
        with self._lock:
            self.calls.append({"task_id": identity["task_id"], "role_id": identity["role_id"],
                               "request_sha256": request.request_sha256})
        return ModelResponse(json.dumps(envelope, ensure_ascii=False),
            provider_receipt="TEST_FIXTURE:" + identity["task_id"], input_tokens=0, output_tokens=0,
            actual_amount=Decimal(0), currency="USD", request_sha256=request.request_sha256)


class ShortVolumeRunner:
    """One isolated original short volume, with durable application handoffs."""

    def __init__(self, kernel, *, output_dir=None, fixture_text_overrides=None):
        self.kernel = kernel
        self.output_dir = Path(output_dir) if output_dir is not None else None
        self.planning = PlanningService(kernel)
        self.scheduler = WorldSchedulerService(kernel)
        self.narrative = NarrativeService(kernel)
        self.review = ReviewService(kernel, narrative=self.narrative)
        self.release = ReleaseService(kernel)
        self.adapter = ShortVolumeFixtureAdapter(writer_text_overrides=fixture_text_overrides)
        self.gateway = AgentGateway(kernel, {"short-volume-fixture": self.adapter},
                                    material_reader=self.narrative.read_agent_material)
        self.runtime = RuntimeTaskService(kernel, self.gateway)
        self.trace = []
        self.calls = []
        self.receipts = []

    def record(self, stage, **payload):
        entry = {"stage": stage, "execution_kind": "TEST_FIXTURE", **payload}
        self.trace.append(entry)
        if self.output_dir:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            with (self.output_dir / "process_trace.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")

    def artifact(self, ref):
        with self.kernel.db.uow(write=False) as repo:
            self.kernel.authorize(repo, self.principal, self.wr["work_id"], "ADMIN")
            return deepcopy(self.kernel.artifact(repo, ref, self.wr["work_id"])["payload"])

    def command(self, template, args, label):
        accepted = run_command(self.kernel, self.principal, self.wr, template, args, key=label)
        self.wr = accepted["world_ref"]
        self.receipts.append(accepted["receipt"])
        self.record("world_acceptance", label=label, template=template, args=args,
                    proposal_ref=accepted["proposal"]["proposal_ref"],
                    request=accepted["request"], receipt=accepted["receipt"])
        return accepted

    def prepare(self, role, mode, assignment, *, story_time=None, inputs=(), materials=(), actor_id=None):
        return self.runtime.prepare(self.principal, self.wr, role_id=role, task_kind=mode,
            story_time=story_time or point(8), subject_ids=[self.ids["sword"], self.ids["friend"]],
            predicates=["possessed_by", "member_of"],
            assignment=json.dumps({"execution_kind": "TEST_FIXTURE", **assignment}, ensure_ascii=False),
            adapter_id="short-volume-fixture", actor_id=actor_id,
            input_artifacts=list(inputs), material_refs=list(materials), lease_seconds=3600,
            max_amount="1", token_budget=200000, context_token_budget=20000)

    def dispatch(self, prepared):
        request = {"schema_version": "2.1", "task_id": prepared.task["task_id"],
            "generation": prepared.task["generation"], "worker_id": prepared.worker_id,
            "input_ref": prepared.agent_input["input_ref"], "budget_id": self.budget_id,
            "call_key": "production:" + prepared.task["task_id"], "reserve_amount": "0.01",
            "token_reservation": 100000}
        result = self.gateway.execute(prepared.principal, request)
        if result.get("validation_status") != "VALIDATED_CANDIDATE":
            raise DomainError("PRODUCTION_STAGE_FAILED", json.dumps(result, ensure_ascii=False))
        payload = self.artifact(result["output_ref"])
        self.calls.append(result)
        self.record("agent_result", request=request, result=result)
        return payload, result

    def initialize(self):
        definition = fixture_definition("white_bridge")
        self.ids = definition["ids"]
        for key, name in NAMES.items():
            definition["genesis"]["entities"][self.ids[key]]["name"] = name
        for relation in definition["genesis"]["relations"]:
            if relation["subject_id"] == self.ids["rival"] and relation["predicate"] == "located_in":
                relation["object_value"] = entity_value(self.ids["south"])
        created = self.kernel.bootstrap_work("TEST_FIXTURE:" + TITLE, definition["genesis"],
                                             frontiers=definition["frontiers"])
        self.principal, self.wr = created["principal"], created["world_ref"]
        self.initial_wr = deepcopy(self.wr)
        self.budget_id = self.kernel.tasks.create_budget(self.wr["work_id"], "10")
        with self.kernel.db.uow(write=False) as repo:
            work = repo.one("works", {"work_id": self.wr["work_id"]})
            setup = repo.one("artifacts", {"work_id": self.wr["work_id"], "kind": "INITIAL_SETUP"})
        self.rules_ref = work["rules_ref"]
        self.setup_ref = {key: setup[key] for key in ("artifact_id", "artifact_revision_id", "sha256")}
        designer = self.kernel.issue_grant(self.principal, self.wr["work_id"], role_id="world_designer",
            capabilities=["READ", "ARTIFACT_WRITE"], planes=["WORLD"], max_story_time=point(100))["principal"]
        dossier = {"schema_version": "2.1", "dossier_id": uid(), "input_world_ref": self.wr,
            "ruleset_ref": self.rules_ref, "initial_setup_ref": self.setup_ref,
            "background_entity_ids": [self.ids["smith2"], self.ids["sword2"]],
            "factions": [{"entity_id": self.ids["sect"], "decision_maker_id": self.ids["master"],
                "independent_goal": "维持南北两岸汛期的可核对粮路",
                "without_protagonist_action": "继续守闸、清点北仓并接受南埠救济申请",
                "constraints": ["资源守恒；交付必须有账；铜汛印作临时铜楔后报废，不能复活"]}],
            "sections": {"geography": "北岸仓与南埠两线在潮闸交汇。", "history": "旧汛印是可牺牲的实物凭证。",
                "institutions": "灯守社接受实物凭证，也可在损毁后核查公账及共同责任。",
                "resources": "北仓100袋、救济0袋；冬储独立，不默认挪用。",
                "powers": "没有复活毁坏物品或凭空增粮的能力。"},
            "open_questions": [{"question": "三人的合作是否能持续", "blocking": False}], "source_event_refs": [], "proposed_world_changes": []}
        self.dossier_ref = self.planning.save_world_dossier(designer, dossier, "volume:dossier")
        self.dossier_review_ref = self.planning.review_world_dossier(self.principal, self.wr,
            self.dossier_ref, "APPROVED", [], "volume:dossier-review")
        self.record("world_prepared", world_ref=self.wr, dossier_ref=self.dossier_ref,
                    review_ref=self.dossier_review_ref, rules_ref=self.rules_ref,
                    caveat="Emergency copper-wedge causality is an explicit fixture semantic decision; generic physics is not implemented.")

    def initial_plan(self):
        self.volume_id, self.narrative_id = uid(), uid()
        contract = {"schema_version": "2.0", "volume_id": self.volume_id,
            "contract_revision_id": uid(), "input_world_ref": self.wr,
            "creative_brief": "两条有独立责任的粮路在潮闸交汇，合法损失迫使合作改变形式。",
            "focus_subject_ids": [self.ids[k] for k in MOTIVES],
            "narrative_intents": ["保留有限救济与冬储的真实分歧", "损失不被下一版卷纲撤销"],
            "protected_commitments": [], "open_choices": ["毁印后的合作须依实际裁决重规划"],
            "completion_or_stop_conditions": ["六场完整原文经多尺度审核、整卷编辑与内部冻结"],
            "budget": {"max_semantic_calls": 64, "max_candidate_branches": 1},
            "upstream_literary_refs": [], "dependency_fingerprint": fingerprint(self.wr)}
        self.volume_ref = self.planning.save_volume_contract(self.principal, contract, "volume:contract")
        goals = ["领取铜汛印", "北仓和南埠分别核算独立需要", "在潮闸分配有限粮食",
                 "保持完整铜汛印", "凭完好铜汛印建立协作", "核查交付后的余数"]
        self.nodes = [{"node_id": uid(), "goal": goal, "status": "VIABLE",
            "target_time": {"kind": "EXACT", "at": point(2 + i)},
            "subject_ids": [self.ids["sword"] if i == 3 else self.ids["protagonist"]],
            "hard_preconditions": [{"kind": "ENTITY_ACTIVE", "subject_id": self.ids["sword"]}] if i == 3 else [],
            "risk_notes": ["计划不是已发生事件"], "failure_exit": "保留后果并核验局部替代路径",
            "fulfillment_event_refs": []} for i, goal in enumerate(goals)]
        plan = {"schema_version": "2.1", "plan_id": uid(), "plan_revision_id": uid(),
            "volume_id": self.volume_id, "input_world_ref": self.wr, "volume_contract_ref": self.volume_ref,
            "prior_plan_ref": None, "nodes": self.nodes, "published_prefix_node_ids": [],
            "dependencies": [{"source_node_id": self.nodes[3]["node_id"], "target_node_id": self.nodes[4]["node_id"],
                              "kind": "HARD_PREREQUISITE", "reason": "原协作路线以完好铜印作凭证"}],
            "decision": self.decision("先按完好铜印路线安排，保留独立清点线", "持印申请", "未经核验直接取得资格")}
        self.original_plan = deepcopy(plan)
        self.plan_ref = self.planning.save_plan(self.principal, plan, "volume:original-plan")
        self.original_plan_ref = self.plan_ref
        self.planning.adopt_plan(self.principal, self.wr, self.plan_ref, None, "volume:original-adopt")
        stale = self.prepare("writer", "scene_draft", {"purpose": "等待原凭证路线"}, inputs=[self.plan_ref])
        self.planning.bind_task_for_controller(self.principal, stale.task["task_id"], stale.task["generation"],
            self.plan_ref, [self.nodes[4]["node_id"]], "volume:old-task-guard")
        self.old_plan_task = stale.task
        self.record("initial_plan", contract_ref=self.volume_ref, plan_ref=self.plan_ref,
                    guarded_future_task_id=stale.task["task_id"])

    @staticmethod
    def decision(reason, selected, rejected):
        return {"reason": reason, "considered_options": [
            {"option_id": uid(), "summary": selected, "disposition": "SELECTED", "reason": reason},
            {"option_id": uid(), "summary": rejected, "disposition": "REJECTED", "reason": "无法尊重已有世界记录或独立动机"}]}

    def simulate(self):
        source = [{"kind": "ARTIFACT", "artifact_ref": self.setup_ref}]
        self.friend_belief = self.command("RECORD_COGNITION", {
            "actor_id": self.ids["friend"], "statement": encode_proposition(self.ids["sword"], "possessed_by", entity_value(self.ids["smith"])),
            "record_kind": "BELIEF", "epistemic_mode": "OBSERVED", "evidence_refs": source, "at": point(1, 1)}, "initial-belief")
        self.gift = self.command("TRANSFER_ITEM", {"item_id": self.ids["sword"], "from_id": self.ids["smith"],
            "to_id": self.ids["protagonist"], "transfer_kind": "GIFT", "at": point(2)}, "gift-seal")
        self.protagonist_belief = self.command("RECORD_COGNITION", {
            "actor_id": self.ids["protagonist"], "statement": encode_proposition(self.ids["sword"], "possessed_by", entity_value(self.ids["protagonist"])),
            "record_kind": "BELIEF", "epistemic_mode": "OBSERVED",
            "evidence_refs": [{"kind": "EVENT", "world_ref": self.gift["world_ref"], "event_ref": self.gift["receipt"]["accepted_event_refs"][0]}],
            "at": point(2, 1)}, "observe-gift")
        self.travel = self.command("PASS_CLEAR_ROUTE", {"traveler_id": self.ids["rival"],
            "region_id": self.ids["south"], "destination_id": self.ids["north"], "at": point(3)}, "south-line-arrival")
        self.command("SCHEDULE_ADVANCE", {"scope_ids": list(self.ids.values()), "target": point(4)}, "before-interaction-coverage")
        process = {"schema_version": "2.2", "process_id": uid(), "process_kind": "INTERACTION",
            "scope_ids": [self.ids[k] for k in ("warehouse", "relief", "sword")],
            "participant_ids": [self.ids[k] for k in MOTIVES], "required_actor_ids": [self.ids[k] for k in MOTIVES],
            "start_at": point(3), "due_at": point(4), "world_stage": "ACTIVE", "high_impact": True,
            "channels": [], "goal": "共同前态下裁决救济、冬储与毁印代价", "evaluator_kind": "SEMANTIC",
            "rule_evidence_refs": [{"kind": "RULE", "artifact_ref": self.rules_ref}],
            "trigger_id": uid(), "trigger_version_id": uid()}
        # Registration before its due point is explicit; it creates no event.
        process["start_at"] = point(4)
        process_ref = self.scheduler.stage_process(self.principal, self.wr, process, "tide-process")
        self.command("REGISTER_PROCESS", {"at": process["start_at"], "process": process, "record_ref": process_ref}, "register-tide")
        actor_tasks = {key: self.prepare("character_actor", "character_action", {"intent": motive},
            story_time=point(4), actor_id=self.ids[key]) for key, motive in MOTIVES.items()}
        interaction = self.scheduler.prepare_interaction(self.principal, self.wr, point(4), process["scope_ids"],
            {self.ids[key]: task.principal.grant_id for key, task in actor_tasks.items()}, "tide-interaction")
        intents = []
        for key, task in actor_tasks.items():
            proposal, result = self.dispatch(task)
            ref = self.scheduler.submit_action(task.principal, interaction["interaction_id"], proposal)
            intents.append(ref)
            with self.kernel.db.uow(write=False) as repo:
                context = repo.one("contexts", {"context_bundle_id": task.context_bundle_id})
            self.record("sealed_action", character=NAMES[key], actor_id=self.ids[key], intent_ref=ref,
                        output_ref=result["output_ref"], knowledge_cut=context["read_cut"]["knowledge_cut"],
                        context_ref=context["context_bundle_id"])
        commands = [
            {"template_id": "DESTROY_ITEM", "args": {"item_id": self.ids["sword"], "at": point(4)}},
            {"template_id": "RESOURCE_TRANSFER", "args": {"from_account": self.ids["warehouse"], "to_account": self.ids["relief"],
                "resource_id": self.ids["grain"], "quantity": "30", "unit": "bag", "at": point(4)}},
        ]
        outcome_ref = self.scheduler.choose_outcome(self.principal, interaction["interaction_id"], commands=commands,
            process_outcomes=[{"process_id": process["process_id"], "outcome": "FIRED", "world_stage": "COMPLETED", "next_due": None}],
            reasoning_summary="TEST_FIXTURE：合法突发木楔故障；林澈同意毁印顶闸，贺宁守住70袋，苏弦取得30袋。规则校验资源守恒和物品不可复活。",
            rule_evidence_refs=[{"kind": "RULE", "artifact_ref": self.rules_ref}], source_kind="FIXTURE_SELECTED")
        with self.kernel.db.uow(write=False) as repo:
            row = repo.one("simulation_interactions", {"interaction_id": interaction["interaction_id"]})
        args = outcome_command(row["artifact_ref"], row["payload"], outcome_ref, self.artifact(outcome_ref))
        self.joint = self.command("JOINT_INTERACTION", args, "joint-tide-outcome")
        self.record("adjudication", interaction=interaction, intent_refs=intents, outcome_ref=outcome_ref,
                    world_ref=self.wr)
        # The actual world change, not a predetermined report, drives impact.
        with self.kernel.db.uow(write=False) as repo:
            task = repo.one("tasks", {"task_id": self.old_plan_task["task_id"]})
            try:
                validate_task_plan_binding(repo, self.kernel, task)
            except DomainError as error:
                if error.code != "STALE_PLAN":
                    raise
                stale_code = error.code
            else:
                raise AssertionError("Destroyed prerequisite did not fence old future task")
        replacement = deepcopy(self.nodes[3:5])
        for node, goal in zip(replacement, ["承认铜印损毁并保留公账", "改由共同汛务与有据记录建立协作"]):
            node.update(goal=goal, status="VIABLE", hard_preconditions=[])
        replanned = self.planning.replan(self.principal, {"schema_version": "2.1", "prior_plan_ref": self.plan_ref,
            "input_world_ref": self.wr, "replacements": replacement, "additional_nodes": [], "additional_dependencies": [],
            "decision": self.decision("毁印使两项依赖失效；北仓清点及其他场景保持不变", "有据的共同责任", "恢复完好铜印")}, "local-replan")
        self.planning.adopt_plan(self.principal, self.wr, replanned["plan_ref"], self.plan_ref, "adopt-local-replan")
        self.plan_ref = replanned["plan_ref"]
        self.replan = replanned
        self.record("local_replan", stale_error=stale_code, **replanned)
        self.observation = self.command("RECORD_COGNITION", {
            "actor_id": self.ids["protagonist"], "statement": "铜汛印已经毁坏；不能作为完好凭证使用。",
            "record_kind": "OBSERVATION", "epistemic_mode": "OBSERVED", "at": point(5),
            "evidence_refs": [{"kind": "EVENT", "world_ref": self.joint["world_ref"], "event_ref": self.joint["receipt"]["accepted_event_refs"][0]}]}, "observe-loss")
        self.affiliation = self.command("SET_RELATION", {"subject_id": self.ids["friend"], "predicate": "member_of",
            "object_value": entity_value(self.ids["sect"]), "at": point(6)}, "formal-cooperation")
        self.command("SCHEDULE_ADVANCE", {"scope_ids": list(self.ids.values()), "target": point(8)}, "complete-story-coverage")
        # A final rolling plan explicitly adopts the completed fixed world cut.
        final_plan = self.planning.get_plan(self.principal, self.wr["work_id"], self.plan_ref)
        final_plan.update(plan_revision_id=uid(), prior_plan_ref=self.plan_ref, input_world_ref=self.wr)
        final_ref = self.planning.save_plan(self.principal, final_plan, "final-writing-cut-plan")
        self.planning.adopt_plan(self.principal, self.wr, final_ref, self.plan_ref, "adopt-final-writing-cut")
        self.plan_ref, self.nodes = final_ref, final_plan["nodes"]
        self.final_world = self.world_snapshot()
        self.record("writing_world_frozen", world_ref=self.wr, plan_ref=self.plan_ref,
                    state_sha256=fingerprint(self.final_world), world_event_count=len(self.final_world["events"]))

    def world_snapshot(self):
        with self.kernel.db.uow(write=False) as repo:
            return deepcopy(self.kernel.revision(repo, self.wr)["snapshot"])

    def state_check(self, subject, predicate, at, value, *, status="VALUE", actor=None):
        return {"check_id": uid(), "plane": "BELIEF" if actor else "WORLD", "subject_id": self.ids[subject],
                "predicate": predicate, "at": at, "expected_status": status, "expected_value": value,
                "actor_id": self.ids[actor] if actor else None, "known_at": at if actor else None}

    def scenes(self):
        scene_ids = [uid() for _ in TEXTS]
        self.scene_times = [2, 3, 4, 5, 6, 7]
        self.scene_events = [self.gift["receipt"]["accepted_event_refs"],
            self.friend_belief["receipt"]["accepted_event_refs"] + self.travel["receipt"]["accepted_event_refs"],
            self.joint["receipt"]["accepted_event_refs"],
            self.observation["receipt"]["accepted_event_refs"], self.affiliation["receipt"]["accepted_event_refs"],
            self.joint["receipt"]["accepted_event_refs"]]
        # The two initial lines run together. The crossing depends on both;
        # subsequent scenes have an explicit exact-text predecessor handoff.
        self.predecessors = [[], [], [0, 1], [2], [3], [4]]
        slots = [{"scene_id": sid, "story_interval": {"start": point(day), "end": point(day + 1), "bounds": "[)"},
            "reader_cut_ordinal": i, "required_predecessor_scene_ids": [scene_ids[j] for j in self.predecessors[i]]}
            for i, (sid, day) in enumerate(zip(scene_ids, self.scene_times))]
        self.batch_ref = self.release.create_writing_batch(self.principal, {
            "schema_version": "2.2", "world_ref": self.wr, "volume_id": self.volume_id, "plan_ref": self.plan_ref,
            "style_refs": [], "narrative_revision_id": self.narrative_id, "arrangement_version": "tide-order-1", "scene_slots": slots}, "writing-batch")
        self.scene_refs, self.scene_cards = [], []
        for i, slot in enumerate(slots):
            day = self.scene_times[i]
            if i < 2:
                checks = [self.state_check("sword", "possessed_by", point(day, 2), entity_value(self.ids["protagonist"]))]
            else:
                checks = [self.state_check("sword", "lifecycle", point(day, 2), {"kind": "STRING", "value": "DESTROYED"})]
            if i == 1:
                checks = [self.state_check("sword", "possessed_by", point(3, 2), entity_value(self.ids["smith"]), actor="friend")]
                checks.append(self.state_check("rival", "located_in", point(3, 2), entity_value(self.ids["north"])))
                for account, quantity in (("warehouse", "100"), ("relief", "0")):
                    checks.append(self.state_check(account, "resource:" + self.ids["grain"] + ":bag", point(day, 2),
                        {"kind": "RESOURCE_BALANCE", "account_id": self.ids[account], "resource_id": self.ids["grain"], "quantity": quantity, "unit": "bag"}))
            if i >= 2:
                for account, quantity in (("warehouse", "70"), ("relief", "30")):
                    checks.append(self.state_check(account, "resource:" + self.ids["grain"] + ":bag", point(day, 2),
                        {"kind": "RESOURCE_BALANCE", "account_id": self.ids[account], "resource_id": self.ids["grain"], "quantity": quantity, "unit": "bag"}))
            contract = {"schema_version": "2.0", "scene_id": slot["scene_id"], "input_world_ref": self.wr,
                "story_interval": slot["story_interval"], "event_refs": self.scene_events[i],
                "entry_commitments": [], "exit_commitments": [], "pov_actor_id": None, "narrator_mode": "omniscient",
                "narrative_revision_id": self.narrative_id, "arrangement_version": "tide-order-1", "reader_cut_ordinal": i,
                "allowed_invention": ["非持久的感官和修辞；重要消息或物质变化须另经世界接受"],
                "style_refs": [], "dependency_fingerprint": fingerprint({"world": self.wr, "plan": self.plan_ref, "slot": slot})}
            scene = {"schema_version": "2.2", "scene_contract": contract, "plan_ref": self.plan_ref,
                "plan_node_ids": [self.nodes[i]["node_id"]], "batch_ref": self.batch_ref, "prior_scene_ref": None,
                "required_event_refs": self.scene_events[i], "required_predecessor_scene_ids": slot["required_predecessor_scene_ids"],
                "entry_checks": checks, "exit_checks": [], "allowed_disclosures": [],
                "literary_purpose": TITLES[i] + "；人物动机、损失与余数相衔接。"}
            ref = self.narrative.save_scene(self.principal, scene, "scene:" + str(i))
            self.scene_refs.append(ref); self.scene_cards.append(scene)
        self.record("scene_outlines", batch_ref=self.batch_ref, scene_refs=self.scene_refs)

    def writer(self, index, accepted, *, prior_draft_ref=None, parallel=False):
        parents = [accepted[j]["acceptance_ref"] for j in self.predecessors[index]]
        originals = [accepted[j]["draft_ref"] for j in self.predecessors[index]]
        if prior_draft_ref:
            originals.append(prior_draft_ref)
        inputs = [self.plan_ref, self.batch_ref, *parents, *([prior_draft_ref] if prior_draft_ref else [])]
        task = self.prepare("writer", "targeted_revision" if prior_draft_ref else "scene_draft",
            {"scene": index, "revision": prior_draft_ref is not None, "parallel_first_pair": parallel,
             "predecessor_draft_ids": [accepted[j]["draft_ref"]["artifact_revision_id"] for j in self.predecessors[index]],
             "revision_draft_id": prior_draft_ref["artifact_revision_id"] if prior_draft_ref else None},
            inputs=inputs, materials=[self.scene_refs[index], *originals])
        tid, generation = task.task["task_id"], task.task["generation"]
        self.planning.bind_task_for_controller(self.principal, tid, generation, self.plan_ref,
            self.scene_cards[index]["plan_node_ids"], "bind-plan:" + tid)
        self.release.bind_writer_task(task.principal, tid, generation, self.scene_refs[index], parents, "bind-write:" + tid)
        if originals:
            self.narrative.bind_reader_task(self.principal, tid, generation, originals, "writer-originals:" + tid)
        payload, result = self.dispatch(task)
        draft_ref = self.narrative.store_draft(task.principal, tid, generation, self.scene_refs[index], payload["text"],
            "store-draft:" + tid, title=TITLES[index], handoff_refs=parents, prior_draft_ref=prior_draft_ref,
            agent_output_ref=result["output_ref"])
        self.record("draft", scene_index=index, task_id=tid, world_ref=self.wr, draft_ref=draft_ref,
                    prior_draft_ref=prior_draft_ref, handoff_refs=parents, agent_output_ref=result["output_ref"])
        return {"draft_ref": draft_ref, "text": payload["text"], "task_id": tid, "scene_index": index}

    def reconcile(self, draft):
        task = self.prepare("reviewer", "fact", {"purpose": "deterministic exact-span extraction"}, inputs=[draft["draft_ref"]])
        tid, generation = task.task["task_id"], task.task["generation"]
        self.narrative.bind_reader_task(self.principal, tid, generation, [draft["draft_ref"]], "reader:" + tid)
        delivered = self.narrative.read_text(task.principal, tid, generation, draft["draft_ref"])
        index = draft["scene_index"]
        text = delivered["text"]
        # Load actual immutable event effects. The extractor receives no scene
        # checks, golden prose or expected quantities to copy into its result.
        sources = {}
        with self.kernel.db.uow(write=False) as repo:
            for name, accepted_event in {
                "gift": self.gift, "belief": self.friend_belief, "own_belief": self.protagonist_belief,
                "travel": self.travel, "joint": self.joint, "observation": self.observation,
                "membership": self.affiliation,
            }.items():
                event_ref = accepted_event["receipt"]["accepted_event_refs"][0]
                event = repo.one("events", {"work_id": self.wr["work_id"], **event_ref})
                sources[name] = {"event_ref": event_ref, "payload": event["payload"]}
        assertions, extraction_audit = extract_fixture_assertions(text, index, self.ids, sources)
        extraction = self.narrative.submit_extraction(task.principal, tid, generation, draft["draft_ref"],
            delivered["delivery_ref"], assertions, "extract:" + tid)
        reconciled = self.narrative.reconcile(self.principal, draft["draft_ref"], extraction, self.wr, "reconcile:" + tid)
        try:
            accepted = self.narrative.accept_narrative(self.principal, draft["draft_ref"], reconciled["reconciliation_ref"],
                self.wr, "narrative-accept:" + tid, alignment_ref=reconciled["alignment_ref"])
        except DomainError as error:
            self.record("narrative_rejected", scene_index=index, draft_ref=draft["draft_ref"],
                text=text, delivery_ref=delivered["delivery_ref"], extraction_ref=extraction,
                extraction_audit=extraction_audit, assertions=assertions,
                reconciliation=reconciled, error_code=error.code)
            raise
        result = {**draft, "extraction_ref": extraction, "reconciliation_ref": reconciled["reconciliation_ref"],
                  "alignment_ref": reconciled["alignment_ref"], "acceptance_ref": accepted}
        self.record("narrative_accepted", scene_index=index, draft_ref=draft["draft_ref"],
                    extraction_ref=extraction, extraction_audit=extraction_audit,
                    reconciliation=reconciled, acceptance_ref=accepted)
        return result

    def binding(self, accepted, *, literary_refs=()):
        return {"world_ref": self.wr, "plan_ref": self.plan_ref,
            "ordered_draft_refs": [item["draft_ref"] for item in accepted],
            "reconciliation_refs": [item["reconciliation_ref"] for item in accepted],
            "ordered_acceptance_refs": [item["acceptance_ref"] for item in accepted],
            "narrative_revision_id": self.narrative_id, "arrangement_version": "tide-order-1",
            "disclosure_revision_id": self.disclosure_id, "style_refs": [], "literary_refs": list(literary_refs)}

    def reviews(self, binding, *, modes=("fact", "epistemic", "literary", "continuity")):
        refs = []
        all_issues = []
        for mode in modes:
            task = self.prepare("reviewer", mode, {"dimension": mode},
                inputs=[*binding["ordered_draft_refs"], *binding["reconciliation_refs"], *binding["literary_refs"]],
                materials=binding["ordered_draft_refs"])
            tid, generation = task.task["task_id"], task.task["generation"]
            self.narrative.bind_reader_task(self.principal, tid, generation, binding["ordered_draft_refs"], "review-scope:" + tid)
            reads = [self.review.read_text(task.principal, tid, generation, ref, idempotency_key="full:" + ref["artifact_revision_id"])
                     for ref in binding["ordered_draft_refs"]]
            payload, result = self.dispatch(task)
            all_issues.extend(payload["issues"])
            if mode == "fact":
                jobs = [("SCENE", "FACT", [ref]) for ref in binding["ordered_draft_refs"]]
            elif mode == "epistemic":
                jobs = [("CHAPTER", "EPISTEMIC", binding["ordered_draft_refs"])]
            elif mode == "literary":
                jobs = [("VOLUME", "LITERARY", binding["ordered_draft_refs"])]
            else:
                jobs = [("SEAM", "CONTINUITY", [a, b]) for a, b in zip(binding["ordered_draft_refs"], binding["ordered_draft_refs"][1:])]
            for scale, dimension, scope in jobs:
                submission = {"schema_version": "2.1", "task_id": tid, "generation": generation,
                    "context_bundle_id": task.context_bundle_id, "binding": binding, "scope_draft_refs": scope,
                    "scale": scale, "dimension": dimension, "coverage_refs": [x["coverage_ref"] for x in reads],
                    "issues": payload["issues"], "summary": payload["summary"],
                    "counterinterpretations": payload["counterinterpretations"],
                    "outcome": "CHANGES_REQUESTED" if payload["issues"] else "PASS",
                    "idempotency_key": "review:" + uid(), "agent_output_ref": result["output_ref"]}
                refs.append(self.review.submit_review(task.principal, submission))
        self.record("full_text_reviews", binding=binding, review_refs=refs, issues=all_issues)
        return refs

    def edit(self, binding, review_refs, *, issue_decisions=(), approve=True):
        session = self.review.start_editing(self.principal, {"schema_version": "2.1", "binding": binding,
            "max_rounds": 6, "max_issue_decisions": 30, "idempotency_key": "edit-start:" + uid()})
        functions = []
        for i, ref in enumerate(binding["ordered_draft_refs"]):
            draft = self.narrative.load_draft(self.principal, self.wr["work_id"], ref)
            text = self.kernel.objects.read(draft["text_ref"]["sha256"]).decode()
            functions.append({"draft_ref": ref, "function": TITLES[i] + "：保留独立动机与不可逆损失。",
                "choice_evidence_refs": [{"kind": "ARTIFACT", "artifact_ref": draft["text_ref"], "span": span_for(text)}]})
        decisions = []
        for phase in (("STRUCTURE", "SEAM", "LANGUAGE") if approve else ("STRUCTURE",)):
            request = {"schema_version": "2.1", "session_ref": session,
                "expected_prior_decision_ref": decisions[-1] if decisions else None,
                "phase": phase, "after_binding": binding, "review_refs": review_refs,
                "issue_decisions": list(issue_decisions) if phase == "STRUCTURE" else [],
                "scene_functions": functions if phase == "STRUCTURE" else [],
                "considered_options": [
                    {"option_id": uid(), "summary": "保留两线交汇与三十袋余数，删去第一场机械重复", "disposition": "SELECTED",
                     "reason": "具体重复未增加信息，删除不影响事件或人物意图", "benefits": ["更清晰的损失回声"], "costs": ["减少一处停顿"]},
                    {"option_id": uid(), "summary": "合并两条线并让毁印无后果", "disposition": "REJECTED",
                     "reason": "会抹除独立责任及世界已接受的代价", "benefits": ["篇幅更短"], "costs": ["失去人物分歧"]}
                ] if phase == "STRUCTURE" else [],
                "summary": "TEST_FIXTURE：按结构、接缝、语言次序完成精确版本编辑。",
                "idempotency_key": "edit:" + uid()}
            decisions.append(self.review.record_decision(self.principal, request))
        approval_ref = None
        if approve:
            approval_ref = self.review.approve_editorial(self.principal, {"schema_version": "2.1", "binding": binding,
                "review_refs": review_refs, "edit_decision_refs": decisions,
                "unresolved_choices": ["真实模型文学表现与作者全文评价尚未验证"], "idempotency_key": "editorial-approval:" + uid()})
        self.record("volume_editing", session_ref=session, decisions=decisions, approval_ref=approval_ref,
                    issue_decisions=list(issue_decisions))
        return approval_ref

    def literary_memory(self, accepted):
        refs = [item["draft_ref"] for item in accepted]
        task = self.prepare("evidence_maintainer", "literary_memory", {"purpose": "解释性关系记忆"}, inputs=refs)
        tid, generation = task.task["task_id"], task.task["generation"]
        self.narrative.bind_reader_task(self.principal, tid, generation, refs, "memory-scope:" + tid)
        reads = [self.review.read_text(task.principal, tid, generation, ref, idempotency_key="memory-full:" + str(i))
                 for i, ref in enumerate(refs)]
        summary = self.kernel.put_artifact(task.principal, self.wr["work_id"],
            "TEST_FIXTURE文学解释：林澈开始理解贺宁的谨慎；共同灯光并不证明全部信任。铜印与袖口断线是损失的回声。".encode(), "TEXT")
        candidate = {"schema_version": "2.0", "candidate_id": uid(), "task_id": tid, "generation": generation,
            "world_ref": self.wr, "memory_kind": "LITERARY_MEMORY", "plane": "LITERARY", "audience": "WRITER",
            "actor_id": self.ids["protagonist"], "source_refs": [{"kind": "ARTIFACT", "artifact_ref": reads[4]["text_ref"], "span": reads[4]["span"]}],
            "counterevidence_refs": [{"kind": "ARTIFACT", "artifact_ref": reads[5]["text_ref"], "span": reads[5]["span"]}],
            "source_coverage": "COMPLETE", "coverage_note": "六场精确原文已交付；解释不成为世界事实或角色信念。",
            "summary_ref": summary, "method_revision_sha256": fingerprint("tide-gate-fixture-memory-v1"),
            "dependency_fingerprint": "0" * 64, "narrative_revision_id": self.narrative_id,
            "entry_kind": "RELATIONSHIP_ASYMMETRY", "unresolved_items": ["合作后是否形成稳定信任，仍需后续原文与真实读者判断"]}
        candidate["dependency_fingerprint"] = memory_dependency(candidate, refs)
        ref = self.review.save_memory(task.principal, {"schema_version": "2.1", "candidate": candidate,
            "source_draft_refs": refs, "coverage_refs": [x["coverage_ref"] for x in reads], "idempotency_key": "literary-memory"})
        self.record("literary_memory", memory_ref=ref)
        return ref

    def run(self):
        self.initialize(); self.initial_plan(); self.simulate(); self.scenes()
        self.disclosure_id = uid()
        self.adapter.writer_barrier = Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            drafts = [pool.submit(self.writer, index, [], parallel=True) for index in range(2)]
            initial_drafts = [future.result() for future in drafts]
        self.adapter.writer_barrier = None
        accepted = [self.reconcile(draft) for draft in initial_drafts]
        # Fix the first draft before any successor consumes its old text.
        old_binding = self.binding(accepted)
        old_reviews = self.reviews(old_binding, modes=("literary",))
        issue = self.artifact(old_reviews[0])["submission"]["issues"][0]
        decision = {"issue_id": issue["issue_id"], "action": "REVISE", "replacement_draft_refs": [],
                    "reason": "只删一次重复，不变动事件、物品状态或读者获知内容。", "evidence_refs": issue["evidence_refs"]}
        self.edit(old_binding, old_reviews, issue_decisions=[decision], approve=False)
        initial_ref = accepted[0]["draft_ref"]
        accepted[0] = self.reconcile(self.writer(0, accepted, prior_draft_ref=initial_ref))
        for index in range(2, len(TEXTS)):
            readiness = self.release.ready_scenes(self.principal, self.batch_ref,
                                                  [item["acceptance_ref"] for item in accepted])
            if self.scene_cards[index]["scene_contract"]["scene_id"] not in readiness["ready_scene_ids"]:
                raise AssertionError("Successor did not become ready through exact accepted predecessors")
            accepted.append(self.reconcile(self.writer(index, accepted)))
        memory_ref = self.literary_memory(accepted)
        binding = self.binding(accepted, literary_refs=[memory_ref])
        reviews = self.reviews(binding)
        approval_ref = self.edit(binding, reviews)
        request = {"schema_version": "2.2", "world_ref": self.wr, "volume_id": self.volume_id, "plan_ref": self.plan_ref,
            "batch_refs": [self.batch_ref], "ordered_acceptance_refs": binding["ordered_acceptance_refs"],
            "editorial_approval_ref": approval_ref, "narrative_revision_id": self.narrative_id,
            "arrangement_version": binding["arrangement_version"], "disclosure_revision_id": self.disclosure_id,
            "style_refs": [], "literary_refs": [memory_ref], "prior_release_ref": None, "revision_reason": None}
        frozen = self.release.freeze(self.principal, request, "internal-release")
        retry = self.release.freeze(self.principal, request, "internal-release")
        if retry != frozen or self.world_snapshot() != self.final_world:
            raise AssertionError("Narrative/edit/release changed world or duplicated release")
        exported = self.release.export_release(self.principal, self.wr["work_id"], frozen["release_ref"])
        self.record("internal_release", request=request, result=frozen, world_unchanged=True)
        result = {"title": TITLE, "execution_kind": "TEST_FIXTURE", "engineering_status": "PASS",
            "real_model_status": "BLOCKED_ENVIRONMENT", "author_status": "PENDING_AUTHOR",
            "literary_quality_status": "NOT_EVALUATED", "external_delivery": "NOT_REQUESTED",
            "work_id": self.wr["work_id"], "initial_world_ref": self.initial_wr, "world_ref": self.wr,
            "scene_count": len(accepted), "text_codepoints": len(exported["text"]),
            "original_plan_ref": self.original_plan_ref, "replan": self.replan, "final_plan_ref": self.plan_ref,
            "batch_ref": self.batch_ref, "initial_draft_ref": initial_ref, "final_binding": binding,
            "release": frozen, "review_refs": reviews, "literary_memory_ref": memory_ref,
            "model_fixture_call_count": len(self.calls), "real_model_call_count": 0,
            "fixture_cost": {"currency": "USD", "amount": "0", "basis": "DETERMINISTIC_TEST"},
            "real_model_cost": None, "world_events_before_writing": len(self.final_world["events"]),
            "world_events_after_release": len(self.world_snapshot()["events"]),
            "source_status": "Authored deterministic test vectors; not generated by a production model",
            "implementation_sha256": {name: sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                                      for name in ("short_volume.py", "short_volume_fixture.py", "short_volume_extraction.py")},
            "completed_at_utc": datetime.now(UTC).isoformat()}
        if self.output_dir:
            self.export(result, exported["text"], accepted)
        return result

    def export(self, result, text, accepted):
        root = self.output_dir
        assert root is not None
        root.mkdir(parents=True, exist_ok=True)
        def write_json(name, value):
            (root / name).write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
        write_json("run_manifest.json", result)
        write_json("world_receipts.json", self.receipts)
        write_json("initial_plan.json", self.original_plan)
        write_json("final_plan.json", self.planning.get_plan(self.principal, self.wr["work_id"], self.plan_ref))
        write_json("final_world.json", self.final_world)
        write_json("model_call_results.json", self.calls)
        (root / "short_volume.md").write_text("# " + TITLE + "\n\n> TEST_FIXTURE：确定性工程样卷；尚未经过真实模型生产与作者文学验收。\n\n" +
            "\n\n".join("## " + TITLES[i] + "\n\n" + item["text"] for i, item in enumerate(accepted)) + "\n", encoding="utf-8")
        (root / "release.txt").write_text(text, encoding="utf-8")
        (root / "author_feedback.md").write_text(
            "# 作者全文阅读评价记录\n\n状态：PENDING_AUTHOR。以下栏目为空，未填写代表未取得反馈。\n\n"
            "- 对应发布清单 SHA-256：" + result["release"]["manifest"]["manifest_sha256"] + "\n"
            "- 阅读者：\n- 阅读时间：\n- 是否完整阅读六场原文：\n- 人物动机与两线交汇：\n"
            "- 意外是否合理、代价是否保留：\n- 章节衔接、声线与语言：\n- 问题原文及建议：\n"
            "- 最终评价（通过／修改后复核／不通过）：\n", encoding="utf-8")
        # Only controlled fictional data and non-secret audit artifacts are
        # exported. Grants, tokens, runtime credentials and DB URLs are excluded.
        with self.kernel.db.uow(write=False) as repo:
            rows = repo.many("artifacts", {"work_id": self.wr["work_id"]})
            contexts = repo.many("contexts", {"work_id": self.wr["work_id"]})
            events = repo.many("events", {"work_id": self.wr["work_id"]})
        write_json("contexts.json", [{"task_id": row["task_id"], "generation": row["generation"],
            "context_bundle_id": row["context_bundle_id"], "read_cut": row["read_cut"], "payload": row["payload"]} for row in contexts])
        write_json("events.json", [row["payload"] for row in events])
        artifact_dir = root / "artifacts"
        artifact_dir.mkdir(exist_ok=True)
        index = []
        for row in rows:
            if row["kind"] in {"POLICY", "CAPABILITY_GRANT"}:
                continue
            name = row["artifact_revision_id"] + (".txt" if row["payload"] is None else ".json")
            content = self.kernel.objects.read(row["sha256"])
            (artifact_dir / name).write_bytes(content)
            index.append({"kind": row["kind"], "artifact_id": row["artifact_id"],
                "artifact_revision_id": row["artifact_revision_id"], "sha256": row["sha256"], "path": "artifacts/" + name})
        write_json("artifact_index.json", index)
        checksums = {str(path.relative_to(root)): sha256(path.read_bytes()).hexdigest()
                     for path in sorted(root.rglob("*")) if path.is_file() and path.name != "checksums.json"}
        write_json("checksums.json", checksums)


def run_short_volume(kernel, *, output_dir=None):
    """Run the explicitly labelled fixture experiment; never auto-detect keys."""
    return ShortVolumeRunner(kernel, output_dir=output_dir).run()
