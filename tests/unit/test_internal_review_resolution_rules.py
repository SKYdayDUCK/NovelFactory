"""Internal answer rules preserve unknowns, exact versions and every question."""
import json
from copy import deepcopy
from uuid import NAMESPACE_URL, uuid5

import pytest

from novel_factory.application.internal_review_resolution import (
    AUTHOR_DECISION,
    RESOLVED,
    WORLD_INPUT,
    assignment,
    evidence_slots_instruction,
    parse_resolution,
    request_from_blocked,
    request_from_fact_report,
)
from novel_factory.domain.canonical import fingerprint
from novel_factory.domain.errors import DomainError


def uid(name):
    return str(uuid5(NAMESPACE_URL, "internal-question-fixture:" + name))


def ref(name, payload=None):
    return {"artifact_id": uid(name), "artifact_revision_id": uid(name + ":revision"),
            "sha256": fingerprint(payload if payload is not None else name)}


def case():
    wr = {"work_id": uid("work"), "branch_id": uid("branch"), "world_revision_id": uid("world")}
    target, diagnosis_id = "draft:" + uid("scene"), "reassessment:fixture:2"
    source = {"target": target, "world_ref": wr, "source_draft_ref": ref("draft"), "report_ref": ref("report"),
        "writer_task_id": uid("writer"), "writer_generation": 1, "revision_count": 76,
        "repair_allowance": {"revision_baseline": 75, "reassessment_baseline": 0}, "round": 2}
    record = {"request": source, "outcome": "REASSESSMENT_BLOCKED", "reason": "REASSESSMENT_NEEDS_INPUT",
        "task_id": uid("diagnosis"), "generation": 1, "attempt_id": uid("diagnosis-attempt"),
        "deferred_questions": ["核查该身份是否已经登记。", "核查该报告是否属于当前原稿。"]}
    node = {"stage": "reassessment", "state": "STRATEGY_CONSUMED", "task_id": record["task_id"],
            "generation": 1, "output_ref": ref("diagnosis", record)}
    writer = {"stage": "draft", "state": "STOPPED", "task_id": source["writer_task_id"], "generation": 1,
        "output_ref": source["source_draft_ref"], "revision_count": 76, "repair_allowance": source["repair_allowance"],
        "reassessments": [{"node_id": "old:1"}, {"node_id": diagnosis_id}]}
    board = {"world_ref": wr, "story_time": {"tick": 1, "phase": 0}, "nodes": {target: writer, diagnosis_id: node}}
    return board, target, node, record


def response(request, statuses=None):
    statuses = statuses or ["NO_CONFLICT"] * len(request["questions"])
    citation = {"kind": "ARTIFACT", "artifact_ref": request["report_ref"]}
    answers = [{"index": request["questions"][i]["index"], "status": status, "explanation": "合成固定版本核查依据。",
                "correction": "按已查证的本版事实提出局部修订。" if status == "CONFIRMED_CONFLICT" else None,
                "evidence_refs": [citation]} for i, status in enumerate(statuses)]
    return {"decision": json.dumps({"case_hash": request["case_hash"], "answers": answers, "method": "按本版证据独立复核。"}),
            "reason": "合成核查，不生成正文或世界事实。", "deferred_questions": [], "evidence_refs": [citation]}


def fact_case():
    board, target, _, _ = case()
    assertion = {"assertion_id": uid("assertion"), "span": {"start": 0, "end": 1, "unit": "UNICODE_CODEPOINT", "quote_sha256": "a" * 64},
        "summary": "合成登记情况待查。", "world_check": {"subject_id": uid("unknown")}, "knowledge_use": None}
    extraction = {"draft_ref": board["nodes"][target]["output_ref"], "task_id": uid("facts-task"), "generation": 1,
                  "assertions": [assertion]}
    report = {"status": "BLOCKED", "draft_ref": extraction["draft_ref"], "adopted_world_ref": board["world_ref"],
        "extraction_ref": ref("extraction", extraction), "hard_blockers": [
            {"code": "UNRESOLVED_STATE", "assertion_id": assertion["assertion_id"], "message": "合成未登记状态待核查。"},
            {"code": "STATE_CONFLICT", "assertion_id": assertion["assertion_id"], "message": "真实矛盾不能被本核查删除。"}]}
    facts = {"stage": "facts", "state": "STOPPED", "parents": [target], "task_id": extraction["task_id"], "generation": 1,
             "output_ref": ref("facts"), "attempt_id": uid("facts-attempt")}
    board["nodes"]["facts:fixture"] = facts
    return board, target, facts, ref("report", report), report, extraction


def test_case_freezes_original_questions_and_existing_second_slot():
    board, target, node, record = case()
    request = request_from_blocked(board, target, node, record)
    assert request["source_kind"] == "DIAGNOSIS" and request["diagnosis_round"] == 2
    assert request["questions"][0]["question"] == record["deferred_questions"][0]
    record["deferred_questions"][0] = "later mutation"
    assert request["questions"][0]["question"] != "later mutation"
    text = assignment(request)
    assert "实际获准MCP只读工具" in text and "UNREGISTERED" in text and "算术" in text
    assert len(board["nodes"][target]["reassessments"]) == 2


def test_new_and_corrected_assignments_share_receipt_slots_without_rewriting_frozen_input(monkeypatch):
    from types import SimpleNamespace

    from novel_factory.application import production_verification as flow
    from novel_factory.application.candidate_delivery import CandidateDelivery

    request = request_from_blocked(*case())
    original_assignment = assignment(request)
    frozen = {"assignment": original_assignment, "runtime_sha256": "1" * 64, "model_config_sha256": "2" * 64,
              "output_contract": {"name": "AgentDecisionOutput", "version": "agent"}}
    source = deepcopy(frozen)
    task = {"task_id": uid("new-verifier"), "generation": 1, "state": "RESULT_STORED",
            "role_id": "reviewer", "task_kind": "verification", "work_id": request["world_ref"]["work_id"]}
    node = {"stage": "internal_verification", "state": "ENROLLED", "task_id": task["task_id"], "internal_case": request}
    evidence = {"attempt_id": uid("new-attempt"), "output_ref": ref("candidate"), "input_ref": ref("input")}
    job = {"config": {"required_review_mcp": True, "production_node": "verification:" + request["case_hash"]}}
    repo = SimpleNamespace(one=lambda *args, **kwargs: job, change=lambda *args, **kwargs: None, now=lambda: None, conn=None)
    kernel = SimpleNamespace(artifact=lambda *args, **kwargs: {"payload": frozen}, tasks=SimpleNamespace(_emit=lambda *args: None))
    monkeypatch.setattr(CandidateDelivery, "audited_candidate", lambda *args, **kwargs: (evidence, {}))
    assert flow.recover_verification(kernel, repo, None, node, task,
                                     DomainError("INVALID_EVIDENCE", "Every answer must cite its actual state-query evidence"))
    instructions = evidence_slots_instruction()
    assert instructions in original_assignment and instructions in node["verification_repair"]["method"]
    assert "answers[index].evidence_refs至少1条必须" in instructions
    assert "nf_query_batch工具响应顶层receipt_ref" in instructions and "只在外层evidence_refs引用不够" in instructions
    assert frozen == source and node["internal_case"] == request


@pytest.mark.parametrize("conclusion", ["CONFIRMED_CONFLICT", "NO_CONFLICT", "STALE_REPORT"])
def test_documented_three_slot_examples_pass_query_gate_but_material_only_answers_do_not(monkeypatch, conclusion):
    from test_query_state_conclusions import proof
    from test_query_state_conclusions import ref as state_ref

    from novel_factory.application.production_verification import _query_gate

    arguments, receipts = proof(monkeypatch, status=conclusion,
                               spans=() if conclusion == "STALE_REPORT" else [(0, 2), (6, 8)])
    answer = arguments["parsed"]["answers"][0]
    if conclusion == "STALE_REPORT":
        request = arguments["request"]
        provenance_ref, old_report = state_ref("slot-provenance"), state_ref("slot-old-report")
        receipts[-1]["result"]["structuredContent"].update(finding_provenance_ref=provenance_ref,
            finding_provenance={"version": 1, "case_hash": request["case_hash"], "current_draft_ref": request["source_draft_ref"],
                "current_text_ref": state_ref("text"), "report_ref": request["report_ref"], "candidates": [{
                    "report_ref": old_report, "source_draft_ref": state_ref("older-draft"),
                    "matched_finding_indices": [0], "matches_current": False}]})
        answer["evidence_refs"] = [{"kind": "ARTIFACT", "artifact_ref": arguments["query_ref"]},
                                   {"kind": "ARTIFACT", "artifact_ref": provenance_ref},
                                   {"kind": "ARTIFACT", "artifact_ref": old_report}]
    assert len(answer["evidence_refs"]) == 3 and _query_gate(**arguments)
    answer["evidence_refs"].pop(0)
    with pytest.raises(DomainError, match="Every answer must cite its actual state-query evidence"):
        _query_gate(**arguments)


@pytest.mark.parametrize("statuses,outcome", [(["NO_CONFLICT", "STALE_REPORT"], RESOLVED),
    (["CONFIRMED_CONFLICT", "NO_CONFLICT"], RESOLVED), (["UNREGISTERED", "NO_CONFLICT"], WORLD_INPUT),
    (["EXTERNAL_DECISION_REQUIRED", "UNREGISTERED"], AUTHOR_DECISION)])
def test_all_questions_answered_without_turning_unknown_into_conflict(statuses, outcome):
    request = request_from_blocked(*case())
    assert parse_resolution(request, response(request, statuses))["outcome"] == outcome


def test_new_internal_question_cannot_bypass_the_frozen_typed_answers():
    request = request_from_blocked(*case())
    candidate = response(request)
    candidate["deferred_questions"] = ["仍缺一项内部登记资料。"]
    with pytest.raises(DomainError, match="Extra deferred questions"):
        parse_resolution(request, candidate)


def test_resolved_diagnosis_cannot_be_accepted_without_a_consumable_method():
    request = request_from_blocked(*case())
    candidate = response(request)
    parsed = json.loads(candidate["decision"])
    parsed.pop("method")
    candidate["decision"] = json.dumps(parsed)
    with pytest.raises(DomainError, match="concrete continuation"):
        parse_resolution(request, candidate)


@pytest.mark.parametrize("fault", ["missing", "index", "extra", "untrusted_citation", "long_method", "bad_status", "conflict_without_correction",
                                  "long_explanation", "long_correction", "too_many_citations"])
def test_incomplete_reordered_or_self_reported_answers_are_rejected(fault):
    request = request_from_blocked(*case())
    candidate = response(request)
    parsed = json.loads(candidate["decision"])
    if fault == "missing":
        parsed["answers"].pop()
    elif fault == "index":
        parsed["answers"][0]["index"] = 1
    elif fault == "extra":
        parsed["queries_completed"] = True
    elif fault == "untrusted_citation":
        candidate["evidence_refs"] = []
    elif fault == "long_method":
        parsed["method"] = "字" * 1001
    elif fault == "bad_status":
        parsed["answers"][0]["status"] = []
    elif fault == "long_explanation":
        parsed["answers"][0]["explanation"] = "字" * 257
    elif fault == "long_correction":
        parsed["answers"][0]["correction"] = "字" * 257
    elif fault == "too_many_citations":
        parsed["answers"][0]["evidence_refs"] *= 4
    else:
        parsed["answers"][0]["status"] = "CONFIRMED_CONFLICT"
    candidate["decision"] = json.dumps(parsed)
    with pytest.raises(DomainError):
        parse_resolution(request, candidate)


def test_duplicate_json_keys_and_tampered_case_are_rejected():
    request = request_from_blocked(*case())
    candidate = response(request)
    candidate["decision"] = '{"case_hash":"a","case_hash":"b","answers":[]}'
    with pytest.raises(DomainError, match="Duplicate"):
        parse_resolution(request, candidate)
    tampered = deepcopy(request)
    tampered["questions"][0]["question"] = "changed"
    with pytest.raises(DomainError, match="case changed"):
        assignment(tampered)


def test_assignment_contains_compact_binding_metadata_instead_of_full_question_text():
    board, target, node, record = case()
    record["deferred_questions"] = ["完整问题材料" * 500]
    request = request_from_blocked(board, target, node, record)
    prompt = assignment(request)
    metadata = json.loads(prompt.splitlines()[-1])
    assert metadata["case_hash"] == request["case_hash"] and metadata["question_indices"] == [0]
    assert "questions" not in metadata and record["deferred_questions"][0] not in prompt
    assert len(prompt) < 5000


def test_fact_case_preserves_unknown_scope_and_keeps_real_conflicts_separate():
    request = request_from_fact_report(*fact_case())
    assert request["source_kind"] == "FACT_RECONCILIATION"
    assert len(request["questions"]) == 1
    question = request["questions"][0]
    assert question["code"] == "UNRESOLVED_STATE" and question["span"] and question["world_check"]
    assert parse_resolution(request, response(request, ["UNREGISTERED"]))["outcome"] == WORLD_INPUT


@pytest.mark.parametrize("fault", ["old_draft", "wrong_report", "missing_attempt", "unknown_assertion"])
def test_fact_case_requires_exact_persisted_source(fault):
    board, target, facts, reference, report, extraction = fact_case()
    if fault == "old_draft":
        extraction["draft_ref"] = ref("old-draft")
    elif fault == "wrong_report":
        reference = ref("different-report")
    elif fault == "missing_attempt":
        facts.pop("attempt_id")
    else:
        report["hard_blockers"][0]["assertion_id"] = uid("absent-assertion")
        reference = ref("report", report)
    with pytest.raises(DomainError):
        request_from_fact_report(board, target, facts, reference, report, extraction)


def test_thirty_five_questions_are_five_bounded_windows_without_omissions():
    board, target, facts, _, report, extraction = fact_case()
    original = extraction["assertions"][0]
    extraction["assertions"] = [{**deepcopy(original), "assertion_id": uid("assertion:" + str(i))} for i in range(35)]
    report["hard_blockers"] = [{"code": "UNRESOLVED_STATE", "assertion_id": item["assertion_id"],
                               "message": "合成未决核查" + str(i)} for i, item in enumerate(extraction["assertions"])]
    report["extraction_ref"] = ref("extraction", extraction)
    reference = ref("report", report)
    cases = [request_from_fact_report(board, target, facts, reference, report, extraction, question_start=start)
             for start in range(0, 35, 8)]
    assert [len(request["questions"]) for request in cases] == [8, 8, 8, 8, 3]
    assert [q["index"] for request in cases for q in request["questions"]] == list(range(35))
    assert len({request["full_questions_sha256"] for request in cases}) == 1
    assert len({request["case_hash"] for request in cases}) == 5
    assert all(request["question_total"] == 35 and len(assignment(request)) <= 32000 for request in cases)
    assert all(parse_resolution(request, response(request))["outcome"] == RESOLVED for request in cases)
    assert cases[0] == request_from_fact_report(board, target, facts, reference, report, extraction)


@pytest.mark.parametrize("start,limit", [(1, 8), (-1, 8), (8, 8), (0, 1), (0, 9)])
def test_invalid_or_overlapping_question_window_is_rejected(start, limit):
    with pytest.raises(DomainError, match="canonical"):
        request_from_fact_report(*fact_case(), question_start=start, question_limit=limit)
