"""Create and independently read a child draft set for a returned, unpublished volume."""

import json
from datetime import datetime

from ..domain.canonical import canonical_bytes, fingerprint
from ..domain.errors import DomainError
from ..domain.review_gate import blocking_review_issues
from .authorization import Principal
from .idea_intake import _audited_design_result
from .kernel import require
from .narrative import NarrativeService
from .planning import PlanningService
from .production_pipeline import _artifact, _current_owner
from .release import ReleaseService
from .runtime_tasks import RuntimeTaskService
from .volume_successor import _selected

MAX_EDITIONS = 3
MAX_SCENE_REWORKS = 2


def begin(kernel, repo, owner, state, cursor, task, proof, output):
    from .quality_memory import plan_repair, reflect_repair

    wr = state["binding"]["world_ref"]
    rounds = cursor.setdefault("edition_attempts", {})
    count = rounds.get(state["volume"], 0)
    require(type(count) is int and 0 <= count < MAX_EDITIONS,
            "EDITION_REWORK_EXHAUSTED", "Unpublished volume revision allowance is exhausted")
    rounds[state["volume"]] = count + 1
    from .rolling_production import _save
    _save(kernel, repo, owner, cursor)
    findings = {"phase": state["jobs"][state["index"] - 1].get("phase"),
                "reason": output.get("reason", output.get("summary", "")),
                "deferred_questions": output.get("deferred_questions", [])}
    plan = reflect_repair(kernel, repo, owner, wr, task["task_id"], proof["attempt_id"], proof["output_ref"],
                          plan_repair("editorial", findings, []), "FROZEN_EDITOR_RETURNED")
    state.update(status="EDITION_REVISING", edition={"reflection_ref": plan["reflection_ref"], "error_ref": plan["error_ref"],
        "parent_checkpoint": cursor["last_checkpoint"], "parent_session": state["session"],
        "editor_task_id": task["task_id"], "editor_attempt_id": proof["attempt_id"],
        "editor_output_ref": proof["output_ref"], "scene_index": 0, "phase": "write",
        "round": count + 1, "scene_reworks": {}, "candidate_history": [],
        "candidates": [], "readings": []})


def _guard(kernel, repo, state, cursor):
    from .rolling_production import frozen_volume_scenes

    wr = state["binding"]["world_ref"]
    edit = state["edition"]
    require(cursor["run_id"] == state["run"] and cursor["state"] == "FROZEN"
            and cursor["last_checkpoint"] == edit["parent_checkpoint"] and state["session"] == edit["parent_session"]
            and kernel.head(repo, wr["work_id"], wr["branch_id"]) == wr
            and not _selected(repo, wr["work_id"], "RELEASE:" + state["volume"]),
            "TASK_REBIND_REQUIRED", "Returned edition is no longer the current unpublished frozen parent")
    scenes = frozen_volume_scenes(kernel, repo, cursor, state["volume"])
    require([s["acceptance_ref"] for s in scenes] == state["binding"]["ordered_acceptance_refs"],
            "NARRATIVE_SELECTION_CONFLICT", "Original frozen acceptances changed")
    return scenes


def _writer_source(kernel, repo, owner, state, scene):
    """Retain the immutable writing card; adopt only through fresh child checks."""
    adopted = state["binding"]["world_ref"]
    work = adopted["work_id"]
    card = kernel.artifact(repo, scene["scene_ref"], work, kind="SCENE_PLAN")["payload"]
    plan = kernel.artifact(repo, card["plan_ref"], work, kind="PLAN_REVISION")["payload"]
    parent = kernel.artifact(repo, scene["draft_ref"], work, kind="NARRATIVE_DRAFT")["payload"]
    source = card["scene_contract"]["input_world_ref"]
    acceptance = kernel.artifact(repo, scene["acceptance_ref"], work, kind="NARRATIVE_ACCEPTANCE")["payload"]
    require(plan["input_world_ref"] == parent["input_world_ref"] == source
            and parent["scene_ref"] == scene["scene_ref"] and parent["text_ref"] == scene["text_ref"]
            and repo.ancestor(source, adopted)
            and acceptance["draft_ref"] == scene["draft_ref"] and acceptance["scene_ref"] == scene["scene_ref"]
            and acceptance["text_ref"] == scene["text_ref"] and acceptance["plan_ref"] == card["plan_ref"]
            and acceptance["world_ref"] == adopted,
            "TASK_REBIND_REQUIRED", "Returned writer source must be the frozen parent's exact ancestral scene and plan")
    checked = NarrativeService(kernel).validate_for_acceptance(
        repo, owner, scene["draft_ref"], acceptance["reconciliation_ref"], adopted)
    require(checked["alignment_ref"] == acceptance["alignment_ref"],
            "NARRATIVE_ALIGNMENT_REQUIRED", "Frozen parent lacks its executed adopted-world alignment")
    return {"writer_source_world_ref": source, "adopted_world_ref": adopted,
            "parent_acceptance_ref": scene["acceptance_ref"], "scene_ref": scene["scene_ref"]}


def _write_job_world(kernel, repo, owner, state, scene, task):
    binding = _writer_source(kernel, repo, owner, state, scene)
    job = repo.one("supervisor_jobs", {"task_id": task["task_id"]})
    spec = repo.one("task_inputs", {"task_id": task["task_id"], "generation": task["generation"]})
    require(job and job["config"].get("returned_writer_binding") == binding
            and job["config"].get("returned_edition") == {
                "parent_session": state["session"], "phase": "write", "scene": state["edition"]["scene_index"]}
            and (task["role_id"], task["task_kind"]) == ("writer", "targeted_revision")
            and spec and spec["spec"]["world_ref"] == binding["writer_source_world_ref"]
            and scene["scene_ref"] in spec["spec"]["input_artifacts"],
            "TASK_REBIND_REQUIRED", "Returned writing result lost its frozen source and adopted-world binding")
    return binding["writer_source_world_ref"]


def _assign(kernel, repo, gateway, owner, state, cursor, scene):
    from .supervisor import RuntimeSupervisor

    ed = state["edition"]
    wr = state["binding"]["world_ref"]
    work = wr["work_id"]
    phase = ed["phase"]
    reading = phase != "write"
    writer_binding = None if reading else _writer_source(kernel, repo, owner, state, scene)
    task_wr = wr if reading else writer_binding["writer_source_world_ref"]
    role, kind = {"write": ("writer", "targeted_revision"), "read": ("reviewer", "literary"),
                  "epistemic": ("reviewer", "epistemic"), "facts": ("evidence_maintainer", "assertion_extract"),
                  "verify": ("reviewer", "verification")}[phase]
    deadline = int((datetime.fromisoformat(cursor["authorization"]["deadline"]) - repo.now()).total_seconds())
    require(deadline > 0, "TASK_EXPIRED", "Returned edition allowance expired")
    auth = cursor["allowance"]
    prior = kernel.artifact(repo, scene["draft_ref"], work, kind="NARRATIVE_DRAFT")["payload"]
    packet_ref, predecessors = None, []
    if not reading:
        from .returned_edition_acceptance import PACKET_KIND, predecessor_packet
        packet = predecessor_packet(kernel, repo, owner, state, scene)
        if packet:
            packet_ref = _artifact(kernel, repo, work, owner, PACKET_KIND, packet)
            predecessors = [item["draft_ref"] for item in packet["predecessors"]]
    assignment = {"operation": "read_returned_edition" if phase == "read" else "revise_returned_edition" if not reading else phase,
                  "volume_scene_ordinal": ed["scene_index"] + 1,
                  "instructions": ("先读原稿，再读本次候选；核对是否回应退回意见、保留视角与既有事件。报告仍有的问题。"
                                   if reading else "按已授权的本场原文与退回意见修订。返回完整正文。只有涉及本场的问题才改动；保留事件、认知边界和未受影响文字。")}
    original = scene["draft_ref"]
    mate = ed["candidates"][ed["scene_index"]]["draft_ref"] if reading else None
    # If the writer preserved a passage, its child draft has a new identity yet equal bytes.
    materials = [original, mate] if phase == "read" else [mate] if reading else [scene["scene_ref"], original, ed["editor_output_ref"]]
    extra = []
    if phase == "write" and ed.get("retry_feedback_ref"):
        extra = [ed["failed_draft_ref"], ed["retry_feedback_ref"]]
        assignment["instructions"] += "先读失败候选及本版逐项诊断，再按具体修正方法修改；待核查不等于已证实世界冲突，勿编造新设定或删除必要事件来规避核查。"
    elif phase == "facts":
        extra = ed.get("internal_resolution_refs", [])
    elif phase == "verify":
        from .internal_review_resolution import _source_refs
        from .internal_review_resolution import assignment as verification_assignment
        request = ed["verification_request"]
        assignment = verification_assignment(request)
        extra = _source_refs(request)
        materials = [mate, request["report_ref"]]
    if phase != "verify":
        materials += [ref for ref in extra if ref not in materials]
    if packet_ref:
        materials += [packet_ref, *predecessors]
        assignment["instructions"] += "先通读本版前驱子稿及其已通过三审的交接包；以下续写必须衔接本版前驱，旧正式接受回执仅保留历史，不代表本版正文。"
    if phase == "epistemic":
        assignment["instructions"] = "独立通读本次候选，核对人物认知来源与披露时序；只将有固定原文和系统查询反证的问题标为硬冲突，未知事项交待核查。"
    elif phase == "facts":
        from .rolling_facts import assignment as facts_assignment
        assignment = facts_assignment()
    card = kernel.artifact(repo, scene["scene_ref"], work, kind="SCENE_PLAN")["payload"]
    refs = [card["plan_ref"], card["batch_ref"], scene["scene_ref"], original, ed["editor_output_ref"],
            *prior["handoff_refs"], *([mate] if reading else []), *extra]
    if packet_ref:
        refs += [packet_ref, *predecessors]
    correction = state.get("technical_correction")
    if correction:
        if isinstance(assignment, str):
            assignment += "\n格式修复方法：" + correction["method"]
        else:
            assignment.update(repair_strategy=correction["method"], validation_errors=[correction["diagnostic"]])
    prepared = RuntimeTaskService(kernel, gateway).prepare(
        owner, task_wr, role_id=role, task_kind=kind, assignment=assignment if isinstance(assignment, str) else json.dumps(assignment, ensure_ascii=False),
        review_belief=phase in {"epistemic", "verify"},
        knowledge_reread_reason="known_error" if correction else None,
        adapter_id=auth["routes"]["review" if reading else "draft"], subject_ids=auth["subject_ids"],
        predicates=auth["predicates"], story_time=auth["story_time"], material_refs=materials, input_artifacts=refs,
        queue_state="READY", deadline_seconds=deadline, lease_seconds=min(600, deadline),
        max_amount=auth["reserve_amount"], token_budget=auth["token_reservation"],
        currency=repo.one("budgets", {"work_id": work, "budget_id": auth["budget_id"]})["currency"])
    sup = RuntimeSupervisor(kernel, gateway)
    config, _ = sup.dispatch_config(
        repo, prepared.spec, repo.one("grants", {"grant_id": prepared.principal.grant_id}),
        manifest_ref=prepared.manifest_ref, knowledge_cut={"plane": "WORLD"}, context_tokens=20000,
        input_ref=prepared.agent_input["input_ref"], budget_id=auth["budget_id"],
        reserve_amount=auth["reserve_amount"], token_reservation=auth["token_reservation"])
    config["returned_edition"] = {"parent_session": state["session"], "phase": ed["phase"], "scene": ed["scene_index"]}
    if writer_binding:
        config["returned_writer_binding"] = writer_binding
    if packet_ref:
        config["returned_predecessor_packet_ref"] = packet_ref
    if reading:
        frozen = kernel.artifact(repo, prepared.agent_input["input_ref"], work, kind="AGENT_INPUT")["payload"]
        require(gateway.adapters[frozen["adapter_id"]].configuration.get("adapter_kind") == "codex-cli",
                "MODEL_UNAVAILABLE", "Child edition checking requires native Codex")
        config["required_review_mcp"] = True
    if correction:
        frozen = kernel.artifact(repo, prepared.agent_input["input_ref"], work, kind="AGENT_INPUT")["payload"]
        require(all(frozen[key] == correction[key] for key in ("runtime_sha256", "model_config_sha256", "output_contract")),
                "TASK_REBIND_REQUIRED", "Child format correction cannot change the frozen runtime, model or contract")
        state.pop("technical_correction", None)
    sup.install_job(repo, prepared.task, config)
    state["active"] = prepared.task["task_id"]


def prepare_materials(kernel, task, mark):
    from .frozen_editing import _state
    from .rolling_production import _load

    work, branch = task["work_id"], task["branch_id"]
    with kernel.objects.lock(), kernel.db.uow() as repo:
        repo.one("works", {"work_id": work}, lock="update")
        bound = kernel.in_transaction(repo)
        cursor = _load(bound, repo, work, branch)
        owner = _current_owner(bound, repo, cursor)
        state = _state(bound, repo, work, branch)
        require(state and state["status"] == "EDITION_REVISING" and state["active"] == task["task_id"]
                and mark == {"parent_session": state["session"], "phase": state["edition"]["phase"],
                             "scene": state["edition"]["scene_index"]},
                "TASK_REBIND_REQUIRED", "Child-edition task lost its frozen assignment")
        scene = _guard(bound, repo, state, cursor)[mark["scene"]]
        key = "returned-edition:" + task["task_id"] + ":" + str(task["generation"])
        orig = scene["draft_ref"]
        refs = [orig]
        if mark["phase"] == "write":
            _write_job_world(bound, repo, owner, state, scene, task)
            prior = bound.artifact(repo, orig, work, kind="NARRATIVE_DRAFT")["payload"]
            card = bound.artifact(repo, scene["scene_ref"], work, kind="SCENE_PLAN")["payload"]
            PlanningService(bound).bind_task_for_controller(owner, task["task_id"], task["generation"],
                                                          card["plan_ref"], card["plan_node_ids"], key)
            ReleaseService(bound).bind_writer_task(Principal(task["grant_id"], task["principal_id"]),
                task["task_id"], task["generation"], scene["scene_ref"], prior["handoff_refs"], key)
            if state["edition"].get("failed_draft_ref"):
                refs.append(state["edition"]["failed_draft_ref"])
            job = repo.one("supervisor_jobs", {"task_id": task["task_id"]})["config"]
            if job.get("returned_predecessor_packet_ref"):
                from .returned_edition_acceptance import PACKET_KIND
                packet = bound.artifact(repo, job["returned_predecessor_packet_ref"], work, kind=PACKET_KIND)["payload"]
                refs += [item["draft_ref"] for item in packet["predecessors"]]
        else:
            child = state["edition"]["candidates"][mark["scene"]]
            source = repo.one("tasks", {"work_id": work, "task_id": child["writer_task_id"]})
            require(source and source["principal_id"] != task["principal_id"], "INDEPENDENT_REVIEW_REQUIRED",
                    "The child author cannot independently read its own revision")
            refs = [child["draft_ref"]] if mark["phase"] != "read" else [orig, child["draft_ref"]]
        NarrativeService(bound).bind_reader_task(owner, task["task_id"], task["generation"], refs, key)


def _current_job(kernel, repo, task, mark):
    """Restrict a corrected reading to its exact current unpublished child slot."""
    from .frozen_editing import _state
    from .rolling_production import _load
    state = _state(kernel, repo, task["work_id"], task["branch_id"])
    require(state and state["status"] == "EDITION_REVISING" and state["active"] == task["task_id"]
            and mark == {"parent_session": state["session"], "phase": state["edition"]["phase"],
                         "scene": state["edition"]["scene_index"]}
            and state["edition"]["phase"] in {"epistemic", "facts", "read", "verify"},
            "TASK_REBIND_REQUIRED", "The returned child reading is no longer the selected slot")
    cursor = _load(kernel, repo, task["work_id"], task["branch_id"])
    _guard(kernel, repo, state, cursor)
    current = repo.one("tasks", {"task_id": task["task_id"]})
    spec = repo.one("task_inputs", {"task_id": task["task_id"], "generation": task["generation"]})
    child = state["edition"]["candidates"][mark["scene"]]
    expected = {"epistemic": ("reviewer", "epistemic"), "facts": ("evidence_maintainer", "assertion_extract"),
                "read": ("reviewer", "literary"), "verify": ("reviewer", "verification")}[mark["phase"]]
    require(current and current["generation"] == task["generation"] and current["context_bundle_id"] == task["context_bundle_id"]
            and (current["role_id"], current["task_kind"]) == expected
            and spec and spec["spec"]["world_ref"] == state["binding"]["world_ref"]
            and child["draft_ref"] in spec["spec"]["input_artifacts"],
            "TASK_REBIND_REQUIRED", "Corrected child reading must retain its fixed world and candidate")
    return state


def material(kernel, principal, task_id, generation, context_bundle_id, artifact_ref):
    """Deliver only the active scene's editorial feedback, never the whole volume's raw issue set."""
    from .frozen_editing import _state
    from .rolling_production import _load
    with kernel.db.uow(write=False) as repo:
        task, _, spec, _ = NarrativeService(kernel)._task(repo, principal, task_id, generation)
        job = repo.one("supervisor_jobs", {"task_id": task_id})
        mark = job and job["config"].get("returned_edition")
        if not mark:
            return None
        state = _state(kernel, repo, task["work_id"], task["branch_id"])
        if not state:
            return None
        ed = state["edition"]
        allowed = [ed["editor_output_ref"], *ed.get("internal_resolution_refs", []),
                   *([ed["retry_feedback_ref"]] if ed.get("retry_feedback_ref") else []),
                   *([ed["verification_request"]["report_ref"]] if ed.get("phase") == "verify" else [])]
        packet_ref = job["config"].get("returned_predecessor_packet_ref")
        if packet_ref:
            allowed.append(packet_ref)
        if artifact_ref not in allowed:
            return None
        require(state["status"] == "EDITION_REVISING" and state["active"] == task_id
                and task["context_bundle_id"] == context_bundle_id and artifact_ref in spec["input_artifacts"]
                and mark == {"parent_session": state["session"], "phase": state["edition"]["phase"], "scene": state["edition"]["scene_index"]},
                "VIEW_DENIED", "Editorial feedback is outside this child task")
        cursor = _load(kernel, repo, task["work_id"], task["branch_id"])
        if artifact_ref == packet_ref:
            from .returned_edition_acceptance import PACKET_KIND, predecessor_packet
            scene = _guard(kernel, repo, state, cursor)[mark["scene"]]
            actual = kernel.artifact(repo, packet_ref, task["work_id"], kind=PACKET_KIND)["payload"]
            require(mark["phase"] == "write" and actual == predecessor_packet(
                kernel, repo, _current_owner(kernel, repo, cursor), state, scene),
                "VIEW_DENIED", "Edition handoff packet is outside this exact current writer")
            return canonical_bytes(actual)
        scene = _guard(kernel, repo, state, cursor)[mark["scene"]]
        if ed.get("phase") == "verify" and artifact_ref == ed["verification_request"]["report_ref"]:
            from .internal_review_resolution import _assignment_case_hash
            frozen = kernel.artifact(repo, job["config"]["dispatch"]["input_ref"], task["work_id"], kind="AGENT_INPUT")["payload"]
            require((task["role_id"], task["task_kind"]) == ("reviewer", "verification")
                    and _assignment_case_hash(frozen["assignment"]) == ed["verification_request"]["case_hash"],
                    "VIEW_DENIED", "Child verification cannot switch its frozen case")
            return canonical_bytes({"source_ref": artifact_ref, "projection": "INTERNAL_CASE_WINDOW",
                                    "case": ed["verification_request"]})
        if artifact_ref in ed.get("internal_resolution_refs", []):
            record = kernel.artifact(repo, artifact_ref, task["work_id"], kind="INTERNAL_REVIEW_RESOLUTION")["payload"]
            require(record["request"]["source_draft_ref"] == ed["candidates"][ed["scene_index"]]["draft_ref"],
                    "STALE_GENERATION", "Child verification findings belong to another candidate")
            return canonical_bytes({"source_ref": artifact_ref, "resolution": record})
        if artifact_ref == ed.get("retry_feedback_ref"):
            record = kernel.artifact(repo, artifact_ref, task["work_id"], kind="RETURNED_EDITION_REVISION_FEEDBACK")["payload"]
            require(mark["phase"] == "write" and record["failed_draft_ref"] == ed["failed_draft_ref"]
                    and record["parent_draft_ref"] == scene["draft_ref"],
                    "VIEW_DENIED", "Child revision feedback belongs to another scene")
            return canonical_bytes(record)
        candidate = kernel.artifact(repo, artifact_ref, task["work_id"], kind="AGENT_CANDIDATE")["payload"]
        findings = []
        for ref in state["reports"]:
            report = kernel.artifact(repo, ref, task["work_id"], kind="REVIEW_REPORT")["payload"]["submission"]
            findings.extend(issue for issue in report["issues"] if issue["subject_ref"] in (scene["draft_ref"], scene["text_ref"]))
        return canonical_bytes({"editor_output_ref": artifact_ref, "source_draft_ref": scene["draft_ref"],
            "editor_reason": candidate.get("reason", candidate.get("summary", "")), "scene_findings": findings,
            "deferred_questions": candidate.get("deferred_questions", []),
            "revision_findings": state["edition"].get("revision_findings", [])})


def _board(state, child):
    """Adapt exact child evidence to the existing independent factual services."""
    source = {"stage": "draft", "output_ref": child["draft_ref"], "task_id": child["writer_task_id"],
              "generation": child["writer_generation"]}
    knowledge = {"stage": "epistemic", "state": "KNOWLEDGE_CLEAR", "output_ref": child.get("knowledge_ref"),
                 "task_id": child.get("epistemic_task_id"), "generation": child.get("epistemic_generation")}
    return {"world_ref": state["binding"]["world_ref"], "nodes": {"child": source, "knowledge": knowledge}}


def _retry(kernel, repo, owner, state, scene, task, proof, *, stage, findings, report_ref):
    """Spend one shared scene allowance; retain the failed child and exact diagnosis."""
    ed = state["edition"]
    key = str(ed["scene_index"])
    count = ed["scene_reworks"].get(key, 0)
    require(count < MAX_SCENE_REWORKS, "EDITION_REWORK_EXHAUSTED", "Child scene revision allowance is exhausted")
    child = ed["candidates"][ed["scene_index"]]
    feedback = {"world_ref": state["binding"]["world_ref"], "parent_session": state["session"],
        "scene_id": scene["scene_id"], "parent_draft_ref": scene["draft_ref"], "failed_draft_ref": child["draft_ref"],
        "stage": stage, "report_ref": report_ref, "task_id": task["task_id"], "generation": task["generation"],
        "attempt_id": proof["attempt_id"], "findings": findings,
        "internal_resolutions": [{"resolution_ref": reference,
            "resolution": kernel.artifact(repo, reference, task["work_id"], kind="INTERNAL_REVIEW_RESOLUTION")["payload"]}
            for reference in ed.get("internal_resolution_refs", [])],
        "method": "逐项定位失败候选中的精确span，按当前独立核查证据局部修订，保留既有事件与人物知识边界。"
                  "UNKNOWN/待核查不能写成已证实矛盾，不得编造新canon。返回完整正文供全套重新独立审读。"}
    reference = _artifact(kernel, repo, task["work_id"], owner, "RETURNED_EDITION_REVISION_FEEDBACK", feedback)
    ed["scene_reworks"][key] = count + 1
    child["failure_feedback_ref"] = reference
    ed["candidate_history"].append(ed["candidates"].pop())
    ed.update(phase="write", failed_draft_ref=child["draft_ref"], retry_feedback_ref=reference)
    for field in ("verification_board", "verification_request", "internal_resolution_refs"):
        ed.pop(field, None)
    state["active"] = None


def _fact_case(kernel, repo, state, child, task, facts, report, *, start=0):
    from .internal_review_resolution import request_from_fact_report
    board = _board(state, child)
    board["story_time"] = repo.one("task_inputs", {"task_id": task["task_id"], "generation": task["generation"]})["spec"]["story_time"]
    board["nodes"]["facts"] = {"stage": "facts", "parents": ["child"], "task_id": task["task_id"],
        "generation": task["generation"], "output_ref": child["facts_ref"]}
    extraction = kernel.artifact(repo, facts["extraction_ref"], task["work_id"], kind="ASSERTION_EXTRACTION")["payload"]
    call = repo.one("call_attempts", {"task_id": task["task_id"], "generation": task["generation"], "call_key": "supervised-candidate"})
    request = request_from_fact_report(board, "child", board["nodes"]["facts"], facts["reconciliation_ref"], report,
                                      extraction, attempt_id=call["attempt_id"], question_start=start)
    state["edition"].update(verification_board=board, verification_request=request, phase="verify")


def _repair_key(state):
    ed = state["edition"]
    return "returned:" + fingerprint([state["session"], ed["scene_index"], ed["phase"]])


def _reread(kernel, repo, gateway, state, task, error, proof=None):
    """Correct returned review evidence or representation, never the manuscript."""
    from .review_recovery import ERRORS, recover_review
    key = _repair_key(state)
    _current_job(kernel, repo, task, repo.one("supervisor_jobs", {"task_id": task["task_id"]})["config"]["returned_edition"])
    evidence = state.setdefault("evidence_repairs", {}).setdefault(key, [])
    formats = state.setdefault("format_history", {}).setdefault(key, [])
    semantic = state.setdefault("technical_history", {}).setdefault(key, [])
    if len(evidence) + len(formats) + len(semantic) >= 6:
        state.update(status="EDITION_REVISION_BLOCKED", reason="TECHNICAL_REPAIR_BUDGET_EXHAUSTED")
        return
    if error.code in ERRORS:
        node = {"task_id": task["task_id"], "state": "ENROLLED", "evidence_repairs": evidence}
        require(recover_review(kernel, repo, gateway, node, task, error), error.code, error.safe_message)
        if node["state"] != "PENDING":
            state.update(status="EDITION_REVISION_BLOCKED", reason=node["reason"])
            return
        row = evidence[-1]
        diagnostic = {"code": error.code}
    else:
        require(proof is not None and task["state"] == "RESULT_STORED", "INVALID_EVIDENCE", "Representation repair needs a verified returned reading")
        if len(semantic) >= 2:
            state.update(status="EDITION_REVISION_BLOCKED", reason="CHILD_READING_FORMAT_EXHAUSTED")
            return
        diagnostic = getattr(error, "diagnostic", {"code": error.code, "constraint": error.safe_message})
        row = {"task_id": task["task_id"], "generation": task["generation"], "attempt_id": proof["attempt_id"],
            "candidate_ref": proof["output_ref"], "input_ref": proof["input_ref"], "diagnostic": diagnostic,
            "method": "保持当前原稿、WorldRef和判断范围不变，重新亲自MCP读取全部材料。只校正报告表示与引用："
                      "事实抽取逐字覆盖完整正文，优先使用连续passage_ids；核查答复完整对应本case各问题，引用实际查询和当前原稿/报告。"
                      "不得通过改写原稿、降低断言分类或伪造证据来修复报告。诊断：" + json.dumps(diagnostic, ensure_ascii=False)}
        semantic.append(row)
        repo.change("tasks", {"task_id": task["task_id"]}, {"state": "WAITING_REVIEW", "updated_at": repo.now(),
            "lease_owner": None, "lease_expiry": None})
        kernel.tasks._emit(repo.conn, task["task_id"], task["generation"], "WAITING_REVIEW")
    frozen = kernel.artifact(repo, row["input_ref"], task["work_id"], kind="AGENT_INPUT")["payload"]
    state.update(active=None, technical_correction={"method": row["method"], "diagnostic": diagnostic,
        "attempt_id": row["attempt_id"], **{name: frozen[name] for name in ("runtime_sha256", "model_config_sha256", "output_contract")}})


def advance(kernel, repo, gateway, owner, state, cursor):
    ed = state["edition"]
    if ed["phase"] == "accept":
        from .returned_edition_acceptance import commit_edition
        commit_edition(kernel, repo, owner, state, cursor)
        return True
    scenes = _guard(kernel, repo, state, cursor)
    scene = scenes[ed["scene_index"]]
    if not state["active"]:
        _assign(kernel, repo, gateway, owner, state, cursor, scene)
        return True
    task = repo.one("tasks", {"task_id": state["active"]})
    if task["state"] != "RESULT_STORED":
        if task["state"] == "FAILED" and ed["phase"] != "write":
            from .frozen_format_recovery import recover
            try:
                recover(kernel, repo, gateway, owner, state, task)
            except DomainError as error:
                if error.code != "CALL_RECONCILIATION_REQUIRED":
                    raise
                state.update(status="EDITION_REVISION_BLOCKED", reason=error.code)
            return True
        if task["state"] in {"FAILED", "PAUSED", "STALE", "WAITING_REVIEW", "CANCELLED"}:
            state.update(status="EDITION_REVISION_BLOCKED", reason=task["state"])
            return True
        return False
    try:
        result_wr = (_write_job_world(kernel, repo, owner, state, scene, task)
                     if ed["phase"] == "write" else state["binding"]["world_ref"])
        proof, output = _audited_design_result(kernel, gateway, repo, task, result_wr)
        require(proof["world_ref"] == result_wr, "TASK_REBIND_REQUIRED", "Child audit differs from its fixed task cut")
        if ed["phase"] != "write":
            from .review_mcp import require_review_queries
            require_review_queries(kernel, repo, task, proof)
    except DomainError as error:
        from .review_recovery import ERRORS
        if error.code == "CALL_RECONCILIATION_REQUIRED":
            state.update(status="EDITION_REVISION_BLOCKED", reason=error.code)
            return True
        if ed["phase"] == "write" or error.code not in ERRORS:
            raise
        _reread(kernel, repo, gateway, state, task, error)
        return True
    work = task["work_id"]
    if ed["phase"] == "write":
        prior = kernel.artifact(repo, scene["draft_ref"], work, kind="NARRATIVE_DRAFT")["payload"]
        draft = NarrativeService(kernel).store_draft(
            Principal(task["grant_id"], task["principal_id"]), task["task_id"], task["generation"],
            scene["scene_ref"], output["text"], "returned-child:" + task["task_id"],
            prior_draft_ref=scene["draft_ref"], handoff_refs=prior["handoff_refs"], title=prior["title"],
            declared_important_deltas=output["important_deltas"], agent_output_ref=proof["output_ref"])
        payload = kernel.artifact(repo, draft, work, kind="NARRATIVE_DRAFT")["payload"]
        from .returned_edition_acceptance import record_handoff
        handoff = record_handoff(kernel, repo, owner, task, proof, draft)
        ed["candidates"].append({"scene_id": scene["scene_id"], "parent_ref": scene["draft_ref"], "draft_ref": draft,
                                "text_ref": payload["text_ref"], "changed": scene["text_ref"]["sha256"] != payload["text_ref"]["sha256"],
                                "writer_task_id": task["task_id"], "writer_generation": task["generation"],
                                "writer_source_world_ref": result_wr, "adopted_world_ref": state["binding"]["world_ref"],
                                "attempt_id": proof["attempt_id"]})
        if handoff:
            ed["candidates"][-1]["handoff_proof_ref"] = handoff
        ed["phase"] = "epistemic"
        ed["fact_rereads"] = 0
    elif ed["phase"] == "epistemic":
        from .rolling_facts import record_knowledge
        child = ed["candidates"][ed["scene_index"]]
        reference, status = record_knowledge(kernel, repo, owner, _board(state, child), {"parents": ["child"]}, task, proof, output)
        child.update(knowledge_ref=reference, epistemic_task_id=task["task_id"], epistemic_generation=task["generation"])
        if status != "KNOWLEDGE_CLEAR":
            _retry(kernel, repo, owner, state, scene, task, proof, stage="epistemic",
                   findings=blocking_review_issues(output["issues"]), report_ref=reference)
        else:
            ed["phase"] = "facts"
    elif ed["phase"] == "facts":
        from .rolling_facts import audit
        child = ed["candidates"][ed["scene_index"]]
        from .rolling_facts import ReadingFormatError
        try:
            facts = audit(kernel, repo, owner, _board(state, child), {"parents": ["child"], "knowledge_node": "knowledge"},
                          task, output, defer_accept=True)
        except ReadingFormatError as error:
            _reread(kernel, repo, gateway, state, task, error, proof)
            return True
        child.update(facts_ref=_artifact(kernel, repo, work, owner, "ROLLING_FACT_REPORT", facts),
                     reconciliation_ref=facts["reconciliation_ref"], alignment_ref=facts["alignment_ref"],
                     extraction_ref=facts["extraction_ref"])
        if facts["status"] == "CLEAR":
            ed["phase"] = "read"
        else:
            report = kernel.artifact(repo, facts["reconciliation_ref"], work, kind="RECONCILIATION_REPORT")["payload"]
            if any(item["code"].startswith("UNRESOLVED_") for item in report["hard_blockers"]) and ed.get("fact_rereads", 0) < 2:
                _fact_case(kernel, repo, state, child, task, facts, report)
            else:
                extraction = kernel.artifact(repo, facts["extraction_ref"], work, kind="ASSERTION_EXTRACTION")["payload"]
                assertions = {item["assertion_id"]: item for item in extraction["assertions"]}
                _retry(kernel, repo, owner, state, scene, task, proof, stage="facts",
                       findings=[{**item, "assertion": assertions.get(item["assertion_id"])} for item in report["hard_blockers"]],
                       report_ref=facts["reconciliation_ref"])
    elif ed["phase"] == "verify":
        from .internal_review_resolution import RESOLVED, consume_resolution
        from .production_verification import deliver_verification
        request = ed["verification_request"]
        board = ed["verification_board"]
        try:
            reference, outcome = deliver_verification(kernel, repo, owner, board, {"internal_case": request}, task, proof, output)
        except DomainError as error:
            if error.code not in {"INVALID_ARGUMENT", "INVALID_EVIDENCE"}:
                raise
            _reread(kernel, repo, gateway, state, task, error, proof)
            return True
        ed.setdefault("internal_resolution_refs", []).append(reference)
        if outcome != RESOLVED:
            state.update(status="EDITION_REVISION_BLOCKED", reason=outcome)
        else:
            consumed = consume_resolution(kernel, repo, owner, board, request, reference)
            if consumed.get("next_question_start") is not None:
                child = ed["candidates"][ed["scene_index"]]
                source = repo.one("tasks", {"task_id": request["source_task_id"]})
                facts = kernel.artifact(repo, child["facts_ref"], work, kind="ROLLING_FACT_REPORT")["payload"]
                report = kernel.artifact(repo, facts["reconciliation_ref"], work, kind="RECONCILIATION_REPORT")["payload"]
                _fact_case(kernel, repo, state, child, source, facts, report, start=consumed["next_question_start"])
            else:
                ed.update(phase="facts", fact_rereads=ed.get("fact_rereads", 0) + 1)
    else:
        if issues := blocking_review_issues(output["issues"]):
            _retry(kernel, repo, owner, state, scene, task, proof, stage="literary", findings=issues, report_ref=proof["output_ref"])
            kernel.tasks.accept_in_transaction(repo.conn, task["task_id"], task["generation"])
            state["active"] = None
            return True
        child = ed["candidates"][ed["scene_index"]]
        child.update(literary_ref=proof["output_ref"], literary_task_id=task["task_id"])
        ed.pop("revision_findings", None)
        for field in ("retry_feedback_ref", "failed_draft_ref", "internal_resolution_refs", "verification_board", "verification_request"):
            ed.pop(field, None)
        ed["readings"].append({"scene_id": scene["scene_id"], "review_ref": proof["output_ref"],
                              "review_task_id": task["task_id"], "attempt_id": proof["attempt_id"],
                              "issues": len(output["issues"])})
        ed["scene_index"] += 1
        ed["phase"] = "write"
        if ed["scene_index"] == len(scenes):
            changed = any(c["changed"] for c in ed["candidates"])
            require(changed, "EDITORIAL_NO_PROGRESS", "The returned edition made no textual progress")
            payload = {"world_ref": state["binding"]["world_ref"], "run": state["run"], "volume": state["volume"],
                       "parent_binding_sha256": fingerprint(state["binding"]), **ed,
                       "controller": {"grant_id": owner.grant_id, "principal_id": owner.principal_id}}
            state["edition_ref"] = _artifact(kernel, repo, work, owner, "P6_RETURNED_EDITION_CANDIDATE", payload)
            ed["phase"] = "accept"
    kernel.tasks.accept_in_transaction(repo.conn, task["task_id"], task["generation"])
    state["active"] = None
    return True
