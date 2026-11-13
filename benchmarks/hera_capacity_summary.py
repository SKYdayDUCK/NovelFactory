"""Summarize committed admission request times, embedded SQL and sampled waits."""
import argparse
from collections import Counter, defaultdict
import gzip
import json
from math import ceil
from pathlib import Path


def summarize(rows):
    runs = defaultdict(list)
    for row in rows:
        runs[(row["depth"],row["mode"])].append(row)
    metrics = {}
    for (depth,mode), trials in runs.items():
        requests = [r for trial in trials for r in trial["requests"]]
        latency = sorted((r["end"]-r["start"])*1000 for r in requests)
        stages = defaultdict(float)
        for request in requests:
            for sql, ms in request["sql"]:
                for stage, needle in (
                    ("policy_write", "UPDATE nf_durable_admission_policy"),
                    ("policy_read", "FROM nf_durable_admission_policy WHERE singleton AND (NOT enabled"),
                    ("scan", "SELECT count(*) AS total"),
                    ("task_insert", "INSERT INTO nf_tasks "),
                    ("idempotency_read", "FROM nf_task_commands "),
                    ("idempotency_write", "INSERT INTO nf_task_commands "),
                    ("outbox", "INSERT INTO nf_outbox "),
                ):
                    if needle in sql:
                        stages[stage] += ms
                        break
        waits = Counter()
        for trial in trials:
            for _, samples in trial["backend_waits"]:
                for wait_type, wait_event, count, _ in samples:
                    waits[f"{wait_type}.{wait_event}"] += count
        metrics[f"{depth}-{mode}"] = {
            "creates": len(requests), "rate": len(requests)/sum(r["elapsed_s"] for r in trials),
            "rates": [len(r["requests"])/r["elapsed_s"] for r in trials],
            "p50": latency[ceil(.5*len(latency))-1], "p95": latency[ceil(.95*len(latency))-1],
            "p99": latency[ceil(.99*len(latency))-1],
            "stage_wall_ms_per_request": {k:v/len(requests) for k,v in stages.items()},
            "backend_samples": dict(waits),
        }
    return metrics


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("raw", type=Path)
    args = parser.parse_args()
    with gzip.open(args.raw, "rt") as f:
        print(json.dumps(summarize(json.load(f)["runs"]), indent=2))
