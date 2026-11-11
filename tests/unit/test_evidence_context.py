"""Meaningful P3-03 summary provenance and controlled-experiment regressions."""
import json
from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from benchmarks.memory.controlled_evaluate import paired_deltas, run_case
from novel_factory.algorithms.evidence_context import (
    compile_evidence_context,
    extract_source,
    verify_extract,
)
from novel_factory.algorithms.memory_retrieval import Passage, sha256_text

pytestmark = pytest.mark.unit
DATA = Path(__file__).resolve().parents[2] / "benchmarks/memory"


def source(identifier="one", text="沈禾笑了。她仍为旧日丧失难过。陆舟没有把笑当成和解。", links=()):
    return Passage(identifier, text, sha256_text(text), ("shen",), tuple(links))


@pytest.fixture(scope="module")
def fixture():
    return (json.loads((DATA / "corpus.json").read_text()),
            json.loads((DATA / "gold_cases.json").read_text())["cases"])


def test_summary_is_literal_ranges_and_never_claims_full_original():
    original = source()
    lead = extract_source(original, "lead")
    tail = extract_source(original, "lead_tail")
    assert lead.text == "沈禾笑了。" and not lead.original_complete
    assert "旧日丧失" not in tail.text and "[…]" in tail.text
    assert verify_extract(original, lead) and verify_extract(original, tail)
    assert not verify_extract(original, replace(lead, text="沈禾已经不再难过。"))
    assert not verify_extract(original, replace(lead, spans=((0, len(original.text)),)))
    assert extract_source(original, "full").original_complete


def test_citation_overhead_and_unicode_ranges_fit_exact_budget():
    original = source()
    packed = compile_evidence_context([original], "沈禾", budget_bytes=1000, strategy="lead_tail")
    assert packed.content_bytes == len(packed.content.encode("utf-8"))
    assert "original_sha256=" + original.sha256 in packed.content
    assert "complete=false" in packed.content
    same = compile_evidence_context([original], "沈禾", budget_bytes=packed.content_bytes, strategy="lead_tail")
    assert same.content == packed.content
    missing = compile_evidence_context([original], "沈禾", budget_bytes=packed.content_bytes - 1, strategy="lead_tail")
    assert not missing.extracts and missing.omitted_source_ids == ("one",)


def test_hidden_link_is_neither_followed_nor_exposed_in_returned_context():
    original = source(links=["hidden-secret-source"])
    packed = compile_evidence_context([original], "沈禾", budget_bytes=1000, expand_links=True)
    assert "hidden-secret-source" not in json.dumps(asdict(packed), ensure_ascii=False)
    assert packed.candidate_source_ids == ("one",)


def test_pool_is_identical_across_representation_and_graph_arms(fixture):
    corpus, cases = fixture
    case = cases[2]
    results = [run_case(corpus, case, 1500, strategy, links)
               for strategy in ("full", "lead", "lead_tail") for links in (False, True)]
    assert len({row["authorized_pool_sha256"] for row in results}) == 1
    assert len({row["candidate_pool_sha256"] for row in results}) == 1
    assert all(row["all_anchors_verified"] and row["selected_bytes"] <= 1500 for row in results)
    assert len(paired_deltas(results)) == 3


def test_gold_cannot_change_retrieval_or_packing(fixture):
    corpus, cases = fixture
    case = deepcopy(cases[1])
    before = run_case(corpus, case, 1500, "lead_tail", True)
    case["gold"]["required_source_ids"] = []
    case["gold"]["forbidden_source_ids"] = ["E03"]
    after = run_case(corpus, case, 1500, "lead_tail", True)
    for key in ("context_sha256", "ranking_trace", "extracts", "candidate_pool_sha256"):
        assert before[key] == after[key]


def test_review_comparison_exposes_missing_prior_originals_without_faking_verdict(fixture):
    corpus, cases = fixture
    case = next(row for row in cases if row["case_id"] == "M29")
    local = run_case(corpus, case, 6000, "full", True, mode="chapter_only", review_chapter=37)
    memory = run_case(corpus, case, 6000, "full", True, mode="accumulated", review_chapter=37)
    assert local["authorized_pool_sha256"] == memory["authorized_pool_sha256"]
    assert local["candidate_pool_sha256"] != memory["candidate_pool_sha256"]
    assert "E20" in local["missed_required_source_ids"]
    assert memory["full_original_recall"] > local["full_original_recall"]
    assert local["review_verdict"] == memory["review_verdict"] == "NOT_EVALUATED"
    assert memory["actual_model_calls"] == 0


def test_actor_view_and_small_budget_remain_partial(fixture):
    corpus, cases = fixture
    case = next(row for row in cases if row["case_id"] == "M11")
    for strategy in ("full", "lead", "lead_tail"):
        row = run_case(corpus, case, 30, strategy, True)
        assert row["selected_source_ids"] == []
        assert row["forbidden_source_ids"] == []
        assert row["source_recall"] == 0
        assert row["context_status"] == "PARTIAL_SEARCH_ONLY"


def test_duplicate_or_corrupt_source_and_noninteger_budget_fail_closed():
    good = source()
    with pytest.raises(ValueError, match="Duplicate"):
        compile_evidence_context([good, good], "沈禾", budget_bytes=1000)
    with pytest.raises(ValueError, match="hash"):
        extract_source(replace(good, text="不同原文"), "lead")
    with pytest.raises(ValueError, match="integer"):
        compile_evidence_context([good], "沈禾", budget_bytes=True)
