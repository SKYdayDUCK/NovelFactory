"""A trusted query receipt's state is not a model-selectable registration verdict."""
import json
from copy import deepcopy
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

import pytest

from novel_factory.application import review_mcp
from novel_factory.application.internal_review_resolution import WORLD_EVIDENCE
from novel_factory.application.production_verification import (
    _author_protection,
    _query_gate,
    consume_verification,
)
from novel_factory.domain.canonical import fingerprint
from novel_factory.domain.errors import DomainError
from novel_factory.domain.reconciliation import span_for


def uid(name):
    return str(uuid5(NAMESPACE_URL, "state-conclusion-fixture:" + name))


def ref(name):
    return {"artifact_id": uid(name), "artifact_revision_id": uid(name + ":revision"), "sha256": fingerprint(name)}


def proof(monkeypatch, *, state="PENDING", status="NO_CONFLICT", spans=(), error=None):
    wr = {"work_id": uid("work"), "branch_id": uid("branch"), "world_revision_id": uid("world")}
    at = {"tick": 1, "phase": 0}
    check = {"subject_id": uid("subject"), "predicate": "flag", "at": at, "plane": "WORLD",
             "actor_id": None, "known_at": None, "expected_status": "VALUE", "expected_value": {"kind": "BOOLEAN", "value": True}}
    request = {"world_ref": wr, "story_time": at, "case_hash": "a" * 64, "source_draft_ref": ref("draft"),
               "report_ref": ref("report"), "questions": [{"index": 0, "world_check": check}]}
    selector = {key: check[key] for key in ("subject_id", "predicate", "at", "plane")}
    known = state in {"VALUE", "NONE", "NOT_APPLICABLE"}
    value = state if known else "UNRESOLVED"
    answer = {"world_ref": wr, "story_time": at, "plane": "WORLD", "evaluation_status": "PENDING" if state == "PENDING" else "EVALUATED",
              "knowledge_status": "KNOWN" if known else "UNKNOWN", "value_status": value,
              "freshness": "CURRENT", "truth_status": "DETERMINED" if known else "UNDETERMINED",
              "data": {"kind": "BOOLEAN", "value": True} if state == "VALUE" else None}
    item = {"item_id": "0", "status": "SUCCESS", "payload": {"kind": "STATE", "state_items": [
        {"subject_id": check["subject_id"], "predicate": check["predicate"], "answer": answer}]}}
    if error:
        item = {"item_id": "0", "status": "ERROR", "error": {"code": error}}
    text = "十二箱总数。十箱已发。二箱待发。"
    query_ref, material_ref, report_receipt_ref = ref("query"), ref("material"), ref("report-read")
    receipts = [{"tool": "nf_query_batch", "outcome": "OK", "arguments": {"lookups": [selector]},
                 "result": {"structuredContent": {"items": [item], "all_items_succeeded": not bool(error)}}},
        {"tool": "nf_review_material", "outcome": "OK", "arguments": {"artifact_ref": request["source_draft_ref"]},
         "result": {"structuredContent": {"draft_ref": request["source_draft_ref"], "text_ref": ref("text"), "text": text}}},
        {"tool": "nf_review_material", "outcome": "OK", "arguments": {"artifact_ref": request["report_ref"]},
         "result": {"structuredContent": {"source_ref": request["report_ref"]}}}]
    citations = [{"kind": "ARTIFACT", "artifact_ref": query_ref}, *[
        {"kind": "ARTIFACT", "artifact_ref": ref("text"), "span": span_for(text, start, end)} for start, end in spans]]
    if not spans:
        citations.append({"kind": "ARTIFACT", "artifact_ref": request["report_ref"]})
    parsed = {"answers": [{"index": 0, "status": status, "evidence_refs": citations}]}
    response = {"provider_response_json": json.dumps({"review_mcp_receipt_refs": [query_ref, material_ref, report_receipt_ref]})}
    def artifact(*args, kind=None, **kwargs):
        if kind == "NARRATIVE_DRAFT":
            return {"payload": {"scene_ref": ref("scene")}}
        if kind == "SCENE_PLAN":
            return {"payload": {"scene_contract": {"story_interval": {
                "start": at, "end": {"tick": 1, "phase": 1}, "bounds": "[)"}}}}
        return {"payload": response}
    kernel = SimpleNamespace(artifact=artifact)
    monkeypatch.setattr(review_mcp, "require_review_queries", lambda *args: receipts)
    return {"kernel": kernel, "repo": None, "request": request, "task": {"work_id": wr["work_id"]},
            "evidence": {"response_ref": ref("response")}, "query_ref": query_ref, "parsed": parsed}, receipts


@pytest.mark.parametrize("state,status,accepted", [
    ("PENDING", "PENDING_EVALUATION", True), ("PENDING", "UNKNOWN", False), ("PENDING", "UNREGISTERED", False),
    ("PENDING", "NO_CONFLICT", False), ("PENDING", "CONFIRMED_CONFLICT", False),
    ("UNKNOWN", "UNKNOWN", True), ("UNKNOWN", "UNREGISTERED", False), ("UNKNOWN", "PENDING_EVALUATION", False),
    ("UNKNOWN", "NO_CONFLICT", False), ("UNKNOWN", "CONFIRMED_CONFLICT", False),
    ("VALUE", "NO_CONFLICT", True), ("VALUE", "CONFIRMED_CONFLICT", False), ("VALUE", "UNREGISTERED", False),
    ("VALUE", "PENDING_EVALUATION", False), ("NONE", "CONFIRMED_CONFLICT", True), ("NONE", "NO_CONFLICT", False)])
def test_query_state_controls_which_conclusion_has_actual_evidence(monkeypatch, state, status, accepted):
    arguments, _ = proof(monkeypatch, state=state, status=status)
    if accepted:
        assert _query_gate(**arguments)
        row = arguments["parsed"]["query_evidence"][0]
        assert row["status"] == state and row["selector"]["at"] == {"tick": 1, "phase": 0}
    else:
        with pytest.raises(DomainError, match="Unknown|Pending|conflict|registration|evaluation|knowledge"):
            _query_gate(**arguments)


@pytest.mark.parametrize("error,accepted", [("ENTITY_NOT_FOUND", True), ("UNKNOWN_PREDICATE", True),
    ("VIEW_DENIED", False), ("INVALID_ARGUMENT", False), ("PROCESS_NOT_EVALUATED", False)])
def test_missing_registration_requires_a_specific_actual_item_error(monkeypatch, error, accepted):
    arguments, _ = proof(monkeypatch, status="UNREGISTERED", error=error)
    if accepted:
        assert _query_gate(**arguments)
    else:
        with pytest.raises(DomainError, match="registration"):
            _query_gate(**arguments)


@pytest.mark.parametrize("spans,accepted", [([(0, 2), (6, 8)], True), ([(0, 2), (0, 2)], False),
    ([(0, 4), (1, 3)], False), ([(0, 2)], False)])
@pytest.mark.parametrize("status", ["NO_CONFLICT", "CONFIRMED_CONFLICT"])
def test_text_only_arithmetic_can_be_decided_without_advancing_world(monkeypatch, spans, accepted, status):
    arguments, _ = proof(monkeypatch, status=status, spans=spans)
    if accepted:
        assert _query_gate(**arguments)
    else:
        with pytest.raises(DomainError, match="independent"):
            _query_gate(**arguments)


@pytest.mark.parametrize("field,value", [("world_ref", {}), ("story_time", {"tick": 0, "phase": 9}), ("plane", "BELIEF")])
def test_state_answer_cannot_change_the_fixed_cut_time_or_plane(monkeypatch, field, value):
    arguments, receipts = proof(monkeypatch, state="VALUE")
    receipts[0]["result"]["structuredContent"]["items"][0]["payload"]["state_items"][0]["answer"][field] = value
    with pytest.raises(DomainError, match="fixed world"):
        _query_gate(**arguments)


@pytest.mark.parametrize("fault", [None, "current_report", "no_specific_report", "other_case"])
def test_stale_report_needs_exact_old_report_and_current_bound_provenance(monkeypatch, fault):
    arguments, receipts = proof(monkeypatch, status="STALE_REPORT")
    request = arguments["request"]
    old_report, provenance_ref = ref("old-report"), ref("provenance")
    old = {"report_ref": old_report, "reconciliation_ref": ref("old-reconciliation"),
           "source_draft_ref": ref("old-draft"), "matched_finding_indices": [0], "matches_current": False}
    provenance = {"version": 1, "case_hash": request["case_hash"], "current_draft_ref": request["source_draft_ref"],
                  "current_text_ref": ref("text"), "report_ref": request["report_ref"],
                  "candidates": [old, {**deepcopy(old), "report_ref": ref("current-report"),
                    "source_draft_ref": request["source_draft_ref"], "matches_current": True}]}
    if fault == "current_report":
        old["matches_current"] = True
    if fault == "other_case":
        provenance["case_hash"] = "b" * 64
    receipts[-1]["result"]["structuredContent"].update(finding_provenance=provenance, finding_provenance_ref=provenance_ref)
    arguments["parsed"]["answers"][0]["evidence_refs"] = [{"kind": "ARTIFACT", "artifact_ref": arguments["query_ref"]},
        {"kind": "ARTIFACT", "artifact_ref": provenance_ref},
        {"kind": "ARTIFACT", "artifact_ref": request["report_ref"] if fault == "no_specific_report" else old_report}]
    if fault is None:
        assert _query_gate(**arguments)
    else:
        with pytest.raises(DomainError, match="stale report"):
            _query_gate(**arguments)


@pytest.mark.parametrize("outcome,state", [(WORLD_EVIDENCE, "WAITING_WORLD_EVIDENCE")])
def test_unresolved_world_wait_has_an_explicit_persisted_reason(outcome, state):
    writer = {"stage": "draft", "state": "STOPPED"}
    node = {"stage": "internal_verification", "state": outcome, "internal_case": {"target": "writer"}}
    board = {"nodes": {"writer": writer, "verification": node}}
    assert consume_verification(None, None, None, board) == "verification"
    assert node["state"] == state and node["reason"] == outcome and writer["reason"] == outcome


@pytest.mark.parametrize("fault,accepted", [(None, True), ("long_clause_hash", True), ("empty", False),
    ("not_read", False), ("other_volume", False), ("tampered_clause", False), ("no_specific_clause", False),
    ("wrong_hash", False), ("not_cited", False), ("wrong_projection", False)])
def test_author_decision_requires_current_actually_read_specific_protected_clause(fault, accepted):
    description = "测试保护：结局签字须由作者决定。" if fault not in {"long_clause_hash", "wrong_hash"} else "受保护的完整条款。" * 30
    clause = {"commitment_id": uid("protected-clause"), "description": description,
              "policy_ref": "registered-policy", "evidence_refs": []}
    contract_ref = ref("current-contract")
    contract = {"volume_id": uid("volume"), "contract_revision_id": uid("contract-version"),
                "protected_commitments": [] if fault == "empty" else [clause]}
    payloads = {"NARRATIVE_DRAFT": {"scene_ref": ref("scene")}, "SCENE_PLAN": {"plan_ref": ref("plan")},
                "PLAN_REVISION": {"volume_contract_ref": contract_ref, "volume_id": uid("other") if fault == "other_volume" else uid("volume")},
                "VOLUME_CONTRACT": contract}
    kernel = SimpleNamespace(artifact=lambda *args, kind, **kwargs: {"payload": payloads[kind]})
    material = {"projection": "REGISTERED_AUTHOR_PROTECTION", "source_ref": contract_ref, **deepcopy(contract)}
    if fault == "tampered_clause":
        material["protected_commitments"] = ["伪造受保护条款。"]
    if fault == "wrong_projection":
        material["projection"] = "MODEL_CLAIM"
    receipt = {"tool": "nf_review_material", "outcome": "OK", "arguments": {"artifact_ref": contract_ref},
               "result": {"structuredContent": material}}
    explanation = ("引用作者条款：" + description) if len(description) <= 128 else f"protected_commitments[0] sha256:{fingerprint(clause)}"
    if fault == "wrong_hash":
        explanation = "protected_commitments[0] sha256:" + "0" * 64
    if fault == "no_specific_clause":
        explanation = "疑似作者保护，需要询问。"
    answer = {"explanation": explanation, "evidence_refs": [] if fault == "not_cited" else [
        {"kind": "ARTIFACT", "artifact_ref": contract_ref}]}
    assert _author_protection(kernel, None, {"source_draft_ref": ref("draft")}, {"work_id": uid("work")},
                              answer, [] if fault == "not_read" else [receipt]) is accepted
