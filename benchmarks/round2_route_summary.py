"""Reconcile HTTP outcomes, admission stages, and emitted logical steps by route."""
import argparse
from collections import Counter, defaultdict
import gzip
import json
import lzma
from pathlib import Path

from benchmarks.scion_agent_completion import count_agents, mixed_expectations

ROUTES = {
    "queries/timeline": "query", "queries/batch": "query", "contexts/compile": "context",
    "proposals": "proposal", "world/acceptance-certificates": "certificate",
    "world/accept-guarded": "accept", "world/accept": "accept",
}


def summarize(path):
    opener = gzip.open if path.suffix == ".gz" else lzma.open
    with opener(path) as handle:
        data = json.load(handle)
    workers = [w["admission_by_route"] for w in data["service_boundary"]["workers"]]
    assert all(workers)
    counts = defaultdict(Counter)
    for worker in workers:
        for key, row in worker.items():
            peak = row.get("max_waiters_seen_on_entry", 0)
            counts[key]["max_waiters_seen_on_entry"] = max(counts[key]["max_waiters_seen_on_entry"], peak)
            counts[key].update({k: v for k, v in row.items() if k != "max_waiters_seen_on_entry"})

    routes = defaultdict(Counter)
    final_steps = {}
    for row in data["raw_requests"]:
        route = ROUTES[row["endpoint"]]
        routes[route][str(row["status"])] += 1
        step = row["operation_id"]
        if step not in final_steps or row["attempt"] > final_steps[step]["attempt"]:
            final_steps[step] = row
    business = count_agents(data)
    expected = sum((mixed_expectations(data, i) for i in range(business["total_agents"])), Counter())
    emitted = Counter(r["label"] for r in final_steps.values())
    valid = Counter(r["label"] for r in final_steps.values() if
                    (r.get("expected_conflict") if r["label"] == "accept_conflict"
                     else r["status"] == 200 and not r.get("error_type")))
    assert business["fulfilled_calls"] == sum(valid.values())

    per_stage = {}
    for route, http in routes.items():
        ingress, auth, op = (counts[route + "." + stage] for stage in ("ingress", "auth", "operation"))
        rate_errors = counts.get(route + ".rate_limit", {})
        rate = sum(v for k, v in rate_errors.items() if k != "max_waiters_seen_on_entry")
        assert ingress["attempts"] == sum(http.values())
        assert ingress["admitted"] == auth["attempts"]
        assert auth["admitted"] == op["attempts"] + rate
        assert op["admitted"] == http["200"] + http["409"]
        assert http["429"] == sum(row["queue_full"] + row["wait_expired"] for row in (ingress, auth, op)) + rate
        for stage, row in zip(("ingress", "auth", "operation"), (ingress, auth, op)):
            assert row["attempts"] == row["admitted"] + row["queue_full"] + row["wait_expired"]
            n = row["admitted"]
            per_stage[route + "." + stage] = {
                "attempts": row["attempts"], "admitted": n,
                "max_waiters_seen_on_entry": row["max_waiters_seen_on_entry"],
                "queue_full": row["queue_full"], "wait_expired": row["wait_expired"],
                "mean_admitted_wait_ms": row["admitted_wait_ns"] / n / 1e6 if n else None,
                "mean_slot_held_ms": row["slot_held_ns"] / n / 1e6 if n else None,
            }
    return {"inputs": {k: data["paired_inputs"][k] for k in ("source_sha", "fixture_sha256", "harness_sha256", "cpu_affinity")},
            "service_schema_links_active": [w["service_schema_links_active"] for w in data["service_boundary"]["workers"]],
            "actual_status_by_route": routes, "steps": {k: {"planned": n, "emitted": emitted[k],
            "not_emitted": n - emitted[k], "fulfilled": valid[k]} for k, n in expected.items()},
            "workers_raw_counters": workers, "per_stage": per_stage, "business": business,
            "new_world_commits": data["correctness"]["new_world_revisions"],
            "duplicates": data["metrics"]["successful_duplicate_receipts"], "correctness_errors": data["metrics"]["correctness_errors"],
            "capacity_gate": data["capacity_gate"], "first_attempt_gate": data["first_attempt_capacity_gate"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", type=Path)
    args = parser.parse_args()
    print(json.dumps({p.name: summarize(p) for p in args.inputs}, indent=2, sort_keys=True))
