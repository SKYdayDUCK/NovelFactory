"""Archive PG17 six-arm records and derive paired metrics, including failed calls.

Run from the repository root with --runs /path/to/matrix-runs --output /path/to/s5.
Each raw .tar.xz contains the exact uncompressed JSON emitted by the harness.
Join multipart files in order before extracting. --verify rereads all archive members.
To recheck gate, business, performance and pairing metrics without the run machines,
use --audit-archives --output evidence/scion_round2/s5.
"""
import argparse
from collections import Counter, defaultdict
import csv
import fcntl
from hashlib import sha256
import gzip
from io import BytesIO, StringIO
from itertools import groupby
import json
import lzma
import os
from pathlib import Path
import re
from statistics import median
import tarfile

from scion_agent_completion import count_agents, mixed_expectations


ARMS = tuple("ABCDEF")
FOLDERS = ("exactmatrix", "mixmatrix", "largemix", "openmatrix", "soakmatrix", "hotmatrix", "soakrepeat", "verifytraffic")
SWITCHES = {"A": set(), "B": {"WORKER_COLUMNS", "REVIEW_COLUMNS"}, "C": {"SCOPES"},
            "D": {"WORKER_COLUMNS", "REVIEW_COLUMNS", "SCOPES"}, "E": {"REPLAY", "REPLAY_LAZY"},
            "F": {"WORKER_COLUMNS"}}
CHUNK_BYTES = 750_000
SECRET = re.compile(rb'(?i)"(?:access_token|api_key|token|password|secret|authorization)"\s*:\s*"(?!none"|null"|redacted")|'
                    rb'-----BEGIN [A-Z ]*PRIVATE KEY-----|Bearer [a-zA-Z0-9._-]{16,}|(?:ghp_|github_pat_)[A-Za-z0-9_]{20,}')


def digest(value):
    return sha256(value).hexdigest()


def json_hash(value):
    return digest(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode())


def atomic_write(path, payload):
    if path.exists() and path.read_bytes() == payload:
        return
    temp = path.with_name(path.name + ".tmp")
    temp.write_bytes(payload)
    temp.replace(path)


def distribution(values):
    if not values:
        return None
    return {"median": median(values), "min": min(values), "max": max(values), "n": len(values)}


def read_doc(path):
    raw = gzip.decompress(path.read_bytes())
    if SECRET.search(raw):
        raise ValueError(f"Credential-like fields in {path.name}: inspect the source before archiving")
    data = json.loads(raw)
    # Ignore periodic copies of cumulative diagnostics during scalar derivation;
    # pack() independently preserves the entire original JSON without modification.
    data.pop("samples", None)
    return data


def replay_counts(counters):
    counters = counters or {}
    decisions = sum(n for k,n in counters.items() if k.startswith("replay."))
    hits = counters.get("replay.hit", 0)
    return {"decisions": decisions, "hits": hits, "hit_rate": hits / decisions if decisions else None}


def exact(doc):
    results = {}
    for r in doc["records"]:
        sql = r["sql"]
        codec = r["database_json_codec"]
        results[r["operation"]] = {
            "request_sha256": r["request_sha256"], "error": r["error"],
            "wall_ms": 1000 * r["wall_seconds"], "app_cpu_ms": 1000 * r["process_cpu_seconds"],
            "rss_after_kib": r["rss_after_kib"], "process_lifetime_peak_rss_kib": r["process_lifetime_max_rss_kib"],
            "sql_statements": sql["statements"], "sql_by_kind": sql["statement_kinds"],
            "selected_rows": sql["selected_rows_reported_by_cursor"],
            "decode_bytes": codec.get("decode", {}).get("bytes", 0),
            "encode_bytes": codec.get("encode", {}).get("bytes", 0),
            "replayed_events": r["replayed_events_total"], "full_replay_calls": len(r["replay_event_counts"]),
        }
    return {"kind": "identical_request_profile", "status": doc["status"], "operations": results,
            "initial_state_hash": doc["datasets"][0]["state_hash"],
            "final_state_hash": doc["verified_state_hash"], "selection_sha256": json_hash(doc["verified_event_selection"]),
            "counters": doc.get("scion_counters_after_measurement"),
            "replay": replay_counts(doc.get("scion_counters_after_measurement"))}


def per_agent(doc, rows, size, mixed):
    counts = [Counter() for _ in range(size)]
    if mixed:
        final = {}
        for row in rows:
            operation = row["operation_id"]
            if operation not in final or row["attempt"] > final[operation]["attempt"]:
                final[operation] = row
        expected = [mixed_expectations(doc, i).total() for i in range(size)]
        for r in final.values():
            valid = r.get("expected_conflict") if r["label"] == "accept_conflict" else (
                r["status"] == 200 and not r.get("error_type"))
            if valid:
                counts[r["agent"]][r["label"]] += 1
    else:
        plan = doc.get("scheduled_count", doc.get("scheduled_requests"))
        expected = [(plan + size - 1 - i) // size for i in range(size)]
        for r in rows:
            if r.get("valid") and r["status"] == 200:
                counts[r["agent"]]["read"] += 1
    pairs = [(counter.total(), n) for counter, n in zip(counts, expected)]
    # Consecutive runs of identical pairs retain every agent's index and workload.
    return [[sum(1 for _ in values), *pair] for pair, values in groupby(pairs)]


def http(doc):
    mixed = "logical_agent_task_count" in doc
    service = doc if mixed else doc.get("service_metrics", doc.get("service_final"))
    db = service["database_application" if mixed else "database"]
    resources = service["service_resources" if mixed else "resources"]
    metrics, client = doc.get("metrics", {}), doc.get("resources", {})
    rows = doc.get("raw_requests", doc.get("records", []))
    business = count_agents(doc)
    previous, after = doc["database_observer"]["before"]["stats"], doc["database_observer"]["after"]["stats"]
    boundary = doc["service_boundary"]
    counters = Counter()
    for worker in boundary["workers"]:
        for group in worker.get("scion", []):
            counters.update(group)
    diagnostics = service.get("service_diagnostics" if mixed else "diagnostics", [])
    codecs = {}
    for codec in ("database.json_decode", "database.json_encode"):
        observed = [item["inclusive_stage_timings"][codec] for item in diagnostics
                    if codec in item.get("inclusive_stage_timings", {})]
        if observed:
            codecs[codec] = {name: sum(item[name] for item in observed)
                             for name in ("calls", "wall_seconds", "thread_cpu_seconds")}
    phases = defaultdict(lambda: dict(attempts=0, outcomes=Counter(), retry_attempts=0, expected_conflicts=0))
    for r in rows:
        row = phases[r.get("label", "read")]
        row["attempts"] += 1
        row["outcomes"][str(r["status"]) + "/" + str(r.get("code") or r.get("error_type") or "-")] += 1
        row["retry_attempts"] += int(bool(r.get("infrastructure_retry")))
        row["expected_conflicts"] += int(bool(r.get("expected_conflict")))
    meter = doc.get("client_header_meter", {})
    duration = metrics.get("duration_seconds", doc.get("network_window_seconds", doc.get("duration_seconds")))
    commits = doc["correctness"]["new_world_revisions"] if mixed else doc["new_world_commits"]
    offered = doc.get("open_loop_offered_rate", {})
    return {
        "kind": "http", "engineer_correctness": doc.get("engineer_correctness"),
        "gate": doc.get("capacity_gate", doc.get("probe_gate")),
        "long_duration_gate": doc.get("long_duration_gate"), "wire_gate": doc.get("offered_wire_rate_gate", offered.get("gate")),
        "first_attempt_gate": doc.get("first_attempt_capacity_gate"), "recovery_gate": doc.get("bounded_recovery_business_gate"),
        "agents": {k: v for k,v in business.items() if k not in ("fixture_sha256", "source_sha")},
        "per_agent_rle": per_agent(doc, rows, business["total_agents"], mixed),
        "attempts": len(rows), "per_operation": dict(phases),
        "status_counts": metrics.get("status_counts", doc.get("status_counts")),
        "correctness_errors": metrics.get("correctness_errors", doc.get("correctness_errors")),
        "unresolved_errors": len(metrics["unresolved_operational_errors"]) if mixed else None,
        "first_attempt_failures": metrics.get("first_attempt_failure_count"),
        "first_attempt_latency_ms": metrics.get("first_attempt_latency"),
        "all_attempts_latency_ms": metrics.get("latency_all_responses", doc.get("latency_all_attempts")),
        "scheduled_to_completion_ms": metrics.get("scheduled_to_completion_latency", doc.get("scheduled_to_network_completion")),
        "retries": metrics.get("infrastructure_retries", 0), "duplicate_receipts": metrics.get("successful_duplicate_receipts", 0),
        "conflicts": metrics.get("conflict_count", 0), "world_unchanged": doc.get("world_unchanged"),
        "duration_s": duration, "world_commits": commits, "expected_world_commits": doc["correctness"]["expected_new_world_revisions"] if mixed else 0,
        "verified_world_tps": commits / duration if duration else None,
        "effective_request_rps": metrics.get("effective_call_rps_excluding_duplicates", doc.get("successful_query_rps")),
        "aggregate_service_arrival_rps": boundary["arrivals_per_second"],
        "wire_rps": offered.get("transport_header_start_rps", doc.get("observed_wire_start_rps", meter.get("headers_per_second"))),
        "dispatch_rps": offered.get("actual_call_start_rps", doc.get("dispatch_start_rps", doc.get("actual_call_start_rps"))),
        "app_cpu_s": resources["service_user_cpu_seconds"] + resources["service_system_cpu_seconds"],
        "worker_sum_peak_rss_kib": resources["service_sum_peak_rss_kib"],
        "client_cpu_s": client["process_user_cpu_seconds"] + client["process_system_cpu_seconds"] if mixed else
            meter.get("process_cpu_seconds_including_profile_finalization"),
        "client_peak_rss_kib": client.get("process_peak_rss_kib"),
        "sql": db["sql_statements"], "sql_by_kind": db["statement_kinds"], "http_json_codec":codecs,
        "db_connections_peak": db["peak_checked_out_connections"], "db_connections_end": db["checked_out_at_end"],
        "deadlocks": after["deadlocks"] - previous["deadlocks"], "counters": dict(counters), "replay":replay_counts(counters),
        "api_processes": doc["environment"].get("api_processes"),
    }


def first_requests(doc):
    return {r["operation_id"]: (r["label"], r["request_bytes_sha256"])
            for r in doc.get("raw_requests", []) if r.get("attempt") == 1 and "request_bytes_sha256" in r}


def compare(doc, baseline, selected):
    info, base = doc["paired_inputs"], baseline["paired_inputs"]
    result = {"same_fixture": info["fixture_sha256"] == base["fixture_sha256"],
              "same_harness": info["harness_sha256"] == base["harness_sha256"],
              "same_cpu_affinity": info["cpu_affinity"] == base["cpu_affinity"]}
    if selected["kind"] == "identical_request_profile":
        ref = exact(baseline)
        result["same_ordered_requests"] = [(r["operation"],r["request_sha256"]) for r in doc["records"]] == [
            (r["operation"],r["request_sha256"]) for r in baseline["records"]]
        result["same_initial_state"] = selected["initial_state_hash"] == ref["initial_state_hash"]
        result["same_final_state"] = selected["final_state_hash"] == ref["final_state_hash"]
        result["same_final_selection"] = selected["selection_sha256"] == ref["selection_sha256"]
    else:
        other, original = first_requests(doc), first_requests(baseline)
        if other or original:
            counts = defaultdict(Counter)
            for key in other.keys() | original.keys():
                r, a = other.get(key), original.get(key)
                label = (r or a)[0]
                outcome = ("A_only" if not r else "candidate_only" if not a else
                           "same_request_bytes" if r == a else "different_request_bytes")
                counts[label][outcome] += 1
            result["first_request_bytes_by_phase"] = dict(counts)
        else:
            result["request_byte_checks"] = "not instrumented in read-only HTTP harness"
    return result


def pack(output, group, members, previous, verify):
    files = {name: stat["source_gzip_sha256"] for name, (_,stat) in members.items()}
    if previous.get("source_gzip_sha256") == files and not verify and all(
            (output / p["file"]).exists() for p in previous["parts"]):
        return previous
    archive = BytesIO()
    raw_hashes = {}
    with tarfile.open(fileobj=archive, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for name, (path, _) in sorted(members.items()):
            raw = gzip.decompress(path.read_bytes())
            if SECRET.search(raw):
                raise ValueError(f"credential-like material in {name}")
            raw_hashes[name] = digest(raw)
            entry = tarfile.TarInfo(name.removesuffix(".gz"))
            entry.size = len(raw)
            tar.addfile(entry, BytesIO(raw))
    compressed = lzma.compress(archive.getvalue(), preset=3)
    parts = []
    for i, start in enumerate(range(0, len(compressed), CHUNK_BYTES), 1):
        chunk = compressed[start:start + CHUNK_BYTES]
        suffix = "" if len(compressed) <= CHUNK_BYTES else f".part{i:03d}"
        dest = output / "raw" / f"{group}.tar.xz{suffix}"
        atomic_write(dest, chunk)
        parts.append({"file": str(dest.relative_to(output)), "bytes": len(chunk), "sha256": digest(chunk)})
    if verify:
        data = b"".join((output / p["file"]).read_bytes() for p in parts)
        with tarfile.open(fileobj=BytesIO(data), mode="r:xz") as tar:
            actual = {p.name + ".gz": digest(tar.extractfile(p).read()) for p in tar.getmembers()}
        assert actual == raw_hashes
    return {"source_gzip_sha256": files, "original_json_sha256": raw_hashes, "parts": parts}


def paired_change(observed, reference):
    absolute, relative = [], []
    for observed_value, ref_value in zip(observed, reference):
        if observed_value is not None and ref_value is not None:
            absolute.append(observed_value - ref_value)
            if ref_value:
                relative.append(100 * (observed_value - ref_value) / ref_value)
    return {"absolute": distribution(absolute), "percent": distribution(relative)}


def http_change_metrics(measured):
    return {"fulfilled_calls": measured["agents"]["fulfilled_calls"],
            "complete_agents": measured["agents"]["complete_agents"],
            "world_commits": measured["world_commits"], "world_tps": measured["verified_world_tps"],
            "effective_request_rps": measured["effective_request_rps"], "wire_rps": measured["wire_rps"],
            "p99_ms": (measured["all_attempts_latency_ms"] or {}).get("p99_ms"),
            "sql": measured["sql"], "worker_sum_peak_rss_kib": measured["worker_sum_peak_rss_kib"],
            "app_cpu_s": measured["app_cpu_s"]}


def write_tables(output, cases, samples):
    tables = {"exact_operations": [], "http_runs": []}
    for case, arms in cases.items():
        for arm, data in arms.items():
            for op, metrics in data.get("operations", {}).items():
                row = {"case":case,"arm":arm,"operation":op,"replicates":len(data["repetitions"])}
                for k,dist in metrics.items():
                    row[k] = dist["median"]
                row["wall_min_ms"] = metrics["wall_ms"]["min"]
                row["wall_max_ms"] = metrics["wall_ms"]["max"]
                change = data.get("paired_changes_vs_A", {}).get(op, {}).get("wall_ms", {}).get("percent")
                row["paired_wall_change_percent_median"] = None if change is None else change["median"]
                tables["exact_operations"].append(row)
    for sample in samples.values():
        m = sample["measurement"]
        if m["kind"] != "http":
            continue
        row = {"case":sample["case"], "arm":sample["arm"], "replicate":sample["repeat"],
               "affinity":",".join(map(str,sample["cpu_affinity"])), "fixture_sha256":sample["fixture_sha256"]}
        row.update({k:m[k] for k in ("gate","long_duration_gate","first_attempt_gate","recovery_gate","wire_gate",
            "attempts","retries","duplicate_receipts","conflicts","world_commits","expected_world_commits",
            "verified_world_tps","effective_request_rps","wire_rps","dispatch_rps","aggregate_service_arrival_rps",
            "app_cpu_s","client_cpu_s","worker_sum_peak_rss_kib","sql","db_connections_peak","db_connections_end","deadlocks")})
        row.update(m["agents"])
        row.update({k:(m["all_attempts_latency_ms"] or {}).get(k) for k in ("p50_ms","p95_ms","p99_ms")})
        row.update({"replay_decisions":m["replay"]["decisions"], "replay_hits":m["replay"]["hits"],
                    "replay_hit_rate":m["replay"]["hit_rate"], "correctness_errors":len(m["correctness_errors"] or []),
                    "status_counts":json.dumps(m["status_counts"],sort_keys=True,separators=(",",":"))})
        tables["http_runs"].append(row)
    for name, rows in tables.items():
        if not rows:
            continue
        stream = StringIO(newline="")
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
        atomic_write(output / (name + ".csv"), stream.getvalue().encode())


def audit_archives(output):
    """Recompute each sample and paired identity checks from just the delivery."""
    report = json.loads((output / "performance.json").read_bytes())
    audited = set()
    active_case, baselines = None, {}
    for group, archive in sorted(report["archives"].items()):
        chunks = []
        for part in archive["parts"]:
            chunk = (output / part["file"]).read_bytes()
            assert digest(chunk) == part["sha256"] and len(chunk) == part["bytes"], part["file"]
            chunks.append(chunk)
        with tarfile.open(fileobj=BytesIO(b"".join(chunks)), mode="r:xz") as tar:
            expected_members = archive["original_json_sha256"]
            assert set(tar.getnames()) == {n.removesuffix(".gz") for n in expected_members}
            for member in sorted(tar.getmembers(), key=lambda item:item.name):
                raw = tar.extractfile(member).read()
                assert digest(raw) == expected_members[member.name + ".gz"], member.name
                source_case, arm, repeat = member.name.removesuffix(".json").rsplit("-", 2)
                case = archive.get("case", source_case)
                key = f"{case}/{arm}/{repeat}"
                doc = json.loads(raw)
                if case != active_case:
                    active_case, baselines = case, {}
                if arm == "A":
                    # A pure-read load has no byte digests or exact profile.
                    # Only the metadata can participate in its pairing check.
                    baselines[repeat] = doc if ("metrics" in doc or case.endswith("-exact")) else {"paired_inputs": doc["paired_inputs"]}
                sample = report["samples"][key]
                inputs = doc["paired_inputs"]
                assert inputs["source_sha"] == sample["source_sha"] and inputs["fixture_sha256"] == sample["fixture_sha256"]
                actual = exact(doc) if case.endswith("-exact") else http(doc)
                assert actual == sample["measurement"], key
                if sample["paired_against_A"]:
                    assert compare(doc, baselines[repeat], actual) == sample["paired_against_A"], key
                assert key not in audited
                audited.add(key)
    assert audited == set(report["samples"])
    print(len(audited), "raw samples and paired measurements verified")


def campaign_records(runs):
    records = {}
    for folder in FOLDERS:
        session = runs / folder / "pg" / "postgres_session.json"
        if not session.exists():
            continue
        data = json.loads(session.read_bytes())
        parser = argparse.ArgumentParser(add_help=False)
        parser.add_argument("--arms", nargs="+", default=list(ARMS))
        parser.add_argument("--cases", nargs="+", required=True)
        parser.add_argument("--repetitions", type=int, default=1)
        args, _ = parser.parse_known_args(data["command"])
        records[folder] = {"cases":args.cases,"arm_order_first_repeat":args.arms,"repetitions":args.repetitions,
                           "postgres":data["postgres"],"exit_status":data["exit_status"],
                           "started_at_utc":data["started_at_utc"], "finished_at_utc":data["finished_at_utc"],
                           "fsync":data["fsync"],"synchronous_commit":data["synchronous_commit"]}
    return records


def main(runs, output, verify=False):
    (output / "raw").mkdir(parents=True, exist_ok=True)
    report_path = output / "performance.json"
    previous = json.loads(report_path.read_text()) if report_path.exists() else {}
    samples, groups = {}, defaultdict(dict)
    for folder in FOLDERS:
        for path in sorted((runs / folder).glob("*.json.gz")):
            source_case, arm, rep = path.name.removesuffix(".json.gz").rsplit("-",2)
            case = source_case + "-repeat" if folder in ("soakrepeat", "verifytraffic") else source_case
            if arm not in ARMS: raise ValueError(f"Unexpected arm {path}")
            key = f"{case}/{arm}/{rep}"
            if key in samples: raise ValueError(f"Duplicate run {key}")
            file_hash = digest(path.read_bytes())
            baseline_path = path.with_name(f"{source_case}-A-{rep}.json.gz")
            old = previous.get("samples", {}).get(key, {})
            if (not verify and old.get("source_gzip_sha256") == file_hash and old.get("paired_against_A")
                    and "replay" in old.get("measurement", {}) and (case.endswith("-exact") or "http_json_codec" in old["measurement"])):
                stat = old
            else:
                doc = read_doc(path)
                inputs = doc["paired_inputs"]
                active = {k.removeprefix("NF_SCION_") for k,v in inputs["experiment_env"].items() if v == "1"}
                if active != SWITCHES[arm]: raise ValueError(f"Unexpected feature switches {key}: {active}")
                selected = exact(doc) if case.endswith("-exact") else http(doc)
                stat = {"case":case, "arm":arm, "repeat":int(rep), "source_gzip_sha256":file_hash,
                        "fixture_sha256":inputs["fixture_sha256"], "source_sha":inputs["source_sha"],
                        "source_files_sha256":json_hash(inputs["source_files"]), "harness_sha256":inputs["harness_sha256"],
                        "cpu_affinity":inputs["cpu_affinity"], "measurement":selected,
                        "paired_against_A": compare(doc, doc if arm == "A" else read_doc(baseline_path), selected) if baseline_path.exists() else None}
            samples[key] = stat
            group = case if case.endswith("-exact") else case+"-"+arm+"-"+rep
            groups[group][path.name] = (path, stat)
    by_case = {}
    for case in sorted({k.split("/")[0] for k in samples}):
        case_samples = [s for s in samples.values() if s["case"] == case]
        arms = {}
        for arm in (("A", "F") if case == "steady-500" or case.endswith("-repeat") else ARMS):
            arm_samples = sorted([s for s in case_samples if s["arm"] == arm], key=lambda s:s["repeat"])
            if not arm_samples:
                arms[arm] = {"repetitions": []}
                continue
            measurements = [s["measurement"] for s in arm_samples]
            arm_result = {"repetitions": [s["repeat"] for s in arm_samples]}
            if case.endswith("-exact"):
                operations = {}
                for operation in measurements[0]["operations"]:
                    metrics = [m["operations"][operation] for m in measurements]
                    operations[operation] = {k:distribution([r[k] for r in metrics]) for k in
                        ("wall_ms","app_cpu_ms","selected_rows","sql_statements","decode_bytes","encode_bytes","replayed_events","rss_after_kib")}
                arm_result["operations"] = operations
            else:
                arm_result["gates"] = [m["gate"] for m in measurements]
                arm_result["complete_agents"] = [m["agents"]["complete_agents"] for m in measurements]
                arm_result["actual_world_commits"] = [m["world_commits"] for m in measurements]
                arm_result["world_tps"] = [m["verified_world_tps"] for m in measurements]
            if arm != "A":
                matched = [(s["measurement"], samples[f"{case}/A/{s['repeat']}"]["measurement"])
                           for s in arm_samples if f"{case}/A/{s['repeat']}" in samples and
                           all(s["paired_against_A"][key] for key in ("same_fixture","same_harness","same_cpu_affinity"))]
                changes = {}
                if case.endswith("-exact"):
                    for op in measurements[0]["operations"]:
                        changes[op] = {k:paired_change(
                            [m["operations"][op][k] for m, _ in matched], [a["operations"][op][k] for _, a in matched])
                            for k in ("wall_ms","app_cpu_ms","sql_statements","selected_rows","decode_bytes","replayed_events")}
                else:
                    pairs = [(http_change_metrics(m), http_change_metrics(a)) for m,a in matched]
                    changes = {k:paired_change([m[k] for m,_ in pairs], [a[k] for _,a in pairs])
                               for k in http_change_metrics(measurements[0])}
                arm_result["paired_changes_vs_A"] = changes
            arms[arm] = arm_result
        by_case[case] = arms
    archives = {g:pack(output, g, members, previous.get("archives", {}).get(g, {}), verify) for g,members in sorted(groups.items())}
    for group, members in groups.items():
        archives[group]["case"] = next(iter(members.values()))[1]["case"]
    result = {"schema_version":"scion-round2-evidence-1", "arm_flags":{k:sorted(v) for k,v in SWITCHES.items()},
              "method":{"per_agent_rle":"For agents in index order, [consecutive agents, fulfilled operations, assigned operations].",
                  "postgres_cpu":"Not available in this harness. Raw pg_cluster_cpu_seconds omits backends when proc task children is unavailable.",
                  "sql_json":"Exact requests instrument psycopg encode/decode; HTTP SQL statistics cover statements, not decoded byte counts.",
                  "rss":"HTTP sums per-worker peaks, which need not coincide. Exact records also retain before/after and lifetime process peak.",
                  "paired_changes":"Per-replicate candidate minus A, shown as median/min/max of paired absolute and percentage changes. Zero baseline leaves the percentage undefined; one HTTP repeat is descriptive.",
                  "pairing":"Compare arms only within the same case, fixture, repeat, CPU affinity and harness. Outcome, family/sample size and measurement windows differ across cases.",
                  "http_identity":"First-attempt byte digests compared when both phases emit an operation_id; response-derived identifiers can differ across clones. Exact-profile requests and final state are independently validated.",
                  "archive":"Concatenate manifest parts in order and read tar.xz; each member reproduces the full uncompressed original JSON byte-for-byte."},
              "terminal_campaigns":campaign_records(runs), "cases":by_case,"samples":samples,"archives":archives}
    write_tables(output, by_case, samples)
    atomic_write(report_path, (json.dumps(result,ensure_ascii=False,sort_keys=True,separators=(",",":"))+"\n").encode())
    print(len(samples),"runs",len(archives),"archives",flush=True)


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs",type=Path)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--verify",action="store_true")
    parser.add_argument("--audit-archives",action="store_true")
    args=parser.parse_args()
    if not args.audit_archives and args.runs is None:
        parser.error("--runs or --audit-archives is required")
    args.output.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(args.output, os.O_RDONLY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH if args.audit_archives else fcntl.LOCK_EX)
        if args.audit_archives:
            audit_archives(args.output)
        else:
            main(args.runs,args.output,args.verify)
    finally:
        os.close(descriptor)
