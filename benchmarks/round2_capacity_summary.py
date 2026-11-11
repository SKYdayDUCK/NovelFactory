"""Derive queue, CPU, loop, and effective business measures from original paired records."""
import argparse
import gzip
import hashlib
import json
import lzma
from pathlib import Path

from benchmarks.scion_agent_completion import count_agents


def queue(row):
    return {name: row.get(name, 0) for name in (
        "admitted", "completed", "timed_out", "rejected", "peak_queued",
        "peak_active", "wait_seconds_total", "active", "queued")}


def summarize(path):
    opener = gzip.open if path.name.endswith(".gz") else lzma.open
    with opener(path) as handle:
        doc = json.load(handle)
    inputs = doc["paired_inputs"]
    business = count_agents(doc)
    mixed = "logical_agent_task_count" in doc
    service = (doc if mixed else doc["service_metrics"] if "service_metrics" in doc
               else doc["service_final"])
    resources = service["service_resources"] if mixed else service["resources"]
    metrics = doc.get("metrics", {})
    db = service["database_application"] if mixed else service["database"]
    workers = doc["service_boundary"]["workers"]
    observer = doc["database_observer"]
    last = observer["after"]["stats"]
    first = observer["before"]["stats"]
    wire_starts = [r["wire_started_seconds"] for r in doc.get("raw_requests", [])
                   if r.get("wire_started_seconds") is not None]
    wire_span = max(wire_starts) - min(wire_starts) if len(wire_starts) > 1 else 0
    summary = {
        "file": path.name, "raw_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "fixture_sha256": inputs["fixture_sha256"], "source_sha": inputs["source_sha"],
        "harness_sha256": inputs["harness_sha256"], "affinity": inputs["cpu_affinity"],
        "environment_sha": doc["environment"]["git_sha"], "dirty": doc["environment"]["git_dirty"],
        "effective_service_flags": [w.get("service_schema_links_active") for w in workers],
        "business": business, "statuses": metrics.get("status_counts", doc.get("status_counts")),
        "gate": doc.get("capacity_gate", doc.get("probe_gate")),
        "world_unchanged": doc.get("world_unchanged"),
        "new_world_commits": (doc["correctness"]["new_world_revisions"] if mixed else doc["new_world_commits"]),
        "unique_commit_target": (doc["correctness"]["expected_new_world_revisions"] if mixed else 0),
        "retries": metrics.get("infrastructure_retries", 0), "duplicates": metrics.get("successful_duplicate_receipts", 0),
        "conflicts": metrics.get("conflict_count", 0),
        "correctness_errors": metrics.get("correctness_errors", doc.get("correctness_errors")),
        "duration_s": metrics.get("duration_seconds", doc.get("duration_seconds")),
        "effective_rps": metrics.get("effective_call_rps_excluding_duplicates", doc.get("successful_query_rps")),
        "world_tps": metrics.get("effective_world_commits_per_second", 0),
        "all_attempts_latency_ms": metrics.get("latency_all_responses", doc.get("latency_all_attempts")),
        "client_pool_wait_ms": metrics.get("client_pool_wait"),
        "wire_rps": (doc.get("open_loop_offered_rate") or {}).get("transport_header_start_rps", doc.get("observed_wire_start_rps")),
        "dispatch_rps": (doc.get("open_loop_offered_rate") or {}).get("actual_call_start_rps", doc.get("dispatch_start_rps", doc.get("actual_call_start_rps"))),
        "first_attempt_429": metrics.get("first_attempt_status_counts", {}).get("429", 0) if mixed else None,
        "client_wire_rps_all_phases": (len(wire_starts) - 1) / wire_span if wire_span else doc.get("observed_wire_start_rps"),
        "service_received_rps_all_phases": doc["service_boundary"]["arrivals_per_second"],
        "service_cpu_s": resources["service_user_cpu_seconds"] + resources["service_system_cpu_seconds"],
        "client_cpu_s": (doc["resources"]["process_user_cpu_seconds"] + doc["resources"]["process_system_cpu_seconds"]) if mixed else None,
        "pg_proc_cpu_estimate_s": doc["pg_cluster_cpu_seconds"],
        "pg_cpu_scope": "May omit live backends when /proc children is unavailable; zero is not proof of absent DB CPU",
        "sql": db["sql_statements"],
        "peak_db_connections": db["peak_checked_out_connections"], "db_connections_end": db["checked_out_at_end"],
        "deadlocks": last["deadlocks"] - first["deadlocks"],
        "wal_bytes": observer["wal_bytes"], "failed_sql": db["failed_statements"],
        "db_reads": last["blks_read"] - first["blks_read"],
        "query_cache_hits": service.get("cache_hits"), "query_cache_misses": service.get("cache_misses"),
        "event_loop_delay_by_process": [w["event_loop_delay_ms"] for w in workers],
        "ingress": queue(service["ingress"]), "authentication": queue(service["authentication_admission"]),
        "operations": queue(service["admission"]),
    }
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", type=Path)
    args = parser.parse_args()
    results = {path.name: summarize(path) for path in args.inputs}
    print(json.dumps(results, indent=2, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
