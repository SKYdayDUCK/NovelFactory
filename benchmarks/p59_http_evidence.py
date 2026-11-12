"""Verify GitHub's artifact digest and its inner raw-file manifest, then summarize HTTP results."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import zipfile
from collections import Counter
from pathlib import Path


def select(value, *keys):
    return {key: value[key] for key in keys if key in value}


def summarize_hold(rows):
    groups = {}
    for row in rows:
        if row.get("valid"):
            continue
        key = row.get("code") or str(row["status"])
        group = groups.setdefault(key, {"requests": 0, "first_seconds": None, "last_seconds": None})
        group["requests"] += 1
        t = row.get("started_seconds")
        if t is not None:
            group["first_seconds"] = (
                min(group["first_seconds"], t) if group["first_seconds"] is not None else t
            )
            group["last_seconds"] = (
                max(group["last_seconds"], t) if group["last_seconds"] is not None else t
            )
    return groups


def service(metrics):
    return {
        **select(metrics, "ingress", "authentication_admission", "admission", "resources"),
        "database": select(
            metrics.get("database", {}),
            "sql_statements",
            "transactions",
            "failed_statements",
            "checked_out_at_end",
            "peak_checked_out_connections",
            "configured_pool_size",
            "sql_cursor_execution_seconds",
            "transaction_seconds_before_commit",
        ),
    }


def summarize_old(result):
    if "open_loop_offered_rate" in result:
        return {
            **select(
                result,
                "capacity_gate",
                "first_attempt_capacity_gate",
                "engineer_correctness",
                "bounded_recovery_business_gate",
                "open_loop_offered_rate",
            ),
            "metrics": select(
                result["metrics"],
                "planned_requests",
                "completed_requests",
                "first_attempt_status_counts",
                "valid_successful_calls",
                "effective_call_rps_excluding_duplicates",
                "status_counts",
                "infrastructure_retries",
                "latency_all_responses",
                "new_world_commit_receipts",
            ),
            "world": select(
                result["correctness"], "new_world_revisions", "expected_new_world_revisions"
            ),
            "service": service({**result, "database": result["database_application"]}),
        }
    if "actual_call_start_rps" in result:
        return {
            **select(
                result,
                "probe_gate",
                "long_duration_gate",
                "duration_seconds",
                "scheduled_count",
                "actual_call_start_rps",
                "successful_query_rps",
                "status_counts",
                "worker_lease_renewals",
                "latency_all_attempts",
                "world_unchanged",
            ),
            "validated": sum(bool(r["valid"]) for r in result["records"]),
            "unsuccessful_groups": summarize_hold(result["records"]),
            "service": service(result["service_final"]),
        }
    return {
        **select(
            result,
            "capacity_gate",
            "offered_wire_rate_gate",
            "scheduled_requests",
            "wire_started_requests",
            "validated_successes",
            "successful_query_rps",
            "observed_wire_start_rps",
            "dispatch_start_rps",
            "status_counts",
            "latency_all_attempts",
            "world_unchanged",
        ),
        "service": service(result["service_metrics"]),
    }


def summarize(metadata, archives):
    path = archives / str(metadata["run_id"]) / (str(metadata["artifact_id"]) + ".zip")
    with path.open("rb") as raw:
        actual = hashlib.file_digest(raw, "sha256").hexdigest()
    if actual != metadata["sha256"]:
        raise ValueError("Outer artifact digest mismatch")
    with zipfile.ZipFile(path) as archive:
        files = archive.namelist()
        manifest = "manifest.sha256" if "manifest.sha256" in files else "raw.sha256"
        verified = {}
        for entry in archive.read(manifest).decode().splitlines():
            expected, name = entry.split(maxsplit=1)
            name = Path(name).name
            blob = gzip.decompress(archive.read(name + ".gz"))
            if hashlib.sha256(blob).hexdigest() != expected:
                raise ValueError("Inner file digest mismatch: " + name)
            verified[name] = expected

        def load(name):
            if name not in verified:
                raise ValueError("Missing inner manifest entry: " + name)
            return json.loads(gzip.decompress(archive.read(name + ".gz")))

        base = {
            **metadata,
            "source": archive.read("source").decode().strip(),
            "verified_inner_sha256": verified,
        }
        if "result.json" in verified:
            return {
                **base,
                "exit": int(archive.read("exit-code")),
                "frozen_transport_header_rps": [
                    run["observed_header_start_rps"] for run in load("transport.json")["runs"]
                ],
                "outcome": summarize_old(load("result.json")),
            }
        profiles = {
            profile: load(profile + ".json")
            for profile in ("single", "calibration-1", "calibration-2", "business")
        }
        return {
            **base,
            "frozen_transport_header_rps": [
                run["observed_header_start_rps"] for run in load("frozen-client.json")["runs"]
            ],
            "outcome": load("summary.json"),
            "profiles": {k: d["summary"] for k, d in profiles.items()},
            "service": service(profiles["business"]["server_metrics"]),
            "validation_errors": dict(
                Counter(
                    r["correctness_error"]
                    for r in profiles["business"]["raw_requests"]
                    if r.get("correctness_error")
                )
            ),
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archives", type=Path, required=True)
    parser.add_argument("--metadata", type=Path, required=True)
    args = parser.parse_args()
    summaries = [
        summarize(json.loads(path.read_text()), args.archives)
        for path in sorted(args.metadata.glob("[0-9]*.json"))
    ]
    (args.metadata / "verified-results.json").write_text(json.dumps(summaries, indent=2) + "\n")


if __name__ == "__main__":
    main()
