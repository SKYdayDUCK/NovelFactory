"""Summarize original P2/P3 artifacts without changing their result."""
import collections
import hashlib
import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent

def summarize(result):
    aggregate = result.get("service_metrics", {})
    db = result.get("database_application", aggregate.get("database"))
    obs = result["database_observer"]
    resources = result.get("service_resources", aggregate.get("resources"))
    summaries = {}
    for kind in ("ingress", "authentication_admission", "admission"):
        entry = result.get(kind, aggregate.get(kind))
        summaries[kind] = {k: entry.get(k, 0) for k in ("admitted","completed","rejected","peak_queued","peak_active","wait_seconds_total")}
    raw_metrics = result.get("metrics")
    if raw_metrics:
        raw = [r for r in result["raw_requests"] if r["label"] == "overload" and r["attempt"] == 1]
        assert len(raw) == result["load"]["overload_rounds"] * result["logical_agent_task_count"]
        accepted = [r for r in raw if r["status"] == 200 and "error_type" not in r]
        wall_span = max(r["started_seconds"] + r["latency_ms"]/1000 for r in raw) - min(r["started_seconds"] for r in raw)
        phase = result["open_loop_offered_rate"]
        workload = {
            "call_starts_s": phase["actual_call_start_rps"],
            "header_starts_s": phase["transport_header_start_rps"],
            "overload_original_attempts": len(raw),
            "overload_original_counts": dict(collections.Counter(str(r["status"]) for r in raw)),
            "validated_original_successes": len(accepted),
            "overload_first_completion_window_s": wall_span,
            "overload_validated_per_first_completion_window": len(accepted)/wall_span,
            "first_call_rate_gate": phase["gate"],
            "correctness": result["engineer_correctness"],
            "recovery": result["bounded_recovery_business_gate"],
            "all_phase_planned_logical_operations": raw_metrics["planned_requests"],
            "all_phase_finished_logical_operations": raw_metrics["completed_logical_operations"],
            "all_phase_validated_successes_including_retries": raw_metrics["valid_successful_calls"],
            "all_phase_unique_world_commit_receipts": raw_metrics["new_world_commit_receipts"],
            "all_phase_target_world_commits": result["correctness"]["expected_new_world_revisions"],
            "all_phase_validated_success_rate": raw_metrics["api_success_rps"],
            "all_phase_final_unresolved_errors": len(raw_metrics["unresolved_operational_errors"]),
            "all_phase_client_validation_thread_cpu_s": raw_metrics["client_response_validation_cpu_seconds"],
            "all_phase_client_response_validation_overload_first_cpu_s": sum(r.get("client_response_validation_cpu_ms",0) for r in raw)/1000,
            "all_phase_first_statuses": raw_metrics["first_attempt_status_counts"], 
            "all_phase_latency_ms": raw_metrics["latency_all_responses"],
            "all_phase_httpx_pool_wait_ms": raw_metrics["client_pool_wait"],
            "client_process":result["resources"],
        }
    else:
        workload = {
            "call_starts_s": result["dispatch_start_rps"],
            "header_starts_s": result["observed_wire_start_rps"],
            "overload_original_attempts": result["scheduled_requests"],
            "overload_original_counts": result["status_counts"],
            "validated_original_successes": result["validated_successes"],
            "network_window_s": result["network_window_seconds"],
            "correct_query_rate":result["successful_query_rps"],
            "post_network_validation_wall_s":result["client_validation_seconds"],
            "all_phase_latency_ms": result["latency_all_attempts"],
            "wire_rate_gate":result["offered_wire_rate_gate"],
            "world_unchanged":result["world_unchanged"],
            "new_world_commits": result["new_world_commits"],
            "correctness_errors":result["correctness_errors"],
        }
    all_obs = [r for s in obs["samples"] for r in s["connections"]]
    sampled = collections.Counter()
    for row in all_obs:
        sampled[row.get("wait_event_type") or "active/none"] += row["count"]
    sql = {k:db[k] for k in ("sql_statements","transactions","sql_statements_per_second","sql_cursor_execution_seconds","peak_checked_out_connections","configured_pool_size","checked_out_at_end","failed_statements")}
    return {
        "tested_sha":result["environment"]["git_sha"], "driver_sha256":result.get("source_sha256",result["environment"]["benchmark_source_sha256"]),
        "cpu_models": result["environment"]["cpu_models"], "versions":result["environment"]["versions"],
        "affinity":result["environment"]["affinity_cpus"],
        "capacity_gate":result["capacity_gate"], **workload, 
        "server": {"admissions":summaries,"process_resources":resources,"db":sql,
            "deadlock_delta":obs["after"]["stats"]["deadlocks"]-obs["before"]["stats"]["deadlocks"],
            "observations":len(obs["samples"]), "sampled_connection_wait_type":dict(sampled),
            "observer_errors":obs["observer_errors"],
            "wal_bytes":obs["wal_bytes"]},
    }

def hosted():
    rows=[]
    for artifact_id,name,run_id,digest in json.loads((ROOT/"artifacts.json").read_text()):
        path=ROOT/f"raw/{artifact_id}.zip"
        expected="sha256:"+hashlib.sha256(path.read_bytes()).hexdigest()
        assert expected == digest
        with zipfile.ZipFile(path) as archive:
            for member in sorted(archive.namelist()):
                if member.endswith(".json"):
                    row=summarize(json.loads(archive.read(member)))
                    rows.append({"source":f"https://github.com/SKYdayDUCK/NovelFactory/actions/runs/{run_id}/artifacts/{artifact_id}",
                                 "source_sha256":digest, "job":name, "member":member, **row})
    return rows

def local():
    rows=[]
    for path in sorted((ROOT/"local-p2").glob("*/*/load.json")):
        label=path.parent.relative_to(ROOT/"local-p2").as_posix()
        data=json.loads(path.read_bytes())
        row=summarize(data)
        processes=[json.loads(p.read_bytes()) for p in sorted(path.parent.glob("process-*.json"))]
        client=[p for p in processes if p["role"]=="client"]
        api=[p for p in processes if p["role"]=="api"]
        assert len(api)==len(client)==1
        assert row["tested_sha"]==client[0]["sha"]=="c4f54c0684d366b6c909d97efbe25d878e628d09"
        assert api[0]["sha"]==(client[0]["sha"] if path.parent.name.startswith("a") else "d03dd07ddb4481a6243088f36a46cab37715e198")
        assert row["affinity"]==[4,5,6,7]
        assert data["database_settings"]["server_version"].startswith("17.11")
        assert data["database_settings"]["fsync"]==data["database_settings"]["synchronous_commit"]=="on"
        assert int((path.parent/"original.exit").read_text()) == int(row["capacity_gate"] != "PASS")
        rows.append({"arm":label,"client_sha":client[0]["sha"],"api_sha":api[0]["sha"], 
                     "source_sha256":hashlib.sha256(path.read_bytes()).hexdigest(),**row})
    return rows

if __name__=="__main__":
    (ROOT/"hosted-summary.json").write_text(json.dumps(hosted(),indent=2)+"\n")
    (ROOT/"local-summary.json").write_text(json.dumps(local(),indent=2)+"\n")
