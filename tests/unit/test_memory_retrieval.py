"""P3-03 evidence packing, cut separation, and benchmark-integrity checks."""
import json
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path

import pytest

from benchmarks.memory.build_corpus import build
from benchmarks.memory.evaluate import (
    all_sources,
    candidate_view,
    fixture_visible,
    run_case,
    validate_fixture,
)
from novel_factory.algorithms.memory_retrieval import (
    Passage,
    pack_context,
    rank_authorized,
    sha256_text,
    terms,
)

pytestmark = pytest.mark.unit
DATA = Path(__file__).resolve().parents[2] / "benchmarks/memory"


def passage(identifier, text, *, links=(), entities=()):
    return Passage(identifier, text, sha256_text(text), tuple(entities), tuple(links))


@pytest.fixture(scope="module")
def data():
    return (json.loads((DATA / "corpus.json").read_text()),
            json.loads((DATA / "gold_cases.json").read_text())["cases"])


def test_chinese_terms_are_reproducible_without_provider_token_claim():
    assert terms("铜钥 ABC-12") == frozenset({"铜钥", "abc", "12"})
    assert terms("雨封信") == frozenset({"雨封", "封信"})


def test_budget_counts_headers_and_multibyte_text_without_splitting_sources():
    source = passage("s1", "沈禾还没有宽恕他。")
    ranked = rank_authorized([source], "沈禾")
    whole = pack_context(ranked, 1000)
    assert whole.content_bytes == len(whole.content.encode("utf-8"))
    assert pack_context(ranked, whole.content_bytes).passages == ranked
    refused = pack_context(ranked, whole.content_bytes - 1)
    assert not refused.passages and refused.omitted_source_ids == ("s1",)
    assert refused.status == "PARTIAL_SEARCH_ONLY"


def test_source_hash_and_duplicate_id_are_rejected():
    with pytest.raises(ValueError, match="hash"):
        rank_authorized([Passage("s1", "changed text", "0" * 64)], "text")
    item = passage("s1", "原文")
    with pytest.raises(ValueError, match="Duplicate"):
        rank_authorized([item, item], "原文")


def test_graph_expansion_cannot_cross_missing_hidden_node_or_count_it():
    seed = passage("known", "芦绳留下伏笔。", links=["hidden"])
    target = passage("other", "陆舟收到一封信。")
    before = rank_authorized([seed, target], "芦绳", expand_links=True)
    # The hidden node's links and text are never supplied to this function.
    assert [row.passage.source_id for row in before] == ["known"]
    content = pack_context(before, 5000).content
    assert "hidden" not in content and "other" not in content
    assert "hidden" not in json.dumps([asdict(row) for row in before])
    assert before[0].passage.related_source_ids == ()


def test_link_expansion_is_one_hop_and_deterministic():
    first = passage("a", "芦绳", links=["b"])
    second = passage("b", "未回应的约定", links=["c"])
    third = passage("c", "远方的回信")
    result = rank_authorized([third, second, first], "芦绳", expand_links=True)
    assert [r.passage.source_id for r in result] == ["a", "b"]
    assert result == rank_authorized([first, second, third], "芦绳", expand_links=True)


def test_fixture_regeneration_and_all_anchors_are_exact(data):
    corpus, cases = data
    assert corpus == build()
    assert corpus["total_code_points"] > 50000
    assert len(corpus["chapters"]) == 48
    validate_fixture(corpus, cases)


def test_gold_labels_never_change_the_retrieved_candidate_order(data):
    corpus, cases = data
    case = deepcopy(cases[5])
    first = run_case(corpus, case, "temporal_graph", 1500)
    case["gold"]["required_source_ids"] = []
    case["gold"]["forbidden_source_ids"] = []
    second = run_case(corpus, case, "temporal_graph", 1500)
    assert first["ranking_trace"] == second["ranking_trace"]
    assert first["context_sha256"] == second["context_sha256"]


def test_actor_knowledge_time_and_objective_truth_remain_different(data):
    corpus, cases = data
    by_id = {source["source_id"]: source for source in all_sources(corpus)}
    early = next(case for case in cases if case["case_id"] == "M11")
    late = next(case for case in cases if case["case_id"] == "M12")
    assert fixture_visible(by_id["E07"], early["cut"])
    assert not fixture_visible(by_id["E10"], early["cut"])
    assert not fixture_visible(by_id["E08"], early["cut"])
    assert fixture_visible(by_id["E10"], late["cut"])
    assert not fixture_visible(by_id["E07"], late["cut"])


def test_scopes_filter_branch_other_work_plan_and_reader_before_ranking(data):
    corpus, cases = data
    for case in cases:
        passages, eligible = candidate_view(corpus, case["cut"], "temporal_graph")
        allowed_ids = {row["source_id"] for row in eligible}
        ranked = rank_authorized(passages, case["question"], entity_ids=case["query_entities"],
                                 expand_links=True)
        assert {r.passage.source_id for r in ranked} <= allowed_ids
        assert not ({r.passage.source_id for r in ranked} & set(case["gold"]["forbidden_source_ids"]))


def test_incorrect_gold_and_corrupted_source_fail_before_scoring(data):
    corpus, cases = deepcopy(data)
    cases[0]["gold"]["required_source_ids"].append("E22")
    with pytest.raises(ValueError, match="outside"):
        validate_fixture(corpus, cases)
    corpus, cases = deepcopy(data)
    corpus["chapters"][0]["sources"][0]["text"] += "未经锚定的改写"
    with pytest.raises(ValueError, match="hash"):
        validate_fixture(corpus, cases)


def test_low_budget_misses_are_retained_and_never_called_complete(data):
    corpus, cases = data
    results = [run_case(corpus, case, "temporal_graph", 300) for case in cases]
    assert any(row["missed_source_ids"] for row in results)
    assert all(row["context_status"] == "PARTIAL_SEARCH_ONLY" for row in results)
    assert all(row["selected_bytes"] <= 300 for row in results)
    assert all(row["actual_model_calls"] == 0 and row["model_cost"] is None for row in results)
