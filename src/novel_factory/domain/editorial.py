"""Exact original-text coverage and evidence rules, without aesthetic formulas."""
from __future__ import annotations

from hashlib import sha256

from novel_factory.domain.canonical import fingerprint
from novel_factory.domain.errors import DomainError


def text_span(text: str, start: int, end: int) -> dict:
    if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text):
        raise DomainError("INVALID_TEXT_SPAN", "Text span must address the exact original code points")
    return {"start": start, "end": end, "unit": "UNICODE_CODEPOINT",
            "quote_sha256": sha256(text[start:end].encode("utf-8")).hexdigest()}


def verify_span(text: str, span: dict) -> None:
    if text_span(text, span["start"], span["end"]) != span:
        raise DomainError("INVALID_TEXT_SPAN", "Text evidence does not match the exact original bytes")


def reading_coverage(text: str, artifact_ref: dict, records: list[dict]) -> dict:
    """Union actual FULL_TEXT delivery spans; summaries/excerpts count as zero."""
    intervals = []
    for record in records:
        if record["artifact_ref"] != artifact_ref:
            continue
        if record["source_length_codepoints"] != len(text):
            raise DomainError("INVALID_READING_COVERAGE", "Reading receipt refers to a different text length")
        for span in record["spans"]:
            verify_span(text, span)
            if record["read_mode"] == "FULL_TEXT":
                intervals.append((span["start"], span["end"]))
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    gaps, cursor = [], 0
    for start, end in merged:
        if start > cursor:
            gaps.append([cursor, start])
        cursor = end
    if cursor < len(text):
        gaps.append([cursor, len(text)])
    return {"complete": bool(text) and not gaps, "covered_ranges": merged, "gaps": gaps,
            "covered_codepoints": sum(end - start for start, end in merged),
            "source_length_codepoints": len(text)}


def evidence_was_delivered(artifact_ref: dict, span: dict, records: list[dict]) -> bool:
    """Evidence may use an excerpt, but cannot claim undelivered code points."""
    intervals = sorted((s["start"], s["end"]) for r in records
                       if r["artifact_ref"] == artifact_ref and r["read_mode"] != "SUMMARY"
                       for s in r["spans"])
    cursor = span["start"]
    for start, end in intervals:
        if end <= cursor:
            continue
        if start > cursor:
            break
        cursor = end
        if cursor >= span["end"]:
            return True
    return False


def validate_issue(issue: dict) -> None:
    if not issue["evidence_refs"]:
        raise DomainError("REVIEW_EVIDENCE_REQUIRED", "A review issue must identify evidence")
    if issue["disposition"] != "OPEN":
        raise DomainError("EDITORIAL_DECISION_REQUIRED", "Reviewer cannot pre-resolve their own issue")
    if issue["issue_kind"] == "LITERARY_CHOICE" and issue["category"] == "HARD_CONSISTENCY":
        raise DomainError("INVALID_REVIEW_CLASSIFICATION", "Literary choices are not world invariants")


def issue_fingerprint(issue: dict) -> str:
    # IDs do not make the same unchanged objection new evidence. Paraphrased
    # semantic duplicates remain an editor judgment, not a keyword blacklist.
    return fingerprint({key: issue[key] for key in ("category", "issue_kind", "subject_ref", "description", "evidence_refs")})


def next_edit_phase(previous: str | None, phase: str) -> None:
    phases = ["STRUCTURE", "SEAM", "LANGUAGE"]
    if phase not in phases:
        raise DomainError("INVALID_EDIT_PHASE", "Unknown editorial phase")
    if previous is None and phase != "STRUCTURE":
        raise DomainError("EDIT_ORDER_VIOLATION", "Structural decisions must precede polishing")
    if previous is not None and phases.index(phase) not in {phases.index(previous), phases.index(previous) + 1}:
        raise DomainError("EDIT_ORDER_VIOLATION", "Editorial phases cannot be skipped or silently reversed")


def check_review_matrix(binding: dict, reports: list[dict]) -> None:
    ordered = [r["artifact_revision_id"] for r in binding["ordered_draft_refs"]]
    covered = {}
    for report in reports:
        submission = report["submission"]
        scoped = {r["artifact_revision_id"] for r in submission["scope_draft_refs"]}
        covered.setdefault(("scale", submission["scale"]), set()).update(scoped)
        covered.setdefault(("dimension", submission["dimension"]), set()).update(scoped)
    required = [("scale", "SCENE"), ("scale", "CHAPTER"), ("scale", "VOLUME"),
                ("dimension", "FACT"), ("dimension", "EPISTEMIC"), ("dimension", "LITERARY")]
    if any(covered.get(kind, set()) != set(ordered) for kind in required):
        raise DomainError("REVIEW_COVERAGE_INCOMPLETE", "Scene, chapter, volume, fact, epistemic and literary review are required")
    for left, right in zip(ordered, ordered[1:]):
        if not any(r["submission"]["scale"] == "SEAM"
                   and [left, right] == [d["artifact_revision_id"] for d in r["submission"]["scope_draft_refs"]]
                   for r in reports):
            raise DomainError("REVIEW_COVERAGE_INCOMPLETE", "Each adjacent narrative seam needs review")
    if not any(r["submission"]["scale"] == "VOLUME"
               and r["submission"]["scope_draft_refs"] == binding["ordered_draft_refs"] for r in reports):
        raise DomainError("REVIEW_COVERAGE_INCOMPLETE", "Whole-volume review must cover the ordered original manifest")
