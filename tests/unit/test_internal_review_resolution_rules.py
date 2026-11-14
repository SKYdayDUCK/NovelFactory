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


@pytest.mark.parametrize("statuses,outcome", [(["NO_CONFLICT", "STALE_REPORT"], RESOLVED),
    (["CONFIRMED_CONFLICT", "NO_CONFLICT"], RESOLVED), (["UNREGISTERED", "NO_CONFLICT"], WORLD_INPUT),
    (["EXTERNAL_DECISION_REQUIRED", "UNREGISTERED"], AUTHOR_DECISION)])
def test_all_questions_answered_without_turning_unknown_into_conflict(statuses, outcome):
    request = request_from_blocked(*case())
    assert parse_resolution(request, response(request, statuses))["outcome"] == outcome


def test_new_internal_question_does_not_automatically_escalate_to_author():
    request = request_from_blocked(*case())
    candidate = response(request)
    candidate["deferred_questions"] = ["仍缺一项内部登记资料。"]
    assert parse_resolution(request, candidate)["outcome"] == WORLD_INPUT


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
