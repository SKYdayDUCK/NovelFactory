"""Compare recorded compiler runs after checking semantic fixture equivalence."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def compare(before, after):
    for key in ("entities", "history", "subjects", "repeats"):
        if before["configuration"][key] != after["configuration"][key]:
            raise ValueError("Different workload configuration: " + key)
    for key in ("entities", "genesis_relations", "accepted_world_events", "revisions"):
        if before["dataset"][key] != after["dataset"][key]:
            raise ValueError("Different logical database contents: " + key)
    for key in ("postgres", "settings", "pool_size", "max_overflow"):
        if before["environment"][key] != after["environment"][key]:
            raise ValueError("Different database runtime: " + key)

    def facts(run):
        return {(row["subjects"], row["repeat"]): (row["facts"], row["fact_content_sha256"])
                for row in run["raw_samples"] if row["operation"] == "context_compile"}

    if facts(before) != facts(after):
        raise ValueError("Compiled factual content changed across the paired fixtures")
    rows = []
    old_groups = {row["subjects"]: row for row in before["summary"]}
    for new in after["summary"]:
        old = old_groups[new["subjects"]]
        rows.append({"subjects": new["subjects"], "samples_per_variant": new["samples"],
            "before_median_seconds": old["median_seconds"], "after_median_seconds": new["median_seconds"],
            "median_elapsed_reduction_percent": 100 * (1 - new["median_seconds"] / old["median_seconds"]),
            "before_sql_counts": old["sql_counts"], "after_sql_counts": new["sql_counts"],
            "before_dependency_insert_counts": old["dependency_insert_counts"],
            "after_dependency_insert_counts": new["dependency_insert_counts"]})
    return {"status": "CANDIDATE_MEASUREMENT_ONLY", "all_fact_hashes_identical": True,
        "logical_dataset_identical": True, "database_configuration_identical": True,
        "comparison": rows,
        "limits": ["Five local samples per size are observations, not a production latency SLO.",
            "Compiler SQL batching and shared payload hashing changed together; timing effects are not separated.",
            "The independent PostgreSQL regression suite checks scope completeness, rollback, revocation and idempotency.",
            "No state projection, snapshot integrity, authorization, acceptance rule or guarded template was relaxed.",
            "No external model was called; complete P3-01 acceptance remains pending."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", type=Path, required=True)
    parser.add_argument("--after", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = compare(json.loads(args.before.read_text()), json.loads(args.after.read_text()))
    result["inputs"] = {"before": str(args.before), "after": str(args.after)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
