"""P3-03 controlled source-pool, summary and review-evidence ablations.

Synthetic retrieval/coverage engineering experiment; not an answer generator,
semantic reviewer, model experiment, or measurement of literary quality.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import statistics
import time
from dataclasses import asdict
from pathlib import Path

from benchmarks.memory.evaluate import DATA, all_sources, fixture_visible, validate_fixture
from novel_factory.algorithms.evidence_context import (
    REPRESENTATION_VERSION,
    STRATEGIES,
    compile_evidence_context,
    verify_extract,
)
from novel_factory.algorithms.memory_retrieval import Passage
from novel_factory.domain.canonical import fingerprint


def source_pool(corpus, cut):
    """Offline fixture filter; production authorization stays in the services."""
    rows = [row for row in all_sources(corpus) if fixture_visible(row, cut)]
    return rows, [Passage(row["source_id"], row["text"], row["sha256"],
                         tuple(row["entities"]), tuple(row["related"]), row["category"])
                  for row in rows]


def retrieve(corpus, query, cut, entities, budget, strategy, links, review_chapter=None):
    """No gold parameter and no access to scoring labels."""
    rows, authorized = source_pool(corpus, cut)
    authorized_digest = fingerprint(sorted([p.source_id, p.sha256] for p in authorized))
    candidates = authorized
    if review_chapter is not None:
        current = {row["source_id"] for row in rows if row["chapter"] == review_chapter}
        candidates = [p for p in candidates if p.source_id in current]
    context = compile_evidence_context(candidates, query, budget_bytes=budget,
        strategy=strategy, entity_ids=entities, expand_links=links)
    by_id = {p.source_id: p for p in candidates}
    return context, {
        "authorized_pool_sha256": authorized_digest,
        "authorized_pool_size": len(authorized),
        "candidate_pool_sha256": fingerprint(sorted([p.source_id, p.sha256] for p in candidates)),
        "candidate_pool_size": len(candidates),
        "all_anchors_verified": all(verify_extract(by_id[item.source_id], item) for item in context.extracts),
    }


def score(context, gold):
    selected = {item.source_id for item in context.extracts}
    complete = {item.source_id for item in context.extracts if item.original_complete}
    required = set(gold["required_source_ids"])
    forbidden = set(gold["forbidden_source_ids"])
    # Full-original coverage is deliberately conservative. Source presence in a
    # summary is not counted as proof that its decisive sentence survived.
    return {
        "source_recall": len(selected & required) / len(required) if required else None,
        "full_original_recall": len(complete & required) / len(required) if required else None,
        "all_required_sources_present": required <= selected if required else None,
        "all_required_originals_present": required <= complete if required else None,
        "forbidden_source_ids": sorted(selected & forbidden),
        "missed_required_source_ids": sorted(required - selected),
        "incomplete_required_source_ids": sorted((required & selected) - complete),
        "review_verdict": "NOT_EVALUATED",
    }


def run_case(corpus, case, budget, strategy, links, *, mode="accumulated", review_chapter=None):
    started = time.perf_counter_ns()
    context, metadata = retrieve(corpus, case["question"], case["cut"], case["query_entities"],
        budget, strategy, links, review_chapter if mode == "chapter_only" else None)
    elapsed_ms = (time.perf_counter_ns() - started) / 1e6
    return {
        "case_id": case["case_id"], "category": case["gold"]["category"],
        "budget_bytes": budget, "strategy": strategy, "links": links, "mode": mode,
        "review_chapter": review_chapter, "cut_sha256": fingerprint(case["cut"]),
        **metadata, **score(context, case["gold"]),
        "selected_bytes": context.content_bytes, "context_sha256": fingerprint(context.content),
        "selected_source_ids": [row.source_id for row in context.extracts],
        "extracts": [asdict(row) for row in context.extracts],
        "ranking_trace": list(context.ranking_trace), "context_status": context.status,
        "elapsed_ms": round(elapsed_ms, 6), "actual_model_calls": 0,
        "provider_tokens": None, "literary_quality": "NOT_EVALUATED",
    }


def evaluate(corpus, cases, budgets, review_cases):
    validate_fixture(corpus, cases)
    results = [run_case(corpus, case, budget, strategy, links)
               for budget in budgets for case in cases for strategy in STRATEGIES for links in (False, True)]
    by_case = {row["case_id"]: row for row in cases}
    for review in review_cases:
        if review["case_id"] not in by_case:
            raise ValueError("Unknown review case")
        for budget in budgets:
            for mode in ("chapter_only", "accumulated"):
                results.append(run_case(corpus, by_case[review["case_id"]], budget, "full", True,
                    mode=mode, review_chapter=review["chapter"]))
    return results


def summarize(results):
    groups = {}
    for row in results:
        axis = "review_evidence" if row["review_chapter"] is not None else "controlled_representation"
        key = (axis, row["budget_bytes"], row["strategy"], row["links"], row["mode"])
        groups.setdefault(key, []).append(row)
    output = []
    for (axis, budget, strategy, links, mode), rows in sorted(groups.items()):
        positives = [row for row in rows if row["source_recall"] is not None]
        output.append({"axis": axis, "budget_bytes": budget, "strategy": strategy,
            "links": links, "mode": mode, "cases": len(rows),
            "mean_source_recall": statistics.mean(row["source_recall"] for row in positives),
            "mean_full_original_recall": statistics.mean(row["full_original_recall"] for row in positives),
            "complete_original_evidence_cases": sum(bool(row["all_required_originals_present"]) for row in rows),
            "forbidden_hits": sum(len(row["forbidden_source_ids"]) for row in rows),
            "anchor_failures": sum(not row["all_anchors_verified"] for row in rows),
            "mean_selected_bytes": statistics.mean(row["selected_bytes"] for row in rows),
            "median_elapsed_ms": statistics.median(row["elapsed_ms"] for row in rows)})
    return output


def paired_deltas(results):
    """Per-case link effects hold candidate IDs, representation and cap fixed."""
    paired = {}
    for row in results:
        if row["review_chapter"] is None:
            paired.setdefault((row["case_id"], row["budget_bytes"], row["strategy"]), {})[row["links"]] = row
    deltas = []
    for (case, budget, strategy), pair in sorted(paired.items()):
        left, right = pair[False], pair[True]
        if left["candidate_pool_sha256"] != right["candidate_pool_sha256"]:
            raise ValueError("Paired candidates differ")
        deltas.append({"case_id": case, "budget_bytes": budget, "strategy": strategy,
            "source_recall_delta": right["source_recall"] - left["source_recall"]
                if left["source_recall"] is not None else None,
            "full_original_recall_delta": right["full_original_recall"] - left["full_original_recall"]
                if left["full_original_recall"] is not None else None,
            "candidate_pool_sha256": left["candidate_pool_sha256"]})
    return deltas


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--budgets", type=int, nargs="+", default=[1500, 3000, 6000])
    args = parser.parse_args()
    if not args.budgets or any(b <= 0 for b in args.budgets):
        parser.error("budgets must be positive")
    corpus = json.loads((DATA / "corpus.json").read_text())
    cases = json.loads((DATA / "gold_cases.json").read_text())["cases"]
    review_cases = json.loads((DATA / "review_cases.json").read_text())["cases"]
    records = evaluate(corpus, cases, args.budgets, review_cases)
    args.output.mkdir(parents=True, exist_ok=True)
    raw = args.output / "controlled_cases.jsonl"
    raw.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in records))
    deltas = args.output / "paired_deltas.json"
    deltas.write_text(json.dumps(paired_deltas(records), ensure_ascii=False, indent=2) + "\n")
    files = {"corpus": DATA / "corpus.json", "gold": DATA / "gold_cases.json",
             "review": DATA / "review_cases.json", "runner": Path(__file__), "raw": raw, "paired": deltas,
             "algorithm": DATA.parents[1] / "src/novel_factory/algorithms/evidence_context.py"}
    summary = {"status": "DETERMINISTIC_ENGINEERING_ONLY", "representation_version": REPRESENTATION_VERSION,
        "python": platform.python_version(), "platform": platform.platform(), "record_count": len(records),
        "gold_cases": len(cases), "review_cases": len(review_cases), "budgets": args.budgets,
        "actual_model_calls": 0, "literary_quality": "NOT_EVALUATED",
        "sha256": {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in files.items()},
        "summary": summarize(records), "limitations": [
            "Same authorized candidate pool for all six representation/link arms; no gold passed to retrieval.",
            "Review chapter-only/accumulated arms intentionally differ in available chapters; reported separately.",
            "Full-original recall measures literal evidence completeness, not semantic answer correctness.",
            "Summaries are deterministic literal extracts, not model-written semantic summaries.",
            "UTF-8 bytes include anchors and separators; no conversion to provider tokens or cost.",
            "48-chapter synthetic corpus is not real long fiction or independent human literary evaluation.",
            "All comparisons retained, including regressions and insufficient-budget misses.",
            "Fixture cut labels are not production authorization; PostgreSQL service tests cover that boundary."]}
    (args.output / "controlled_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"status": summary["status"], "record_count": len(records),
                      "sha256": summary["sha256"], "summary": summary["summary"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
