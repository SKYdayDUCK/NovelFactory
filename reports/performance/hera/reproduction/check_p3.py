"""Verify four frozen-client runs and compare adjacent service revisions."""
import argparse
import json
from pathlib import Path


def read_arm(directory, base, product):
    result = json.loads((directory / "raw.json").read_text())
    processes = [json.loads(path.read_text()) for path in directory.glob("process-*.json")]
    assert sorted(row["sha"] for row in processes if row["role"] == "api") == [product, product]
    assert [row["sha"] for row in processes if row["role"] == "client"] == [base]
    assert result["environment"]["git_sha"] == base
    assert result["scheduled_requests"] == result["completed_requests"] == 5000
    assert result["configuration"]["agents"] == result["configuration"]["arrival_rps"] == 1000
    assert result["configuration"]["api_processes"] == 2
    assert result["client_connections_limit"] == 256 and result["first_attempts_only"]
    assert result["world_unchanged"] and not result["correctness_errors"]
    assert result["new_world_commits"] == result["real_model_calls"] == 0
    assert set(result["status_counts"]) <= {"200", "429", "503", "transport_error"}
    assert result["validated_successes"] == result["status_counts"]["200"] > 0
    assert result["capacity_gate"] in {"PASS", "NOT_MET"}
    assert int((directory / "original.exit").read_text()) == int(result["capacity_gate"] != "PASS")
    return {
        "api_sha": product, "client_sha": base, "driver_sha256": result["source_sha256"],
        "capacity_gate": result["capacity_gate"],
        "correct_per_second": result["successful_query_rps"],
        "wire_starts_per_second": result["observed_wire_start_rps"],
        "client_validation_seconds": result["client_validation_seconds"],
        "status_counts": result["status_counts"], **result["latency_all_attempts"],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("baseline")
    parser.add_argument("candidate")
    args = parser.parse_args()
    arms = {name: read_arm(args.directory / name, args.baseline, sha) for name, sha in (
        ("a1", args.baseline), ("b1", args.candidate),
        ("b2", args.candidate), ("a2", args.baseline))}
    assert len({row["driver_sha256"] for row in arms.values()}) == 1
    metrics = ["correct_per_second", "p95_ms", "p99_ms"]
    pairs = []
    for num in (1, 2):
        a, b = arms[f"a{num}"], arms[f"b{num}"]
        pairs.append({key: b[key] / a[key] for key in metrics})
    summary = {"arms": arms, "candidate_over_control": pairs}
    data = json.dumps(summary, indent=2)
    print(data, flush=True)
    (args.directory / "comparison.json").write_text(data + "\n")
    # A newly lost previously met capacity target is a failure in its own right.
    for num in (1, 2):
        if arms[f"a{num}"]["capacity_gate"] == "PASS" and arms[f"b{num}"]["capacity_gate"] != "PASS":
            raise SystemExit("Original P3 capacity gate regressed")
    if all(row["correct_per_second"] < .8 for row in pairs):
        raise SystemExit("Both adjacent trials lost over 20% in validated query throughput")
    for key in ("p95_ms", "p99_ms"):
        if all(row[key] > 1.2 for row in pairs):
            raise SystemExit(f"Both adjacent trials regressed by over 20% in {key}")


if __name__ == "__main__":
    main()
