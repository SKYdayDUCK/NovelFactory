"""P3-03 deterministic, paired Chinese evidence-retrieval experiment.

Run from the repository root:
  uv run python benchmarks/memory/evaluate.py --output evidence/p3/memory

This does not call a model, write the world, or claim literary improvement.
The fixture cut function is an offline experiment oracle, NOT an authorization
implementation. Actual service behavior is checked separately on PostgreSQL.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import statistics
import subprocess
import time
from collections import defaultdict
from pathlib import Path

from novel_factory.algorithms.memory_retrieval import (
    METHOD_VERSION,
    Passage,
    pack_context,
    rank_authorized,
)

ROOT = Path(__file__).resolve().parents[2]
DATA = Path(__file__).resolve().parent
METHODS = ("ordinary_summary", "character_memory", "temporal_graph", "graph_without_links")


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def all_sources(corpus):
    return [source for chapter in corpus["chapters"] for source in chapter["sources"]]


def fixture_visible(source, cut):
    """Apply declared fixture labels before any ranking or edge expansion.

    These labels are experimentally controlled data, not trusted user input for
    a deployed service. Role grants, epochs and task fences stay in Kernel.
    """
    if source["work_id"] != cut["work_id"] or source["branch_id"] != cut["branch_id"]:
        return False
    if cut["revision"] not in source["revisions"] or source["plane"] not in cut["planes"]:
        return False
    if source["plane"] == "BELIEF" and source["actor_id"] != cut["actor_id"]:
        return False
    if source["known_at"] > cut["known_at"]:
        return False
    if source["plane"] == "LITERARY" and source["reader_from"] > cut["reader_chapter"]:
        return False
    if source["plane"] != "PLAN" and source["tick"] > cut["story_tick"]:
        return False
    if not cut["history"]:
        if source["valid_from"] > cut["story_tick"]:
            return False
        if source["valid_to"] is not None and source["valid_to"] <= cut["story_tick"]:
            return False
    return True


def candidate_view(corpus, cut, method):
    """Build each representation without receiving or opening gold labels."""
    eligible = [source for source in all_sources(corpus) if fixture_visible(source, cut)]
    if method == "ordinary_summary":
        # Fixed lead/tail extractive baseline. This is NOT an LLM summary and
        # deliberately has no access to questions while choosing retained text.
        grouped = defaultdict(list)
        for source in eligible:
            grouped[source["chapter"]].append(source)
        selected = []
        for chapter in sorted(grouped):
            ordered = sorted(grouped[chapter], key=lambda s: s["paragraph"])
            selected.append(ordered[0])
            if len(ordered) > 1:
                selected.append(ordered[-1])
    elif method == "character_memory":
        # All source-anchored entries involving a known character, retaining
        # chronology and counterevidence. No manually chosen gold-only facts.
        characters = set(corpus["character_ids"])
        selected = [source for source in eligible if characters.intersection(source["entities"])]
    elif method in {"temporal_graph", "graph_without_links"}:
        selected = eligible
    else:
        raise ValueError("Unknown comparison method")
    passages = [Passage(
        source_id=s["source_id"], text=s["text"], sha256=s["sha256"],
        entities=tuple(s["entities"]), related_source_ids=tuple(s["related"]),
        category=s["category"],
    ) for s in selected]
    return passages, eligible


def validate_fixture(corpus, cases):
    if corpus.get("status") != "TEST_FIXTURE":
        raise ValueError("This evaluator only accepts explicitly labelled fixtures")
    sources = all_sources(corpus)
    by_id = {s["source_id"]: s for s in sources}
    if len(by_id) != len(sources):
        raise ValueError("Duplicate fixture source ID")
    for source in sources:
        if digest_bytes(source["text"].encode()) != source["sha256"]:
            raise ValueError("Corpus source hash mismatch")
        if source["span"] != {"start": 0, "end": len(source["text"]), "unit": "unicode_code_point"}:
            raise ValueError("Invalid source anchor span")
        if not set(source["related"]).issubset(by_id):
            raise ValueError("Source graph contains an unknown source ID")
    seen = set()
    for case in cases:
        if case["case_id"] in seen:
            raise ValueError("Duplicate gold case ID")
        seen.add(case["case_id"])
        required = set(case["gold"]["required_source_ids"])
        forbidden = set(case["gold"]["forbidden_source_ids"])
        if required & forbidden or not (required | forbidden).issubset(by_id):
            raise ValueError("Gold labels conflict or reference absent sources")
        if not all(fixture_visible(by_id[source_id], case["cut"]) for source_id in required):
            raise ValueError("Required gold evidence is outside the declared cut: " + case["case_id"])
        if any(fixture_visible(by_id[source_id], case["cut"]) for source_id in forbidden):
            raise ValueError("Forbidden source is actually visible at this cut: " + case["case_id"])


def run_case(corpus, case, method, budget_bytes):
    start = time.perf_counter_ns()
    passages, eligible = candidate_view(corpus, case["cut"], method)
    ranked = rank_authorized(passages, case["question"], entity_ids=case["query_entities"],
                             expand_links=method == "temporal_graph")
    packed = pack_context(ranked, budget_bytes)
    elapsed_ms = (time.perf_counter_ns() - start) / 1_000_000
    selected = {row.passage.source_id for row in packed.passages}
    eligible_ids = {source["source_id"] for source in eligible}
    # Only the scoring stage sees gold. Gold fields never reach candidate_view,
    # rank_authorized, or pack_context.
    required = set(case["gold"]["required_source_ids"])
    forbidden = set(case["gold"]["forbidden_source_ids"])
    retrieved_gold = selected & required
    recall = len(retrieved_gold) / len(required) if required else None
    return {
        "case_id": case["case_id"], "category": case["gold"]["category"],
        "case_kind": case["gold"]["case_kind"], "review_class": case["gold"]["review_class"],
        "method": method, "budget_bytes": budget_bytes, "cut": case["cut"],
        "question": case["question"], "input_entity_ids": case["query_entities"],
        "context_status": packed.status, "selected_source_ids": sorted(selected),
        "required_source_ids": sorted(required), "missed_source_ids": sorted(required - selected),
        "forbidden_hits": sorted(forbidden & selected), "out_of_cut_hits": sorted(selected - eligible_ids),
        "gold_evidence_recall": recall,
        "gold_evidence_precision": len(retrieved_gold) / len(selected) if selected and required else None,
        "all_required_evidence": required.issubset(selected) if required else None,
        "negative_scope_pass": not bool(forbidden & selected) if not required else None,
        "selected_bytes": packed.content_bytes, "context_sha256": digest_bytes(packed.content.encode()),
        "candidate_count": len(passages), "eligible_count": len(eligible),
        "index_text_bytes_at_cut": sum(len(p.text.encode()) for p in passages),
        "estimated_provider_tokens": None, "actual_model_calls": 0, "model_cost": None,
        "elapsed_ms": round(elapsed_ms, 6),
        "ranking_trace": [{"source_id": item.passage.source_id, "score": round(item.score, 9),
                           "reason": item.reason, "sha256": item.passage.sha256}
                          for item in packed.passages],
        "omitted_source_ids": list(packed.omitted_source_ids),
    }


def summarize(records):
    groups = defaultdict(list)
    for record in records:
        groups[(record["method"], record["budget_bytes"])].append(record)
    summary = []
    for (method, budget), group in sorted(groups.items()):
        positive = [row for row in group if row["gold_evidence_recall"] is not None]
        negative = [row for row in group if row["negative_scope_pass"] is not None]
        summary.append({
            "method": method, "budget_bytes": budget, "positive_cases": len(positive),
            "negative_scope_cases": len(negative),
            "macro_gold_evidence_recall": statistics.mean(row["gold_evidence_recall"] for row in positive),
            "complete_evidence_cases": sum(row["all_required_evidence"] for row in positive),
            "negative_scope_passes": sum(row["negative_scope_pass"] for row in negative),
            "forbidden_source_hits": sum(len(row["forbidden_hits"]) for row in group),
            "out_of_cut_source_hits": sum(len(row["out_of_cut_hits"]) for row in group),
            "mean_selected_bytes": statistics.mean(row["selected_bytes"] for row in group),
            "mean_index_text_bytes_at_cut": statistics.mean(row["index_text_bytes_at_cut"] for row in group),
            "median_elapsed_ms": statistics.median(row["elapsed_ms"] for row in group),
            "actual_model_calls": 0, "provider_token_count": None, "model_cost": None,
        })
    return summary


def evaluate(corpus, cases, budgets=(1500, 3000, 6000)):
    validate_fixture(corpus, cases)
    return [run_case(corpus, case, method, budget) for budget in budgets
            for case in cases for method in METHODS]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--budgets", type=int, nargs="+", default=[1500, 3000, 6000])
    parser.add_argument("--corpus", type=Path, default=DATA / "corpus.json")
    parser.add_argument("--cases", type=Path, default=DATA / "gold_cases.json")
    args = parser.parse_args()
    if not args.budgets or any(budget <= 0 for budget in args.budgets):
        parser.error("budgets must be positive")
    corpus = json.loads(args.corpus.read_text())
    cases = json.loads(args.cases.read_text())["cases"]
    records = evaluate(corpus, cases, tuple(args.budgets))
    args.output.mkdir(parents=True, exist_ok=True)
    raw_path = args.output / "cases.jsonl"
    raw_path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in records))
    try:
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        commit = None
    result = {
        "status": "DETERMINISTIC_FIXTURE_ONLY", "p3_status": "EVALUATION_FOUNDATION_CANDIDATE",
        "method_version": METHOD_VERSION, "base_commit_at_run": commit,
        "python": platform.python_version(), "platform": platform.platform(),
        "corpus_sha256": digest_bytes(args.corpus.read_bytes()),
        "gold_sha256": digest_bytes(args.cases.read_bytes()),
        "runner_sha256": digest_bytes(Path(__file__).read_bytes()),
        "ranker_sha256": digest_bytes((ROOT / "src/novel_factory/algorithms/memory_retrieval.py").read_bytes()),
        "records_sha256": digest_bytes(raw_path.read_bytes()),
        "chapter_count": len(corpus["chapters"]), "source_count": corpus["source_count"],
        "total_code_points": corpus["total_code_points"], "gold_cases": len(cases),
        "comparison_count": len(records), "budgets_bytes": args.budgets,
        "summary": summarize(records),
        "authority": "OFFLINE_RANKING_ONLY; no world mutation, no authorization service replacement",
        "limitations": [
            "Synthetic distance expansion is not naturally authored long fiction.",
            "Gold is authored independently of ranker output, not an independent human literary rating.",
            "Summary uses fixed lead/tail extracts; results do not estimate LLM summary performance.",
            "Character memory is a deterministic source-anchored character index, not semantic generation.",
            "Methods compare representation packages; graph_without_links isolates link expansion only.",
            "All methods share fixture scope filtering; zero leakage is not proof of model non-hallucination.",
            "Evidence recall is not answer accuracy or literary quality; no generated answers are scored.",
            "Equal caps are measured UTF-8 context bytes including headers, not actual provider tokens.",
            "No model API calls, provider charges, embedding index, or real author evaluation were performed.",
            "Actual ContextCompiler/WorldQueryService correctness has separate PostgreSQL test evidence.",
        ],
    }
    (args.output / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"status": result["status"], "comparison_count": len(records),
                      "chapter_count": result["chapter_count"], "code_points": result["total_code_points"],
                      "summary": result["summary"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
