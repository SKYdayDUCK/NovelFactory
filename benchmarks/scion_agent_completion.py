"""Count agents that fulfilled every operation assigned by the existing HTTP workload."""
import argparse
import gzip
import json
import lzma
from collections import Counter, defaultdict
from pathlib import Path


def mixed_expectations(doc, agent):
    expected = Counter(["compile", "state_cold", "state_warm", "history",
                        "graph" if agent % 10 == 0 else "state_mixed"])
    independent = doc["load"]["independent_world_commands"]
    shared = doc["load"]["shared_candidates"]
    if agent < independent + shared:
        expected.update(["propose", "certificate"])
        if agent < independent:
            expected.update(["accept_independent", "accept_retry"])
        elif agent == independent:
            expected.update(["accept_selected_fixture", "accept_retry"])
        else:
            expected.update(["accept_conflict"])
    if doc["profile"] == "overload":
        expected["overload"] = doc["load"]["overload_rounds"]
    return expected


def count_agents(doc):
    counts = defaultdict(Counter)
    if "logical_agent_task_count" in doc:
        size = doc["logical_agent_task_count"]
        expected = [mixed_expectations(doc, agent) for agent in range(size)]
        assert sum(c.total() for c in expected) == doc["metrics"]["planned_requests"]
        final = {}
        for row in doc["raw_requests"]:
            key = row["operation_id"]
            if key not in final or row["attempt"] > final[key]["attempt"]:
                final[key] = row
        for row in final.values():
            ok = (row.get("expected_conflict", False) if row["label"] == "accept_conflict"
                  else row["status"] == 200 and not row.get("error_type"))
            if ok:
                counts[row["agent"]][row["label"]] += 1
    else:
        config = doc["configuration"]
        size = config["agents"]
        records = doc.get("raw_requests", doc.get("records", []))
        scheduled = doc.get("scheduled_requests", int(config.get("seconds", 0) * config.get("read_rps", 0)))
        assert scheduled > 0 and len({row["index"] for row in records}) == len(records)
        expected = [Counter(read=(scheduled + size - 1 - i) // size) for i in range(size)]
        for row in records:
            assert row["index"] % size == row["agent"]
            if row.get("valid") and row["status"] == 200:
                counts[row["agent"]]["read"] += 1
    complete = sum(counts[i] == assigned for i, assigned in enumerate(expected))
    # Bursts follow the normal business flow. A failed additional read does not
    # retroactively undo a successful world commit or an expected conflict.
    regular = sum(all(counts[i][key] == needed for key, needed in assigned.items() if key != "overload")
                  for i, assigned in enumerate(expected))
    return {"complete_agents": complete, "complete_before_extra_burst": regular,
            "total_agents": size, "planned_calls": sum(c.total() for c in expected),
            "fulfilled_calls": sum(c.total() for c in counts.values()),
            "source_sha": doc["paired_inputs"]["source_sha"],
            "fixture_sha256": doc["paired_inputs"]["fixture_sha256"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", type=Path)
    args = parser.parse_args()
    result = {}
    for path in sorted(args.inputs):
        opener = lzma.open if path.suffix == ".xz" else gzip.open
        with opener(path) as stream:
            result[f"{path.parent.name}/{path.name}"] = count_agents(json.load(stream))
    print(json.dumps(result, indent=2))
