"""P2-05 exact source coverage and evidence-bound aesthetic disagreement."""
from copy import deepcopy
from uuid import uuid4

import pytest

from novel_factory.contracts.registry import validate_contract
from novel_factory.domain.canonical import fingerprint
from novel_factory.domain.editorial import (
    check_review_matrix, evidence_was_delivered, issue_fingerprint,
    next_edit_phase, reading_coverage, text_span, validate_issue,
)
from novel_factory.domain.errors import DomainError


def ref(seed):
    return {"artifact_id": str(uuid4()), "artifact_revision_id": str(uuid4()), "sha256": fingerprint(seed)}


def coverage(text, artifact, start, end, mode="FULL_TEXT"):
    return {"artifact_ref": artifact, "source_length_codepoints": len(text), "read_mode": mode,
            "spans": [] if mode == "SUMMARY" else [text_span(text, start, end)]}


def test_exact_unicode_chunk_union_and_unread_gap_are_not_normalized():
    text = "她笑了。\r\n可那并非宽恕。e\u0301👩\u200d🚒"
    original = ref(text)
    split = 7
    first = coverage(text, original, 0, split)
    last = coverage(text, original, split + 1, len(text))
    assert reading_coverage(text, original, [first, last])["gaps"] == [[split, split + 1]]
    bridge = coverage(text, original, split - 1, split + 2)
    result = reading_coverage(text, original, [first, last, bridge])
    assert result["complete"] and result["covered_codepoints"] == len(text)
    assert result["covered_ranges"] == [[0, len(text)]]
    assert not reading_coverage(text, ref("same display, new revision"), [first, last, bridge])["complete"]
    wrong = deepcopy(first); wrong["spans"][0]["quote_sha256"] = fingerprint("NFC or newline normalization")
    with pytest.raises(DomainError, match="exact original"):
        reading_coverage(text, original, [wrong])


def test_summary_and_excerpt_never_satisfy_full_original_reading():
    text = "一整段未被真正送出的原文。"; artifact = ref(text)
    summary = coverage(text, artifact, 0, len(text), "SUMMARY")
    excerpt = coverage(text, artifact, 0, len(text), "EXCERPT")
    assert reading_coverage(text, artifact, [summary, excerpt])["covered_codepoints"] == 0
    quote = text_span(text, 2, 6)
    assert evidence_was_delivered(artifact, quote, [excerpt])
    assert not evidence_was_delivered(artifact, quote, [summary])
    with pytest.raises(DomainError):
        text_span(text, True, 3)
    with pytest.raises(DomainError):
        text_span(text, 0, len(text) + 1)


def literary_issue():
    artifact = ref("她笑着，把旧信收回怀里。")
    return {"issue_id": str(uuid4()), "category": "LITERARY", "severity": "SUGGESTION", "subject_ref": artifact,
        "description": "悲伤中的笑可能是短暂的释然，也可能遮掩愤怒。", "evidence_refs": [{"kind": "ARTIFACT", "artifact_ref": artifact,
        "span": text_span("她笑着，把旧信收回怀里。", 0, 3)}], "suggested_change": "结合后文再决定是否保留这处双重情绪。",
        "disposition": "OPEN", "issue_kind": "LITERARY_CHOICE"}


def test_mixed_emotion_and_distinct_voice_are_not_hardcoded_errors():
    issue = literary_issue()
    validate_contract("ReviewIssue", issue)
    validate_issue(issue)
    same = deepcopy(issue); same["issue_id"] = str(uuid4())
    assert issue_fingerprint(same) == issue_fingerprint(issue)
    same["description"] = "另一位叙述者可以选择克制，不必同样说笑。"
    validate_issue(same)
    assert issue_fingerprint(same) != issue_fingerprint(issue)
    misclassified = deepcopy(issue); misclassified["category"] = "HARD_CONSISTENCY"
    with pytest.raises(DomainError) as error:
        validate_issue(misclassified)
    assert error.value.code == "INVALID_REVIEW_CLASSIFICATION"
    unsupported = deepcopy(issue); unsupported["evidence_refs"] = []
    with pytest.raises(DomainError) as evidence:
        validate_issue(unsupported)
    assert evidence.value.code == "REVIEW_EVIDENCE_REQUIRED"


def test_structure_precedes_seams_and_polishing_with_bounded_same_phase():
    next_edit_phase(None, "STRUCTURE")
    next_edit_phase("STRUCTURE", "STRUCTURE")
    next_edit_phase("STRUCTURE", "SEAM")
    next_edit_phase("SEAM", "LANGUAGE")
    for previous, phase in [(None, "LANGUAGE"), ("STRUCTURE", "LANGUAGE"), ("LANGUAGE", "STRUCTURE")]:
        with pytest.raises(DomainError) as error:
            next_edit_phase(previous, phase)
        assert error.value.code == "EDIT_ORDER_VIOLATION"


def test_review_matrix_requires_every_adjacent_pair_and_entire_ordered_volume():
    drafts = [ref("a"), ref("b"), ref("c")]
    binding = {"ordered_draft_refs": drafts}
    def report(scale, dimension, scope):
        return {"submission": {"scale": scale, "dimension": dimension, "scope_draft_refs": scope}}
    reports = [report("SCENE", "FACT", [draft]) for draft in drafts]
    reports += [report("CHAPTER", "EPISTEMIC", drafts), report("VOLUME", "LITERARY", drafts)]
    reports += [report("SEAM", "CONTINUITY", drafts[:2])]
    with pytest.raises(DomainError) as missing:
        check_review_matrix(binding, reports)
    assert missing.value.code == "REVIEW_COVERAGE_INCOMPLETE"
    reports.append(report("SEAM", "CONTINUITY", drafts[1:]))
    check_review_matrix(binding, reports)
    reports[4]["submission"]["scope_draft_refs"] = list(reversed(drafts))
    with pytest.raises(DomainError):
        check_review_matrix(binding, reports)


def test_editorial_contract_rejects_extra_authority_and_integer_arrangement():
    binding = {"world_ref": {key: str(uuid4()) for key in ("work_id", "branch_id", "world_revision_id")},
        "plan_ref": ref("plan"), "ordered_draft_refs": [ref("draft")], "reconciliation_refs": [ref("report")],
        "ordered_acceptance_refs": [], "narrative_revision_id": str(uuid4()), "arrangement_version": "arrangement-1",
        "disclosure_revision_id": str(uuid4()), "style_refs": [], "literary_refs": []}
    validate_contract("EditorialBinding", binding, "editorial")
    broken = deepcopy(binding); broken["arrangement_version"] = 1
    with pytest.raises(DomainError):
        validate_contract("EditorialBinding", broken, "editorial")
    broken = deepcopy(binding); broken["accept_world_effects"] = True
    with pytest.raises(DomainError):
        validate_contract("EditorialBinding", broken, "editorial")
