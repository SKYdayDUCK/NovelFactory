"""Per-question query correspondence and durable continuation preconditions."""
import json

import pytest
from test_internal_review_resolution_rules import case, fact_case, ref

from novel_factory.application import production_verification as flow
from novel_factory.application.internal_review_resolution import RESOLVED
from novel_factory.domain.errors import DomainError


def test_exhausted_revision_budget_is_checked_before_consuming_any_receipt(monkeypatch):
    board = {"nodes": {"draft": {"stage": "draft", "revision_count": 12, "state": "STOPPED"},
        "verification": {"stage": "internal_verification", "state": RESOLVED,
                         "internal_case": {"target": "draft", "source_kind": "DIAGNOSIS"}}}}
    monkeypatch.setattr(flow, "consume_resolution", lambda *a: pytest.fail("Exhausted repair consumed its one-time receipt"))
    assert flow.consume_verification(None, None, None, board) == "verification"
    assert board["nodes"]["draft"]["reason"] == "REPAIR_BUDGET_EXHAUSTED"
    assert board["nodes"]["verification"]["state"] == "VERIFICATION_HELD"


def test_queue_exhaustion_returns_one_changed_marker_for_persistence():
    board, target, _, record = case()
    board["nodes"][target]["chapter_id"] = "chapter"
    for i in range(2):
        board["nodes"]["old:" + str(i)] = {"stage": "internal_verification", "state": "VERIFICATION_CONSUMED",
            "internal_case": {"source_draft_ref": board["nodes"][target]["output_ref"], "question_start": 0}}
    kernel = type("Kernel", (), {"artifact": lambda *a, **k: {"payload": record}})()
    assert flow.queue_verification(kernel, None, board) == target
    assert board["nodes"][target]["reason"] == "INTERNAL_VERIFICATION_EXHAUSTED"
    assert flow.queue_verification(kernel, None, board) is None


def test_old_fact_report_progress_does_not_block_a_new_report_from_the_same_source_task():
    from types import SimpleNamespace

    board, target, facts, report_ref, report, extraction = fact_case()
    board["nodes"] = {target: board["nodes"][target], "facts:fixture": facts}
    board["nodes"][target].update(chapter_id="chapter", reassessments=[])
    facts["reason"] = "FACT_VERIFICATION_REQUIRED"
    board["nodes"]["old-verification"] = {"stage": "internal_verification", "state": "VERIFICATION_HELD", "internal_case": {
        "source_task_id": facts["task_id"], "source_ref": ref("old-wrapper"), "report_ref": ref("old-report"),
        "source_draft_ref": facts["output_ref"], "question_start": 0}}
    def artifact(repo, reference, work, *, kind):
        return {"payload": {"ROLLING_FACT_REPORT": {"reconciliation_ref": report_ref, "extraction_ref": report["extraction_ref"]},
                            "RECONCILIATION_REPORT": report, "ASSERTION_EXTRACTION": extraction}[kind]}
    kernel = SimpleNamespace(artifact=artifact)
    repo = SimpleNamespace(one=lambda *a, **k: {"attempt_id": facts["attempt_id"]})
    queued = flow.queue_verification(kernel, repo, board)
    assert queued.startswith("verification:")
    assert board["nodes"][queued]["internal_case"]["question_start"] == 0


def test_second_legal_cycle_can_finish_later_windows_after_cycle_cap_is_reached():
    from copy import deepcopy
    from types import SimpleNamespace

    from test_internal_review_resolution_rules import uid

    from novel_factory.domain.canonical import fingerprint

    board, target, facts, _, report, extraction = fact_case()
    board["nodes"] = {target: board["nodes"][target], "facts:fixture": facts}
    board["nodes"][target].update(chapter_id="chapter", reassessments=[])
    facts["reason"] = "FACT_VERIFICATION_REQUIRED"
    first = extraction["assertions"][0]
    extraction["assertions"] = [{**deepcopy(first), "assertion_id": uid("batch:" + str(i))} for i in range(16)]
    report["extraction_ref"] = ref("extraction", extraction)
    report["hard_blockers"] = [{"code": "UNRESOLVED_STATE", "assertion_id": row["assertion_id"], "message": "pending"}
                               for row in extraction["assertions"]]
    report_ref = ref("report", report)
    for i in range(2):
        board["nodes"]["cycle:" + str(i)] = {"stage": "internal_verification", "state": "VERIFICATION_CONSUMED", "internal_case": {
            "source_task_id": facts["task_id"] if i else uid("older-task"),
            "source_ref": facts["output_ref"] if i else ref("older-source"),
            "report_ref": report_ref if i else ref("older-report"), "source_draft_ref": board["nodes"][target]["output_ref"],
            "question_start": 0, "question_total": 16, "questions": [{} for _ in range(8)]}}
    payloads = {"ROLLING_FACT_REPORT": {"reconciliation_ref": report_ref, "extraction_ref": report["extraction_ref"]},
                "RECONCILIATION_REPORT": report, "ASSERTION_EXTRACTION": extraction}
    kernel = SimpleNamespace(artifact=lambda *a, kind, **k: {"payload": payloads[kind]})
    repo = SimpleNamespace(one=lambda *a, **k: {"attempt_id": facts["attempt_id"]})
    queued = flow.queue_verification(kernel, repo, board)
    assert queued.startswith("verification:") and board["nodes"][queued]["internal_case"]["question_start"] == 8
    assert fingerprint(extraction) == report["extraction_ref"]["sha256"]


def gate_case(monkeypatch, *, lookups=None, question=None, report_read=True, cite_report=True,
              answer_status="NO_CONFLICT", value_statuses=None, span_sources=()):
    from novel_factory.application import review_mcp

    draft, report, query = ref("draft"), ref("report"), ref("actual-query")
    lookups = lookups or [{"subject_id": "subject", "predicate": "owner"}]
    text = "甲有七两。乙只有三两。"
    text_ref = ref("current-text")
    references = [query, ref("material-draft"), ref("material-report")]
    def state_item(index, lookup):
        value = value_statuses[index] if value_statuses else "VALUE"
        known = value in {"VALUE", "NONE", "NOT_APPLICABLE"}
        return {"item_id": str(index), "status": "SUCCESS", "payload": {"kind": "STATE", "state_items": [{
            "subject_id": lookup["subject_id"], "predicate": lookup["predicate"], "answer": {
                "world_ref": {}, "story_time": lookup.get("at", {"tick": 1, "phase": 0}),
                "plane": lookup.get("plane", "WORLD"), "value_status": value,
                "evaluation_status": "EVALUATED" if known else "PENDING",
                "knowledge_status": "KNOWN" if known else "UNKNOWN", "freshness": "CURRENT",
                "truth_status": "DETERMINED" if known else "UNDETERMINED",
                "data": {"kind": "STRING", "value": "owner"} if value == "VALUE" else None}}]}}
    receipts = [{"tool": "nf_query_batch", "outcome": "OK", "arguments": {"lookups": lookups},
                 "result": {"structuredContent": {"all_items_succeeded": True, "items": [
                     state_item(i, lookup)
                     for i, lookup in enumerate(lookups)]}}},
                {"tool": "nf_review_material", "outcome": "OK", "arguments": {"artifact_ref": draft},
                 "result": {"structuredContent": {"draft_ref": draft, "text_ref": text_ref, "text": text}}}]
    if report_read:
        receipts.append({"tool": "nf_review_material", "outcome": "OK", "arguments": {"artifact_ref": report}, "result": {"structuredContent": {}}})
    else:
        references.pop()
    monkeypatch.setattr(review_mcp, "require_review_queries", lambda *a: receipts)
    kernel = type("Kernel", (), {"artifact": lambda *a, **k: {"payload": {
        "provider_response_json": json.dumps({"review_mcp_receipt_refs": references})}}})()
    request = {"world_ref": {}, "source_draft_ref": draft, "report_ref": report, "story_time": {"tick": 1, "phase": 0},
               "questions": [question or {"index": 0, "world_check": {"subject_id": "subject", "predicate": "owner", "plane": "WORLD",
                    "at": {"tick": 1, "phase": 0}, "actor_id": None, "known_at": None,
                    "expected_status": "VALUE", "expected_value": {"kind": "STRING", "value": "owner"}}}]}
    citations = [{"kind": "ARTIFACT", "artifact_ref": query}]
    if cite_report:
        citations.append({"kind": "ARTIFACT", "artifact_ref": report})
    citations.extend(span_sources)
    parsed = {"answers": [{"index": 0, "status": answer_status, "evidence_refs": citations}]}
    return lambda: flow._query_gate(kernel, None, request, {"work_id": "work"}, {"response_ref": {}}, query, parsed)


def test_real_query_for_another_question_does_not_satisfy_this_question(monkeypatch):
    check = gate_case(monkeypatch, lookups=[{"subject_id": "different-subject", "predicate": "owner"}])
    with pytest.raises(DomainError, match="does not cover"):
        check()


@pytest.mark.parametrize("changed", [{"predicate": "other"}, {"at": {"tick": 0, "phase": 0}}, {"plane": "BELIEF"}])
def test_query_predicate_time_and_plane_cannot_be_substituted(monkeypatch, changed):
    check = gate_case(monkeypatch, lookups=[{"subject_id": "subject", "predicate": "owner", **changed}])
    with pytest.raises(DomainError, match="does not cover"):
        check()


def test_bound_default_time_world_query_can_answer_its_matching_check(monkeypatch):
    assert gate_case(monkeypatch)()


def test_knowledge_check_needs_belief_actor_and_world_evidence_when_claimed_known(monkeypatch):
    question = {"index": 0, "knowledge_use": {"mode": "KNOWS", "subject_id": "subject", "predicate": "owner",
                                              "actor_id": "actor", "at": {"tick": 1, "phase": 0},
                                              "value": {"kind": "STRING", "value": "owner"}}}
    with pytest.raises(DomainError, match="does not cover"):
        gate_case(monkeypatch, question=question)()
    lookups = [{"subject_id": "subject", "predicate": "owner"},
               {"subject_id": "subject", "predicate": "owner", "plane": "BELIEF", "actor_id": "actor", "known_at": {"tick": 1, "phase": 0}}]
    assert gate_case(monkeypatch, question=question, lookups=lookups)()
    lookups[1]["actor_id"] = "someone-else"
    with pytest.raises(DomainError, match="does not cover"):
        gate_case(monkeypatch, question=question, lookups=lookups)()


def test_unstructured_question_requires_actual_current_material_and_a_citation(monkeypatch):
    question = {"index": 0, "question": "Which report version applies?"}
    assert gate_case(monkeypatch, question=question)()
    with pytest.raises(DomainError, match="must cite"):
        gate_case(monkeypatch, question=question, cite_report=False)()
    with pytest.raises(DomainError, match="actual current"):
        gate_case(monkeypatch, question=question, report_read=False)()


def test_unknown_question_cannot_borrow_another_batch_items_known_value(monkeypatch):
    lookups = [{"subject_id": "someone-else", "predicate": "owner"}, {"subject_id": "subject", "predicate": "owner"}]
    with pytest.raises(DomainError, match="conflict needs"):
        gate_case(monkeypatch, lookups=lookups, value_statuses=["VALUE", "UNRESOLVED"], answer_status="CONFIRMED_CONFLICT")()
    assert gate_case(monkeypatch, lookups=lookups, value_statuses=["UNRESOLVED", "NONE"], answer_status="CONFIRMED_CONFLICT")()


def test_textual_arithmetic_conflict_needs_two_distinct_exact_current_spans(monkeypatch):
    from novel_factory.domain.reconciliation import span_for

    text = "甲有七两。乙只有三两。"
    split = text.index("乙")
    first = {"kind": "ARTIFACT", "artifact_ref": ref("current-text"), "span": span_for(text, 0, split)}
    second = {"kind": "ARTIFACT", "artifact_ref": ref("current-text"), "span": span_for(text, split, len(text))}
    question = {"index": 0, "question": "Compare these two exact amounts in this version."}
    for spans in ([first], [first, first]):
        with pytest.raises(DomainError, match="two independent"):
            gate_case(monkeypatch, question=question, cite_report=False, answer_status="CONFIRMED_CONFLICT", span_sources=spans)()
    assert gate_case(monkeypatch, question=question, cite_report=False, answer_status="CONFIRMED_CONFLICT", span_sources=[first, second])()
    wrong = {**second, "artifact_ref": ref("older-text")}
    with pytest.raises(DomainError, match="current text_ref"):
        gate_case(monkeypatch, question=question, cite_report=False, answer_status="CONFIRMED_CONFLICT", span_sources=[first, wrong])()
    wrong = {**second, "span": {**second["span"], "quote_sha256": "0" * 64}}
    with pytest.raises(DomainError, match="span differs"):
        gate_case(monkeypatch, question=question, cite_report=False, answer_status="CONFIRMED_CONFLICT", span_sources=[first, wrong])()


def correction_case(monkeypatch):
    from types import SimpleNamespace

    from novel_factory.application.candidate_delivery import CandidateDelivery
    from novel_factory.application.internal_review_resolution import request_from_blocked

    request = request_from_blocked(*case())
    task = {"task_id": "synthetic-return", "generation": 1, "work_id": request["world_ref"]["work_id"],
            "role_id": "reviewer", "task_kind": "verification", "state": "RESULT_STORED"}
    node = {"stage": "internal_verification", "state": "ENROLLED", "task_id": task["task_id"], "internal_case": request}
    changes, events = [], []
    job = {"config": {"required_review_mcp": True, "production_node": "verification:" + request["case_hash"]}}
    repo = SimpleNamespace(one=lambda *a, **k: job, change=lambda *a: changes.append(a), now=lambda: "synthetic-clock", conn=None)
    frozen = {"assignment": json.dumps({"case_hash": request["case_hash"]}), "runtime_sha256": "role-version",
              "model_config_sha256": "model-version", "output_contract": {"name": "AgentDecisionOutput", "version": "agent"}}
    kernel = SimpleNamespace(artifact=lambda *a, **k: {"payload": frozen}, tasks=SimpleNamespace(_emit=lambda *a: events.append(a)))
    def audited(self, repo, task, **kwargs):
        return {"input_ref": {}, "output_ref": {}, "attempt_id": task["task_id"] + ":attempt"}, {}
    monkeypatch.setattr(CandidateDelivery, "audited_candidate", audited)
    return kernel, repo, node, task, changes, events


def test_internal_output_format_corrections_are_finite_and_keep_same_case(monkeypatch):
    from copy import deepcopy

    kernel, repo, node, task, changes, events = correction_case(monkeypatch)
    frozen_case = deepcopy(node["internal_case"])
    for index, code in enumerate(("INVALID_ARGUMENT", "INVALID_EVIDENCE", "INVALID_ARGUMENT")):
        task = {**task, "task_id": "return:" + str(index)}
        node.update(state="ENROLLED", task_id=task["task_id"])
        assert flow.recover_verification(kernel, repo, None, node, task, DomainError(code, "synthetic diagnostic"))
        if index < 2:
            assert node["state"] == "PENDING" and node["reason"] == "INTERNAL_VERIFICATION_REPAIR_REQUIRED"
            assert node["verification_repair"]["runtime_sha256"] == "role-version"
            assert flow.recover_verification(kernel, repo, None, node, task, DomainError(code, "same failed result"))
        else:
            assert node["state"] == "STOPPED" and node["reason"] == "INTERNAL_VERIFICATION_REPAIR_EXHAUSTED"
    assert node["internal_case"] == frozen_case and len(node["evidence_repairs"]) == len(changes) == len(events) == 2


def test_internal_correction_cannot_bypass_missing_native_delivery_proof(monkeypatch):
    from copy import deepcopy

    from novel_factory.application.candidate_delivery import CandidateDelivery

    kernel, repo, node, task, changes, _ = correction_case(monkeypatch)
    before = deepcopy(node)
    def blocked(*args, **kwargs):
        raise DomainError("CALL_RECONCILIATION_REQUIRED", "synthetic missing native proof")
    monkeypatch.setattr(CandidateDelivery, "audited_candidate", blocked)
    with pytest.raises(DomainError, match="missing native proof"):
        flow.recover_verification(kernel, repo, None, node, task, DomainError("INVALID_ARGUMENT", "bad JSON"))
    assert node == before and not changes
