"""A mandatory issue must anchor the particular draft named as its subject."""
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from novel_factory.application import review_mcp
from novel_factory.application.review_evidence import require_review_evidence
from novel_factory.domain.errors import DomainError
from novel_factory.domain.reconciliation import span_for


def ref():
    return {"artifact_id": str(uuid4()), "artifact_revision_id": str(uuid4()), "sha256": uuid4().hex * 2}


@pytest.mark.parametrize("anchor_target", ["subject", "another_draft"])
@pytest.mark.parametrize("hard", [False, True])
def test_issue_cannot_borrow_text_anchors_from_another_assigned_draft(monkeypatch, anchor_target, hard):
    first, second, text_a, text_b, input_ref, response_ref, query_ref = [ref() for _ in range(7)]
    bodies = {text_a["sha256"]: "甲处证据。另一个句子。", text_b["sha256"]: "乙处证据。另一个句子。"}
    rows = {input_ref["sha256"]: {"payload": {"material_refs": [first, second]}},
            response_ref["sha256"]: {"payload": {"provider_response_json": json.dumps({"review_mcp_receipt_refs": [query_ref]})}},
            first["sha256"]: {"kind": "NARRATIVE_DRAFT", "payload": {"text_ref": text_a}},
            second["sha256"]: {"kind": "NARRATIVE_DRAFT", "payload": {"text_ref": text_b}}}
    kernel = SimpleNamespace(artifact=lambda repo, artifact, work, **kwargs: rows[artifact["sha256"]],
                             objects=SimpleNamespace(read=lambda sha: bodies[sha].encode("utf-8")))
    monkeypatch.setattr(review_mcp, "require_review_queries", lambda *args: [
        {"tool": "nf_query_batch", "outcome": "OK", "result": {"value_status": "UNKNOWN"}}])
    target = text_a if anchor_target == "subject" else text_b
    body = bodies[target["sha256"]]
    sources = [{"kind": "ARTIFACT", "artifact_ref": target, "span": span_for(body, 0, 5)}]
    if hard:
        sources.extend([{"kind": "ARTIFACT", "artifact_ref": target, "span": span_for(body, 5, len(body))},
                        {"kind": "ARTIFACT", "artifact_ref": query_ref}])
    issue = {"subject_ref": first, "suggested_change": "Resolve this synthetic inconsistency", "evidence_refs": sources,
             "category": "HARD_CONSISTENCY" if hard else "LITERARY", "issue_kind": "HARD_CONFLICT" if hard else "CHANGE_REQUEST",
             "severity": "BLOCKER", "disposition": "OPEN"}
    def gate():
        return require_review_evidence(kernel, None, {"work_id": "synthetic"},
            {"input_ref": input_ref, "response_ref": response_ref}, {"issues": [issue]})
    if anchor_target == "subject":
        assert gate()
    else:
        with pytest.raises(DomainError) as rejected:
            gate()
        assert rejected.value.code == "REVIEW_EVIDENCE_REQUIRED"
