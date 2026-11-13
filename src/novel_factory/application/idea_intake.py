"""Enroll an idea in its own editorial preparation work and consume its audited result."""

import json
from uuid import UUID, uuid5

from novel_factory.application.budget_limits import SharedBudgetService
from novel_factory.application.candidate_delivery import CandidateDelivery
from novel_factory.application.kernel import require
from novel_factory.application.model_routing import POLICY_ROUTE, activate, check_idea_models, idea_policy
from novel_factory.application.narrative import NarrativeService
from novel_factory.application.planning import PlanningService
from novel_factory.application.production_pipeline import _artifact
from novel_factory.application.production_profiles import bind_profile, resolve_profile
from novel_factory.application.release import ReleaseService
from novel_factory.application.runtime_tasks import RuntimeTaskService
from novel_factory.application.semantic_dependencies import check_task_dependencies
from novel_factory.application.supervisor import RuntimeSupervisor
from novel_factory.contracts.registry import validate_contract
from novel_factory.domain.canonical import fingerprint


def parse_inspiration(data):
    require(
        isinstance(data, dict)
        and set(data) == {"idea", "length", "genre"}
        and isinstance(data["idea"], str)
        and data["idea"].strip()
        and len(data["idea"]) <= 4000,
        "INVALID_ARGUMENT",
        "Supply one idea, one length preset, and a genre",
    )
    length = data["length"]
    if isinstance(length, dict):
        require(
            set(length) == {"preset", "unit", "min", "max"}
            and length["unit"] in {"han_characters", "chapters"}
            and type(length["min"]) is int
            and type(length["max"]) is int
            and 1 <= length["min"] <= length["max"],
            "INVALID_ARGUMENT",
            "Invalid length bounds",
        )
        length = length["preset"]
    return resolve_profile(length, data["genre"])


def _first_window_limit(profile, inspiration):
    limit = profile["window_chapters"]
    extent = inspiration["length"]
    if isinstance(extent, dict) and extent["unit"] == "chapters":
        return min(limit, extent["max"])
    return limit


def _uid(namespace, name):
    return str(uuid5(UUID(namespace), name))


def _point(tick, phase=0):
    return {"tick": tick, "phase": phase}


def enroll_idea(kernel, gateway, inspiration, allowance, prep_work_id):
    """Trusted local bootstrap. All nested writes use the caller's single transaction.

    The preparation work contains only an editorial intake record. Its grant cannot
    see or write the literary world, which does not exist until delivery.
    """
    profile = parse_inspiration(inspiration)
    route = allowance["adapter_id"]
    review_route = allowance.get("review_adapter_id")
    check_idea_models(gateway.adapters, route, review_route)
    subject = _uid(prep_work_id, "editorial-intake")
    mark = _point(0)
    metadata = {
        "entity_id": subject,
        "name": "创作委托",
        "entity_kind": "KNOWLEDGE",
        "created_at": mark,
    }
    created = kernel.bootstrap_work(
        "NovelFactory 创作委托",
        {"entities": {subject: metadata}},
        frontiers={subject: _point(0, 1)},
        work_id=prep_work_id,
    )
    wr, owner = created["world_ref"], created["principal"]
    model_routes = None
    policy_ref = None
    if review_route is not None:
        model_routes = {"creative": route, "review": review_route}
        activation = activate(kernel, owner, wr, idea_policy(wr, model_routes),
                              gateway.adapters, expected_revision=0)
        policy_ref = activation["policy_ref"]
        route = POLICY_ROUTE
    budget = kernel.tasks.create_budget(
        prep_work_id, allowance["total_amount"], currency=allowance["currency"]
    )
    SharedBudgetService(kernel).set_limit(
        owner, prep_work_id, wr["branch_id"], budget, allowance["total_tokens"]
    )
    bound_gateway = gateway.with_kernel(kernel)
    # Only the new task freezes this structured result type. Existing initial_world
    # tasks (including legacy AgentTextOutput) keep their original dispatch contract.
    bound_gateway.output_contracts[("world_designer", "initial_world")] = (
        "FirstWorldDesign",
        "production_profile",
    )
    assignment = {
        "creative_input": inspiration,
        "first_window_limit": _first_window_limit(profile, inspiration),
        "instructions": "依据灵感自主设计初始身份、地点、世界规则、故事线及首个窗口的章节目标。",
    }
    role = RuntimeTaskService(kernel, bound_gateway).prepare(
        owner,
        wr,
        role_id="world_designer",
        task_kind="initial_world",
        skill_name="prepare-world",
        assignment=json.dumps(assignment, ensure_ascii=False),
        adapter_id=route,
        subject_ids=[subject],
        predicates=["lifecycle"],
        story_time=mark,
        queue_state="READY",
        deadline_seconds=86400,
        max_amount=allowance["reserve_amount"],
        token_budget=allowance["token_reservation"],
        currency=allowance["currency"],
    )
    tid = role.task["task_id"]
    RuntimeSupervisor(kernel, bound_gateway).enroll(
        owner,
        tid,
        manifest_ref=role.manifest_ref,
        knowledge_cut={"plane": "WORLD"},
        context_tokens=20000,
        input_ref=role.agent_input["input_ref"],
        budget_id=budget,
        reserve_amount=allowance["reserve_amount"],
        token_reservation=allowance["token_reservation"],
    )
    receipt = {
        "world_ref": wr,
        "task_id": tid,
        "runtime_ref": role.agent_input["runtime_ref"],
        "input_ref": role.agent_input["input_ref"],
        "budget_id": budget,
        "profile": profile,
        "creative_input": inspiration,
        **({"model_routes": model_routes, "routing_policy_ref": policy_ref} if model_routes else {}),
    }
    with kernel.db.uow() as repo:
        kernel._receipt(repo, owner, wr, "P6_IDEA_ENROLL", "initial", {}, receipt)
    return created, receipt


def intake_receipt(kernel, repo, owner, wr):
    kernel.authorize(repo, owner, wr["work_id"], "ADMIN", wr["branch_id"])
    receipt = kernel._prior(repo, owner, wr["work_id"], "P6_IDEA_ENROLL", "initial", {})
    require(
        receipt and receipt["world_ref"] == wr, "VIEW_DENIED", "Preparation work is unavailable"
    )
    return receipt


def _audited_design_result(kernel, gateway, repo, task, wr):
    check_task_dependencies(repo, task["task_id"], task["generation"])
    job = repo.one("supervisor_jobs", {"task_id": task["task_id"]})
    grant = repo.one("grants", {"grant_id": task["grant_id"]}, lock="share")
    require(
        job
        and grant
        and grant["active"]
        and grant["epoch"] == job["config"]["grant_epoch"]
        and grant["principal_id"] == task["principal_id"]
        and (grant["branch_ids"] == ["*"] or wr["branch_id"] in grant["branch_ids"])
        and repo.one("works", {"work_id": wr["work_id"]})["policy_version"]
        == job["config"]["policy_version"]
        and task["deadline"] > repo.now(),
        "VIEW_DENIED",
        "Design task authority expired",
    )
    evidence, candidate = CandidateDelivery(kernel, gateway).audited_candidate(repo, task)
    call = repo.one(
        "call_attempts",
        {
            "attempt_id": evidence["attempt_id"],
            "task_id": task["task_id"],
            "generation": task["generation"],
        },
        lock="share",
    )
    require(
        call and call["state"] == "SUCCEEDED",
        "CALL_RECONCILIATION_REQUIRED",
        "An unresolved model attempt must be reconciled before project creation",
    )
    return evidence, candidate


def stage_receipt(kernel, repo, owner, wr):
    kernel.authorize(repo, owner, wr["work_id"], "ADMIN", wr["branch_id"])
    return kernel._prior(repo, owner, wr["work_id"], "P6_IDEA_REVIEW", "initial", {})


def _enroll_review(kernel, gateway, repo, owner, wr, intake, task, evidence, design):
    job = repo.one("supervisor_jobs", {"task_id": task["task_id"]})["config"]
    source = kernel.artifact(repo, intake["input_ref"], wr["work_id"], kind="AGENT_INPUT")[
        "payload"
    ]
    budget = repo.one("budgets", {"budget_id": intake["budget_id"], "work_id": wr["work_id"]})
    # The reviewer receives only the original inspiration, audited final design and
    # content identity. Prior role instructions, hidden reasoning and grants stay private.
    assignment = json.dumps(
        {
            "task": "独立核查初始设计与灵感、世界规则、初始身份和首窗目标；把需要修订的具体问题逐项提交。",
            "creative_input": intake["creative_input"],
            "candidate": design,
            "candidate_ref": evidence["output_ref"],
        },
        ensure_ascii=False,
    )
    require(
        len(assignment) <= 32000,
        "CAPACITY_LIMIT",
        "Initial design exceeds the review prompt boundary",
    )
    dispatch = job["dispatch"]
    role = RuntimeTaskService(kernel, gateway).prepare(
        owner,
        wr,
        role_id="reviewer",
        task_kind="fact",
        skill_name="review-consistency",
        assignment=assignment,
        adapter_id=POLICY_ROUTE if "routing_decision" in source else source["adapter_id"],
        subject_ids=[_uid(wr["work_id"], "editorial-intake")],
        predicates=["lifecycle"],
        story_time=_point(0),
        queue_state="READY",
        deadline_seconds=max(1, min(86400, int((task["deadline"] - repo.now()).total_seconds()))),
        lease_seconds=1,
        max_amount=dispatch["reserve_amount"],
        token_budget=dispatch["token_reservation"],
        currency=budget["currency"],
        input_artifacts=[evidence["output_ref"]],
    )
    RuntimeSupervisor(kernel, gateway).enroll(
        owner,
        role.task["task_id"],
        manifest_ref=role.manifest_ref,
        knowledge_cut={"plane": "WORLD"},
        context_tokens=20000,
        input_ref=role.agent_input["input_ref"],
        budget_id=intake["budget_id"],
        reserve_amount=dispatch["reserve_amount"],
        token_reservation=dispatch["token_reservation"],
    )
    receipt = {
        "status": "DESIGN_REVIEW_ENROLLED",
        "task_id": role.task["task_id"],
        "design_task_id": task["task_id"],
        "input_ref": role.agent_input["input_ref"],
        "candidate_ref": evidence["output_ref"],
    }
    kernel._receipt(repo, owner, wr, "P6_IDEA_REVIEW", "initial", {}, receipt)
    return receipt


def materialize_design(kernel, gateway, owner, wr):
    """Apply a completed, current task once inside a caller-owned transaction.

    Returned credentials are only for first delivery and belong in private storage.
    No model response can choose the target work, budget, authority or world version.
    """
    with kernel.db.uow() as repo:
        gateway = gateway.with_kernel(kernel)
        repo.one("works", {"work_id": wr["work_id"]}, lock="update")
        intake = intake_receipt(kernel, repo, owner, wr)
        previous = kernel._prior(repo, owner, wr["work_id"], "P6_IDEA_MATERIALIZE", "initial", {})
        if previous:
            return None, previous
        require(
            kernel.head(repo, wr["work_id"], wr["branch_id"]) == wr,
            "HEAD_CONFLICT",
            "Preparation head changed",
        )
        task = repo.one(
            "tasks",
            {"task_id": intake["task_id"], "work_id": wr["work_id"], "branch_id": wr["branch_id"]},
            lock="update",
        )
        require(
            task and task["state"] == "RESULT_STORED",
            "TASK_NOT_ACCEPTABLE",
            "Design awaits a current result",
        )
        evidence, design = _audited_design_result(kernel, gateway, repo, task, wr)
        validate_contract("FirstWorldDesign", design, "production_profile")
        require(
            len(design["chapter_goals"])
            <= _first_window_limit(intake["profile"], intake["creative_input"])
            and len(set(design["chapter_goals"])) == len(design["chapter_goals"]),
            "INVALID_MODEL_OUTPUT",
            "Design violates the approved first window",
        )
        selected_review = stage_receipt(kernel, repo, owner, wr)
        if not selected_review:
            return None, _enroll_review(
                kernel, gateway, repo, owner, wr, intake, task, evidence, design
            )
        review = repo.one(
            "tasks",
            {
                "task_id": selected_review["task_id"],
                "work_id": wr["work_id"],
                "branch_id": wr["branch_id"],
            },
            lock="update",
        )
        require(
            review
            and review["state"] == "RESULT_STORED"
            and review["role_id"] == "reviewer"
            and review["principal_id"] != task["principal_id"]
            and review["grant_id"] != task["grant_id"]
            and selected_review["candidate_ref"] == evidence["output_ref"],
            "REVIEW_REQUIRED",
            "An independent completed review of this design is required",
        )
        review_evidence, review_payload = _audited_design_result(kernel, gateway, repo, review, wr)
        if review_payload["issues"]:
            return None, {
                "status": "DESIGN_REWORK_REQUIRED",
                "review_task_id": review["task_id"],
                "review_ref": review_evidence["output_ref"],
                "issue_count": len(review_payload["issues"]),
            }
        target_id = _uid(wr["work_id"], "literary-work")
        created, inputs = _build_story(kernel, repo, target_id, design)
        literary_wr = created["world_ref"]
        literary_owner = created["principal"]
        if "model_routes" in intake:
            policy = kernel.artifact(repo, intake["routing_policy_ref"], wr["work_id"],
                                     kind="RUNTIME_ROUTING_POLICY")["payload"]
            activation = activate(kernel, literary_owner, literary_wr,
                                  {**policy, "work_id": literary_wr["work_id"],
                                   "branch_id": literary_wr["branch_id"]},
                                  gateway.adapters, expected_revision=0)
        profile_refs = bind_profile(kernel, repo, literary_owner, literary_wr, intake["profile"])
        from novel_factory.application.production_extent import bind_extent

        extent = bind_extent(
            kernel, repo, literary_owner, literary_wr, intake["creative_input"]["length"]
        )
        receipt = {
            **inputs,
            **profile_refs,
            "length_ref": extent,
            "creative_input": intake["creative_input"],
            "source_world_ref": wr,
            "task_id": task["task_id"],
            "generation": task["generation"],
            "attempt_id": evidence["attempt_id"],
            "context_bundle_id": task["context_bundle_id"],
            "model": evidence["model_configuration"]["model"],
            "result_ref": evidence["output_ref"],
            "execution_kind": evidence["execution_kind"],
            "source_input_ref": intake["input_ref"],
            **({"model_routes": intake["model_routes"], "routing_policy_ref": activation["policy_ref"]}
               if "model_routes" in intake else {}),
            "profile": intake["profile"],
            "status": "INITIAL_PLAN_MATERIALIZED",
        }
        receipt["design_review"] = {
            "task_id": review["task_id"],
            "generation": review["generation"],
            "attempt_id": review_evidence["attempt_id"],
            "context_bundle_id": review["context_bundle_id"],
            "result_ref": review_evidence["output_ref"],
            "model": review_evidence["model_configuration"]["model"],
            "execution_kind": review_evidence["execution_kind"],
        }
        kernel.tasks.accept_in_transaction(repo.conn, review["task_id"], review["generation"])
        kernel.tasks.accept_in_transaction(repo.conn, task["task_id"], task["generation"])
        kernel._receipt(repo, owner, wr, "P6_IDEA_MATERIALIZE", "initial", {}, receipt)
        kernel._receipt(
            repo,
            created["principal"],
            created["world_ref"],
            "P6_IDEA_PROVENANCE",
            "initial",
            {},
            receipt,
        )
        return created, receipt


def _build_story(kernel, repo, work, design):
    """Translate literary decisions into immutable genesis, adopted plan and authorized scene inputs."""
    actor, place = [_uid(work, key) for key in ("protagonist", "place")]
    first = _point(1)
    facts = [(actor, "CHARACTER", design["protagonist"]), (place, "REGION", design["place"])]
    facts.extend(
        (_uid(work, f"rule:{i}"), "KNOWLEDGE", rule) for i, rule in enumerate(design["world_rules"])
    )
    entities = {
        eid: {"entity_id": eid, "entity_kind": kind, "name": name, "created_at": first}
        for eid, kind, name in facts
    }
    relation = {
        "subject_id": actor,
        "predicate": "located_in",
        "object_value": {"kind": "ENTITY", "entity_id": place},
        "valid_time": {"start": first, "end": None, "bounds": "[)"},
    }
    created = kernel.bootstrap_work(
        design["title"],
        {"entities": entities, "relations": [relation]},
        frontiers={eid: _point(1, 1) for eid in entities},
        work_id=work,
    )
    owner, wr = created["principal"], created["world_ref"]
    design_ref = _artifact(kernel, repo, work, owner, "INITIAL_STORY_DESIGN", design)
    volume, refs = create_planned_window(
        kernel,
        owner,
        wr,
        _uid(work, "volume:1"),
        actor,
        place,
        design_ref,
        design["storyline"],
        design["chapter_goals"],
        design["world_rules"],
    )
    return created, {
        "world_ref": wr,
        "series_id": _uid(work, "series"),
        **refs,
        "design_ref": design_ref,
        "volumes": [volume],
        "story_time": _point(len(design["chapter_goals"]), 1),
        "subject_ids": list(entities),
        "predicates": ["located_in", "lifecycle"],
    }


def create_planned_window(
    kernel, owner, wr, volume, actor, place, design_ref, brief, goals, rules, *, start_after=0
):
    plans = PlanningService(kernel)
    contract = {
        "schema_version": "2.0",
        "volume_id": volume,
        "contract_revision_id": _uid(volume, "contract:1"),
        "input_world_ref": wr,
        "creative_brief": brief,
        "focus_subject_ids": [actor, place],
        "narrative_intents": [brief, *rules],
        "protected_commitments": [],
        "open_choices": ["人物的行动及后果由正式演绎决定。"],
        "completion_or_stop_conditions": ["完成已登记写作窗口"],
        "budget": {"max_semantic_calls": 2 * len(goals) + 1, "max_candidate_branches": 1},
        "upstream_literary_refs": [],
        "dependency_fingerprint": fingerprint([wr, design_ref]),
    }
    contract_ref = plans.save_volume_contract(owner, contract, "intake-volume:" + volume)
    nodes = [
        {
            "node_id": _uid(volume, f"node:{i}"),
            "goal": goal,
            "status": "VIABLE",
            "target_time": {"kind": "EXACT", "at": _point(i + 1 + start_after)},
            "subject_ids": [actor],
            "hard_preconditions": [],
            "risk_notes": [],
            "failure_exit": "局部重规划",
            "fulfillment_event_refs": [],
        }
        for i, goal in enumerate(goals)
    ]
    plan = {
        "schema_version": "2.1",
        "plan_id": _uid(volume, "plan"),
        "plan_revision_id": _uid(volume, "plan:1"),
        "volume_id": volume,
        "input_world_ref": wr,
        "volume_contract_ref": contract_ref,
        "prior_plan_ref": None,
        "nodes": nodes,
        "published_prefix_node_ids": [],
        "dependencies": [],
        "decision": {
            "reason": "采用原生设计任务返回的首个写作窗口。",
            "considered_options": [
                {
                    "option_id": _uid(volume, "selection"),
                    "summary": brief,
                    "disposition": "SELECTED",
                    "reason": "当前写作委托的模型规划。",
                }
            ],
        },
    }
    plan_ref = plans.save_plan(owner, plan, "intake-outline:" + volume)
    plans.adopt_plan(owner, wr, plan_ref, None, "intake-adopt-outline:" + volume)
    narrative_id = _uid(volume, "narrative:1")
    slots = []
    for i in range(len(nodes)):
        slots.append(
            {
                "scene_id": _uid(volume, f"scene:{i}"),
                "reader_cut_ordinal": i,
                "story_interval": {
                    "start": _point(i + 1 + start_after),
                    "end": _point(i + 1 + start_after, 1),
                    "bounds": "[)",
                },
                "required_predecessor_scene_ids": [slots[-1]["scene_id"]] if slots else [],
            }
        )
    binding = {
        "world_ref": wr,
        "volume_id": volume,
        "plan_ref": plan_ref,
        "style_refs": [],
        "narrative_revision_id": narrative_id,
        "arrangement_version": "idea-v1",
    }
    batch = ReleaseService(kernel).create_writing_batch(
        owner, {"schema_version": "2.2", **binding, "scene_slots": slots}, "intake-batch:" + volume
    )
    chapters = []
    for node, slot in zip(nodes, slots, strict=True):
        card = {
            "schema_version": "2.0",
            "scene_id": slot["scene_id"],
            "input_world_ref": wr,
            "story_interval": slot["story_interval"],
            "event_refs": [],
            "entry_commitments": [],
            "exit_commitments": [],
            "pov_actor_id": actor,
            "narrator_mode": "limited",
            "narrative_revision_id": narrative_id,
            "arrangement_version": "idea-v1",
            "reader_cut_ordinal": slot["reader_cut_ordinal"],
            "allowed_invention": ["不改变世界事实的感官细节"],
            "style_refs": [],
            "dependency_fingerprint": fingerprint([wr, plan_ref, slot]),
        }
        scene = NarrativeService(kernel).save_scene(
            owner,
            {
                "schema_version": "2.2",
                "scene_contract": card,
                "plan_ref": plan_ref,
                "plan_node_ids": [node["node_id"]],
                "batch_ref": batch,
                "prior_scene_ref": None,
                "required_event_refs": [],
                "required_predecessor_scene_ids": slot["required_predecessor_scene_ids"],
                "entry_checks": [],
                "exit_checks": [],
                "allowed_disclosures": [],
                "literary_purpose": node["goal"],
            },
            "intake-scene:" + slot["scene_id"],
        )
        chapters.append(
            {
                "chapter_id": _uid(volume, "chapter:" + slot["scene_id"]),
                "scenes": [{"scene_ref": scene, "handoff_refs": []}],
            }
        )
    return {"volume_id": volume, "chapters": chapters}, {
        "plan_ref": plan_ref,
        "batch_ref": batch,
        "contract_ref": contract_ref,
    }
