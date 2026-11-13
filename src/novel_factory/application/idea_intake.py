"""Enroll an idea in its own editorial preparation work and consume its audited result."""

import json
from uuid import UUID, uuid5

from novel_factory.application.budget_limits import SharedBudgetService
from novel_factory.application.candidate_delivery import CandidateDelivery
from novel_factory.application.idea_revision import derive_revision_patch, full_design_contract
from novel_factory.application.idea_window import frozen_window_minimum, initial_window_minimum, window_diagnostics
from novel_factory.application.idea_ending import confirm_ending, derive_ending_plan
from novel_factory.application.idea_repair import UnparsedDesign, initial_or_structural_repair, normalized_design, returned_design
from novel_factory.application.kernel import require
from novel_factory.application.model_routing import POLICY_ROUTE, activate, check_idea_models, idea_policy
from novel_factory.application.narrative import NarrativeService
from novel_factory.application.planning import PlanningService
from novel_factory.application.production_pipeline import _artifact
from novel_factory.application.production_profiles import bind_profile, resolve_profile
from novel_factory.application.release import ReleaseService
from novel_factory.application.quality_memory import PAUSED, plan_repair, reflect_repair, verify_method
from novel_factory.application.runtime_tasks import RuntimeTaskService
from novel_factory.application.semantic_dependencies import check_task_dependencies
from novel_factory.application.supervisor import RuntimeSupervisor
from novel_factory.contracts.registry import validate_contract
from novel_factory.domain.canonical import fingerprint


def parse_inspiration(data, production_mode=None):
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
        from .production_extent import validate_length_bounds
        validate_length_bounds(length)
        length = length["preset"]
    return resolve_profile(length, data["genre"], {"production_mode": production_mode} if production_mode else {})


def _first_window_limit(profile, inspiration):
    limit = profile["window_chapters"]
    extent = inspiration["length"]
    if isinstance(extent, dict) and extent["unit"] == "chapters":
        return min(limit, extent.get("max", limit))
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
    profile = parse_inspiration(inspiration, allowance.get("production_mode"))
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
    # Preparation and literary production are separate works. Bind only the
    # public genre/method selection here, before the first task is frozen.
    with kernel.db.uow() as repo:
        bind_profile(kernel, repo, owner, wr, profile)
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
    design_contract = full_design_contract(profile, model_routes)
    bound_gateway.output_contracts[("world_designer", "initial_world")] = (design_contract, "production_profile")
    maximum = _first_window_limit(profile, inspiration)
    minimum = initial_window_minimum(profile, inspiration["length"], maximum)
    assignment = {
        "creative_input": inspiration,
        "first_window_limit": maximum,
        "window_constraints_version": 1,
        "instructions": "依据灵感自主设计初始身份、地点、世界规则、故事线及首个窗口的章节目标。",
    }
    if minimum > 1:
        assignment.update(window_constraints_version=2, first_window_minimum=minimum,
                          planning_scope="本次章节目标覆盖全书，每章对应一个正文场景，在授权总字数内安排完整的开端、发展和收束。")
    if model_routes and profile["preset"] == "short":
        assignment["extent_instruction"] = "章节目标覆盖整部短篇的开场、中段、转折与结局，在给定篇幅内收束核心冲突。"
    if allowance.get("review_adapter_id"):
        assignment["opening_requirements"] = {
            "instructions": "为第一场设计两个有独立认知的人物和两个地点。opening 中各人的知识只写其已知身份、所见及动机。situation 是尚未确定后果的开场条件。",
            "environment": "另填 environment.moving_entity，指独立于人物决定从初始地点抵达另一地点的实物或环境过程。" if profile["environment_processes"] else "可省略 environment。",
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


def _check_design_authority(repo, task, wr):
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


def _audited_design_result(kernel, gateway, repo, task, wr, *, design_revision_read=False, ending_plan_read=False):
    _check_design_authority(repo, task, wr)
    return CandidateDelivery(kernel, gateway).audited_candidate(repo, task, production_accounting=True,
                                                              design_revision_read=design_revision_read, ending_plan_read=ending_plan_read)


def design_round(kernel, repo, owner, wr, intake):
    """Recover the current designer and its review from immutable round receipts."""
    kernel.authorize(repo, owner, wr["work_id"], "ADMIN", wr["branch_id"])
    designer = intake
    round_index = 0
    structure_count = 0
    editorial_count = 0
    ending_count = 0
    while True:
        key = "initial" if round_index == 0 else str(round_index)
        review = kernel._prior(repo, owner, wr["work_id"], "P6_IDEA_REVIEW", key, {})
        next_design = kernel._prior(repo, owner, wr["work_id"], "P6_IDEA_REVISE", str(round_index + 1), {})
        if next_design is None:
            if review and intake["profile"].get("production_mode") == "quality":
                review = latest_intake_review(repo, wr, review)
            return round_index, {**designer, "structure_count": structure_count, "editorial_count": editorial_count,
                                 "ending_count": ending_count}, review
        if next_design.get("revision_purpose") == "SHORT_ENDING":
            ending_count += 1
        elif next_design.get("review_ref"):
            editorial_count += int(next_design.get("revision_purpose") != "SHORT_ENDING_FIX")
        else:
            structure_count += 1
        designer = next_design
        round_index += 1


def latest_intake_review(repo, wr, review):
    while True:
        next_review = repo.one("receipts", {"work_id": wr["work_id"], "branch_id": wr["branch_id"],
                               "command_type": "P6_IDEA_REVIEW_REPAIR", "idempotency_key": review["task_id"]})
        if not next_review:
            return review
        review = next_review["payload"]


def editor_review_task(kernel, repo, owner, wr, index):
    receipt = kernel._prior(repo, owner, wr["work_id"], "P6_IDEA_REVIEW", str(index) if index else "initial", {})
    return latest_intake_review(repo, wr, receipt)["task_id"]


def _enroll_design_step(kernel, gateway, repo, owner, wr, intake, task, evidence,
                        design, round_index, *, revision=None, structure_repair=False, ending_plan=False, editor_decision=None, diagnostics=None, failed_review=None):
    job = repo.one("supervisor_jobs", {"task_id": task["task_id"]})["config"]
    source = kernel.artifact(repo, job["dispatch"]["input_ref"], wr["work_id"], kind="AGENT_INPUT")["payload"]
    budget = repo.one("budgets", {"budget_id": intake["budget_id"], "work_id": wr["work_id"]})
    content = {"creative_input": intake["creative_input"], "candidate": design,
               "candidate_ref": evidence["output_ref"]}
    inputs = [evidence["output_ref"]]
    syntax_source = isinstance(design, UnparsedDesign)
    is_revision = bool(revision or structure_repair or diagnostics or syntax_source)
    if is_revision:
        role_id, task_kind, skill = "world_designer", "initial_world", "prepare-world"
        contract = source["output_contract"]
        gateway.output_contracts[(role_id, task_kind)] = (contract["name"], contract["version"])
        content["first_window_limit"] = _first_window_limit(intake["profile"], intake["creative_input"])
        original = kernel.artifact(repo, intake["input_ref"], wr["work_id"], kind="AGENT_INPUT")["payload"]
        frozen = json.loads(original["assignment"])
        minimum = frozen_window_minimum(frozen, content["first_window_limit"])
        if minimum > 1:
            content.update(window_constraints_version=2, first_window_minimum=minimum,
                           planning_scope=frozen["planning_scope"])
        else:
            content["window_constraints_version"] = 1
        if syntax_source:
            del content["candidate"]
            content.update(task="上次完整返回见 raw_response_text。请按原契约重新输出合法 JSON，"
                                "仅校正格式，保留原有创作内容。使用本次外层任务身份。",
                           raw_response_text=design.text, validation_errors=[design.diagnostic],
                           source_attempt_id=evidence["attempt_id"], source_audit_ref=evidence["audit_ref"])
            inputs.append(evidence["audit_ref"])
        elif diagnostics:
            content.update(task="根据下列 JSON Schema 校验诊断修正指定字段，返回原契约要求的完整对象。"
                                "其余字段逐字保留。数组内容由你决定。",
                           validation_errors=diagnostics, source_attempt_id=evidence["attempt_id"])
        elif structure_repair:
            content["task"] = ("修复已返回方案的输出结构，保留其中已提出的人物、地点、世界规则和章节构想。"
                               "补齐 opening：counterpart、other_place、双方各自的知识和 situation；"
                               "按当前契约提供 environment。返回规定的外层标识和完整修复后的 payload。")
            inputs.append(evidence["audit_ref"])
            gateway.output_contracts[(role_id, task_kind)] = (
                "EnvironmentalOpeningWorldDesign" if intake["profile"]["environment_processes"] else "OpeningWorldDesign",
                "production_profile",
            )
        else:
            findings, review_ref = revision
            gateway.output_contracts[(role_id, task_kind)] = (
                full_design_contract(intake["profile"], intake.get("model_routes")), "production_profile"
            )
            content.update(task="依据给定审校意见定向修改先前的设计，保留其余设定并返回完整设计。",
                           review_ref=review_ref, issues=[{key: issue[key] for key in
                           ("category", "severity", "issue_kind", "disposition", "description", "suggested_change")} for issue in findings["issues"]])
            if ending_plan:
                gateway.output_contracts[(role_id, task_kind)] = ("ShortEndingPlan", "production_profile")
                content["unresolved_questions"] = findings.get("unresolved_questions", [])
                content.update(task="根据独立审核指出的成篇缺口，在原有篇幅和章节数量内定向改写现有章节目标、故事线。"
                                    "保留人物、开场事实和世界规则。最后目标必须落实一个由人物行动产生的最终后果，"
                                    "让核心冲突得到回应。只返回 storyline、与原章数相同的 chapter_goals；开场需改时才给 opening。",
                               short_arc=findings["short_arc"])
            if editor_decision:
                gateway.output_contracts[(role_id, task_kind)] = ("ShortEndingFix", "production_profile")
                content.update(task="只修改以下开放阻断问题关联的章节或开场字段。chapter 从 1 开始，replacement 是"
                                   "该章完整目标或该开场字段的新文字，每处用 issue_ids 标识所修问题。保持其余各段原文。",
                               editor_decision=editor_decision, ending_plan_source=evidence["ending_plan"]["source"],
                               issues=[i for i in findings["issues"] if i["issue_id"] in editor_decision["issue_ids"]])
                inputs.append(editor_decision["decision_ref"])
            inputs.append(review_ref)
    else:
        role_id, task_kind, skill = "reviewer", "fact", "review-consistency"
        if intake.get("model_routes") and intake["profile"]["preset"] == "short":
            gateway.output_contracts[(role_id, task_kind)] = ("ShortDesignReview", "production_profile")
            content["short_arc_review"] = (
                "逐项填写 short_arc，引用 chapter_goals 实际覆盖的开场、中段、转折和结局；缺失的一项明确写缺失。"
                "若最后一章仍仅出现事端、疑虑或未来行动的打算，核心选择或结果留待续篇，选 ENDING_PLAN_REQUIRED。"
                "有可辨认的结局（含开放式结局）且在该章已经落实时，选 COMPLETE。"
            )
        if "ending_plan" in evidence:
            gateway.output_contracts[(role_id, task_kind)] = ("ShortEndingPlanReview", "production_profile")
            content["ending_plan_source"] = evidence["ending_plan"]["source"]
            content["ending_plan_instruction"] = (
                "对照收到的原始阶段、分组后的各章目标和原有开场，只有判断每一章均可成立、全篇已闭合时"
                "勾选 opening_and_chapters_confirmed。candidate_ref 逐字复制本次候选引用。answers 按源中的"
                " unresolved_questions 顺序，逐条原样复制 question 并填写实质答复，不能把问题留到以后。"
            )
            if "issue_ids" in content["ending_plan_source"]:
                gateway.output_contracts[(role_id, task_kind)] = ("ShortEndingFixReview", "production_profile")
                prior_review = kernel.artifact(repo, content["ending_plan_source"]["review_ref"], wr["work_id"])["payload"]
                content["prior_blockers"] = [issue for issue in prior_review["issues"]
                                             if issue["issue_id"] in content["ending_plan_source"]["issue_ids"]]
                content["resolution_instruction"] = "按 issue_ids 顺序逐一核对原问题在新版本是否已实质解决并写明理由；未解决填 OPEN。"
        content["task"] = ("独立核查当前设计与灵感、世界规则、人物初始所知和首窗目标是否相容。"
                           "阻止成书的矛盾列为 BLOCKER，普通改善建议和待讨论问题分别用 SUGGESTION、QUESTION。")
    if failed_review:
        content["repair_strategy"] = failed_review["plan"]["method"]
        content["validation_errors"] = [failed_review["diagnostic"]]
    repair_plan = None
    if is_revision and intake["profile"].get("production_mode") == "quality":
        family = "schema" if syntax_source or structure_repair or diagnostics else "editorial"
        if syntax_source:
            observed = {key: value for key, value in design.diagnostic.items() if key not in {"offset", "line", "column"}}
            if design.withhold_response or len(design.text) > 16000:
                content.pop("raw_response_text")
                content["task"] = "上次返回未通过契约校验。请从授权灵感及本次 Schema 重新构造完整结果，严格使用本次身份。"
        elif revision:
            report = revision[0]
            observed = sorted(i["category"] + "/" + i["issue_kind"] + "/" + i["severity"]
                              for i in report["issues"] if i["disposition"] in {"OPEN", "ACCEPT"})
            if ending_plan:
                observed.append("ENDING_PLAN_REQUIRED")
        else:
            observed = diagnostics or ["opening"]
        history = [r["payload"]["repair_plan"] for r in repo.many("receipts", {
            "work_id": wr["work_id"], "branch_id": wr["branch_id"], "command_type": "P6_IDEA_REVISE"})
            if "repair_plan" in r["payload"]]
        attempt = evidence.get("attempt_id")
        if revision:
            review_task = repo.one("tasks", {"task_id": editor_review_task(kernel, repo, owner, wr, round_index - 1)})
            reviewed = repo.one("call_attempts", {"task_id": review_task["task_id"], "generation": review_task["generation"],
                                                  "call_key": "supervised-candidate"})
            attempt = reviewed["attempt_id"]
            failed_task = reviewed["task_id"]
        else:
            failed_task = task["task_id"]
        repair_plan = reflect_repair(kernel, repo, owner, wr, failed_task, attempt,
                                     revision[1] if revision else evidence["output_ref"],
                                     plan_repair(family, observed, history), "DESIGN_REVISION")
        if repair_plan["decision"] == PAUSED:
            return {"status": "DESIGN_REWORK_REQUIRED", "reason": PAUSED, "repair_plan": repair_plan,
                    "source_attempt_id": attempt}
        content["repair_strategy"] = repair_plan["method"]
    if intake["profile"]["preset"] == "short" and not diagnostics and not syntax_source:
        content["extent_instruction"] = ("在给定篇幅和章节数内完成整部短篇。核对开场条件、中段发展、转折和结局"
                                         "都由这些章节目标承载，并给核心冲突安排可辨认的收束。")
    assignment = json.dumps(content, ensure_ascii=False)
    if syntax_source and len(assignment) > 32000:
        return {"status": "DESIGN_SCHEMA_REWORK_REQUIRED", "reason": "CAPACITY_LIMIT",
                "source_attempt_id": evidence["attempt_id"], "response_ref": evidence["output_ref"],
                "validation_errors": [design.diagnostic]}
    require(len(assignment) <= 32000, "CAPACITY_LIMIT", "Design handoff exceeds the role prompt boundary")
    dispatch = job["dispatch"]
    role = RuntimeTaskService(kernel, gateway).prepare(
        owner,
        wr,
        role_id=role_id,
        task_kind=task_kind,
        skill_name=skill,
        knowledge_reread_reason=("known_error" if diagnostics or syntax_source else
                                 "review_failure" if revision else None),
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
        input_artifacts=inputs,
    )
    if diagnostics or syntax_source or failed_review:
        if failed_review:
            source = kernel.artifact(repo, failed_review["record"]["input_ref"], wr["work_id"], kind="AGENT_INPUT")["payload"]
        frozen = kernel.artifact(repo, role.agent_input["input_ref"], wr["work_id"], kind="AGENT_INPUT")["payload"]
        require(frozen["adapter_id"] == source["adapter_id"]
                and frozen["model_config_sha256"] == source["model_config_sha256"]
                and frozen["output_contract"] == source["output_contract"], "TASK_REBIND_REQUIRED",
                "Structural correction requires the source task's model configuration and output contract")
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
        "status": "DESIGN_REVISION_ENROLLED" if is_revision else "DESIGN_REVIEW_ENROLLED",
        "round": round_index,
        **({"repair_plan": repair_plan} if repair_plan else {}),
        **({"review_ref": revision[1]} if revision else {}),
        **({"revision_purpose": "JSON_SYNTAX_CORRECTION", "validation_errors": [design.diagnostic],
            "source_attempt_id": evidence["attempt_id"], "source_audit_ref": evidence["audit_ref"]} if syntax_source else {}),
        **({"revision_purpose": "WINDOW_SCHEMA_CORRECTION", "validation_errors": diagnostics,
            "source_attempt_id": evidence["attempt_id"]} if diagnostics else {}),
        **({"revision_purpose": "SHORT_ENDING"} if ending_plan else {}),
        **({"revision_purpose": "SHORT_ENDING_FIX", "editor_decision": editor_decision} if editor_decision else {}),
        "task_id": role.task["task_id"],
        "design_task_id": task["task_id"],
        "input_ref": role.agent_input["input_ref"],
        "candidate_ref": evidence["output_ref"],
    }
    if failed_review:
        before = failed_review["record"]
        receipt.update(previous_review_task_id=before["task_id"],
                       repair_plans=[*before.get("repair_plans", []), failed_review["plan"]],
                       validation_errors=[failed_review["diagnostic"]])
        kernel._receipt(repo, owner, wr, "P6_IDEA_REVIEW_REPAIR", before["task_id"], {}, receipt)
        return receipt
    kernel._receipt(repo, owner, wr, "P6_IDEA_REVISE" if is_revision else "P6_IDEA_REVIEW",
                    str(round_index) if round_index else "initial", {}, receipt)
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
        quality = intake["profile"].get("production_mode") == "quality"
        round_index, designer, selected_review = design_round(kernel, repo, owner, wr, intake)
        task = repo.one(
            "tasks",
            {"task_id": designer["task_id"], "work_id": wr["work_id"], "branch_id": wr["branch_id"]},
            lock="update",
        )
        if task and task["state"] == "FAILED" and intake.get("model_routes"):
            _check_design_authority(repo, task, wr)
            envelope, audit, audit_ref = returned_design(
                kernel, repo, gateway, task, wr, designer,
                allow_malformed=initial_or_structural_repair(round_index, designer),
                allow_contract=quality and initial_or_structural_repair(round_index, designer),
            )
            if isinstance(envelope, UnparsedDesign):
                if selected_review or (not quality and designer["structure_count"] >= intake["profile"]["max_reworks"]):
                    return None, {"status": "DESIGN_SCHEMA_REWORK_REQUIRED", "reason": "JSON_SYNTAX_REPAIR_EXHAUSTED",
                                  "source_attempt_id": audit["attempt_id"], "response_ref": audit["response_ref"],
                                  "validation_errors": [envelope.diagnostic]}
                return None, _enroll_design_step(kernel, gateway, repo, owner, wr, intake, task,
                                                 {**audit, "output_ref": audit["response_ref"], "audit_ref": audit_ref},
                                                 envelope, round_index + 1)
            if designer.get("revision_purpose") in {"SHORT_ENDING", "SHORT_ENDING_FIX"}:
                normalized = derive_ending_plan(kernel, repo, owner, wr, designer, envelope, audit, audit_ref)
            else:
                normalized = normalized_design(kernel, repo, owner, task, wr, designer, envelope, audit, audit_ref)
            if normalized is None:
                design = validate_contract("FirstWorldDesign", envelope.get("payload"), "production_profile")
                require(selected_review is None and (quality or designer["structure_count"] < intake["profile"]["max_reworks"]),
                        "REVIEW_REQUIRED", "The initial design has exhausted its authorized revisions")
                return None, _enroll_design_step(
                    kernel, gateway, repo, owner, wr, intake, task,
                    {"output_ref": audit["response_ref"], "audit_ref": audit_ref, "attempt_id": audit["attempt_id"]}, design, round_index + 1,
                    structure_repair=True,
                )
            evidence, design = normalized
        else:
            require(task and task["state"] == "RESULT_STORED", "TASK_NOT_ACCEPTABLE", "Design awaits a current result")
            ending_task = designer.get("revision_purpose") in {"SHORT_ENDING", "SHORT_ENDING_FIX"}
            evidence, design = _audited_design_result(kernel, gateway, repo, task, wr, ending_plan_read=ending_task)
            if (not ending_task and designer.get("review_ref") and not designer.get("revision_purpose")
                    and kernel.artifact(repo, designer["input_ref"], wr["work_id"], kind="AGENT_INPUT")["payload"]["output_contract"]
                        == {"name": "ShortEndingPlan", "version": "production_profile"}):
                evidence, design = derive_revision_patch(kernel, repo, owner, wr, designer, evidence, design, intake)
            if ending_task:
                audit_row = gateway._read_audit(repo, evidence["attempt_id"])
                audit_ref = {k: audit_row[k] for k in ("artifact_id", "artifact_revision_id", "sha256")}
                raw = kernel.artifact(repo, evidence["response_ref"], wr["work_id"], kind="AGENT_RESPONSE")
                evidence, design = derive_ending_plan(kernel, repo, owner, wr, designer,
                                                       json.loads(raw["payload"]["text"]), evidence, audit_ref)
        validate_contract("FirstWorldDesign", design, "production_profile")
        limit = _first_window_limit(intake["profile"], intake["creative_input"])
        if designer.get("revision_purpose") == "WINDOW_SCHEMA_CORRECTION":
            prior = kernel.artifact(repo, designer["candidate_ref"], wr["work_id"])["payload"]
            require({k: v for k, v in design.items() if k != "chapter_goals"} ==
                    {k: v for k, v in prior.items() if k != "chapter_goals"}, "INVALID_MODEL_OUTPUT",
                    "Window format correction changed a field outside the diagnostic")
        bound_input = kernel.artifact(repo, intake["input_ref"], wr["work_id"], kind="AGENT_INPUT")["payload"]
        minimum = frozen_window_minimum(json.loads(bound_input["assignment"]), limit)
        diagnostics = window_diagnostics(design["chapter_goals"], limit, minimum)
        if diagnostics:
            if selected_review or (not quality and designer["structure_count"] >= intake["profile"]["max_reworks"]):
                return None, {"status": "DESIGN_SCHEMA_REWORK_REQUIRED", "validation_errors": diagnostics,
                              "source_attempt_id": evidence["attempt_id"], "candidate_ref": evidence["output_ref"]}
            return None, _enroll_design_step(kernel, gateway, repo, owner, wr, intake, task, evidence, design,
                                             round_index + 1, diagnostics=diagnostics)
        if intake.get("model_routes"):
            require(
                design.get("opening") and (
                    not intake["profile"]["environment_processes"] or design["opening"].get("environment")
                ),
                "INVALID_MODEL_OUTPUT",
                "The opening must identify both characters and the profile's environmental process",
            )
        if not selected_review:
            return None, _enroll_design_step(
                kernel, gateway, repo, owner, wr, intake, task, evidence, design, round_index
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
        if review and review["state"] == "FAILED" and quality:
            _check_design_authority(repo, review, wr)
            failed, proof, audit_ref = returned_design(kernel, repo, gateway, review, wr, selected_review,
                                                       allow_malformed=True, allow_contract=True)
            require(isinstance(failed, UnparsedDesign), "INVALID_EVIDENCE", "Review failure needs manual diagnosis")
            findings = {k: v for k, v in failed.diagnostic.items() if k not in {"offset", "column", "line"}}
            plan = reflect_repair(kernel, repo, owner, wr, review["task_id"], proof["attempt_id"], audit_ref,
                                  plan_repair("schema", findings, selected_review.get("repair_plans", [])), "REVIEW_SCHEMA")
            if plan["decision"] == PAUSED:
                return None, {"status": "DESIGN_SCHEMA_REWORK_REQUIRED", "reason": PAUSED, "repair_plan": plan}
            return None, _enroll_design_step(kernel, gateway, repo, owner, wr, intake, task, evidence, design,
                                             round_index, failed_review={"record": selected_review, "plan": plan,
                                             "diagnostic": failed.diagnostic})
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
        review_evidence, review_payload = _audited_design_result(
            kernel, gateway, repo, review, wr, design_revision_read=bool(intake.get("model_routes"))
        )
        arc = review_payload.get("short_arc")
        if arc and arc["decision"] == "ENDING_PLAN_REQUIRED":
            if designer["ending_count"] == 0 or quality:
                return None, _enroll_design_step(
                    kernel, gateway, repo, owner, wr, intake, task, evidence, design, round_index + 1,
                    revision=({**review_payload, "unresolved_questions": review_evidence.get("revision_questions", [])},
                              review_evidence["output_ref"]), ending_plan=True,
                )
            return None, {"status": "DESIGN_REWORK_REQUIRED", "reason": "SHORT_ENDING_REQUIRED",
                          "review_task_id": review["task_id"], "review_ref": review_evidence["output_ref"]}
        if "ending_plan" in evidence:
            from .scene_revision import editorial_blockers
            if editorial_blockers(review_payload["issues"]):
                decision = kernel._prior(repo, owner, wr["work_id"], "P6_ENDING_FIX_AUTHORIZATION",
                                         review_evidence["attempt_id"], {})
                if designer.get("revision_purpose") == "SHORT_ENDING" and decision:
                    return None, _enroll_design_step(
                        kernel, gateway, repo, owner, wr, intake, task, evidence, design, round_index + 1,
                        revision=(review_payload, review_evidence["output_ref"]), editor_decision=decision)
                if quality:
                    return None, _enroll_design_step(
                        kernel, gateway, repo, owner, wr, intake, task, evidence, design, round_index + 1,
                        revision=(review_payload, review_evidence["output_ref"]))
                return None, {"status": "DESIGN_REWORK_REQUIRED", "reason": "ENDING_PLAN_BLOCKERS",
                              "review_attempt": review_evidence["attempt_id"], "review_ref": review_evidence["output_ref"]}
            confirm_ending(review_payload, evidence["ending_plan"]["source"], evidence["output_ref"])
        issues = review_payload["issues"]
        revision_available = (intake.get("model_routes") and
                              (quality or designer["editorial_count"] < intake["profile"]["max_reworks"]))
        if intake.get("model_routes"):
            from .scene_revision import editorial_blockers
            open_questions = [i for i in issues if i["disposition"] == "OPEN"
                              and i["issue_kind"] == "SEMANTIC_CONCERN" and i["severity"] == "QUESTION"]
            issues = editorial_blockers(issues)
            if revision_available and designer["editorial_count"] == 0:
                issues += open_questions
        if issues:
            if revision_available:
                return None, _enroll_design_step(
                    kernel, gateway, repo, owner, wr, intake, task, evidence, design, round_index + 1,
                    revision=(review_payload, review_evidence["output_ref"]),
                )
            return None, {
                "status": "DESIGN_REWORK_REQUIRED",
                "review_task_id": review["task_id"],
                "review_ref": review_evidence["output_ref"],
                "issue_count": len(review_payload["issues"]),
            }
        verify_method(kernel, repo, owner, wr, designer.get("repair_plan"), evidence, "initial_world",
                      evidence["output_ref"], review_evidence["attempt_id"], review_evidence["output_ref"])
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
            "source_input_ref": designer["input_ref"],
            **({"design_revision_patch": evidence["revision_patch"]} if "revision_patch" in evidence else {}),
            **({"design_ending_plan": evidence["ending_plan"]} if "ending_plan" in evidence else {}),
            **({"design_normalization": evidence["normalization"]} if "normalization" in evidence else {}),
            **({"design_revision_round": round_index, "original_input_ref": intake["input_ref"]} if round_index else {}),
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
            **({"advisories": review_payload["issues"]} if review_payload["issues"] else {}),
            **({"short_arc": arc} if arc else {}),
        }
        kernel.tasks.accept_in_transaction(repo.conn, review["task_id"], review["generation"])
        if task["state"] == "RESULT_STORED":
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
    opening = design.get("opening")
    first = _point(0 if opening else 1)
    facts = [(actor, "CHARACTER", design["protagonist"]), (place, "REGION", design["place"])]
    facts.extend(
        (_uid(work, f"rule:{i}"), "KNOWLEDGE", rule) for i, rule in enumerate(design["world_rules"])
    )
    if opening:
        from .idea_opening import opening_entities

        facts.extend(opening_entities(work, design))
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
    genesis = {"entities": entities, "relations": [relation]}
    if opening:
        from .idea_opening import add_opening_facts

        add_opening_facts(genesis, work, design, first)
    created = kernel.bootstrap_work(
        design["title"],
        genesis,
        frontiers={eid: _point(first["tick"], 1) for eid in entities},
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
        **({"opening_version": 1} if opening else {}),
        "volumes": [volume],
        "story_time": _point(len(design["chapter_goals"]), 1),
        "subject_ids": list(entities),
        "predicates": ["located_in", "lifecycle"],
    }


def create_planned_window(
    kernel, owner, wr, volume, actor, place, design_ref, brief, goals, rules, *, start_after=0,
    opening_events=(), opening_explanation="", upstream_refs=(),
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
        "upstream_literary_refs": list(upstream_refs),
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
        events = list(opening_events) if not slot["reader_cut_ordinal"] else []
        card = {
            "schema_version": "2.0",
            "scene_id": slot["scene_id"],
            "input_world_ref": wr,
            "story_interval": slot["story_interval"],
            "event_refs": events,
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
                "required_event_refs": events,
                "required_predecessor_scene_ids": slot["required_predecessor_scene_ids"],
                "entry_checks": [],
                "exit_checks": [],
                "allowed_disclosures": [],
                "literary_purpose": node["goal"] + ("\n" + opening_explanation if events else ""),
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
