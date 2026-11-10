"""Offered-load arithmetic for a design package, never a performance benchmark.

No database, HTTP server, Agent Host or language model is invoked.
Run: python benchmarks/calculate_load.py
"""
from __future__ import annotations
import argparse
import csv
import hashlib
import json
import math
import platform
import random
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent


def allocate(population, roles):
    raw = [population * r["share"] for r in roles]
    counts = [math.floor(n) for n in raw]
    remaining = population - sum(counts)
    order = sorted(range(len(roles)), key=lambda i: (-(raw[i] - counts[i]), i))
    for index in order[:remaining]:
        counts[index] += 1
    if sum(counts) != population:
        raise ValueError("ALLOCATION_ERROR")
    return dict(zip((r["role"] for r in roles), counts))


def summary(config, population, cache_hit):
    counts = allocate(population, config["roles"])
    metrics = Counter()
    by_operation = Counter()
    for role in config["roles"]:
        cycles = counts[role["role"]] / role["cycle_seconds"]
        for kind, calls, _phase in role["requests"]:
            offered = cycles * calls
            by_operation[kind] += offered
            metrics["agent_tool_rps"] += offered
            sql = config["operation_sql_assumptions"][kind]
            metrics["database_read_statement_demand_rps"] += offered * (
                sql["uncached_reads"] + (1-cache_hit) * sql["cacheable_reads"])
            metrics["database_write_statement_demand_rps"] += offered * sql["writes"]
        commit_commands = cycles * role["canonical_commands_per_cycle"]
        metrics["canonical_command_demand_rps"] += commit_commands
        sql = config["canonical_command_sql_assumption"]
        metrics["database_read_statement_demand_rps"] += commit_commands * sql["reads"]
        metrics["database_write_statement_demand_rps"] += commit_commands * sql["writes"]
        model_rate = cycles * role["model_calls_per_cycle"]
        metrics["model_call_demand_rps"] += model_rate
        metrics["model_inflight_demand"] += model_rate * role["model_seconds_per_call"]
        metrics["input_token_demand_per_hour"] += model_rate * role["input_tokens_per_call"] * 3600
        metrics["output_token_demand_per_hour"] += model_rate * role["output_tokens_per_call"] * 3600
    metrics["database_total_statement_demand_rps"] = (
        metrics["database_read_statement_demand_rps"] +
        metrics["database_write_statement_demand_rps"])
    return {
        "population": population, "role_counts": counts,
        "assumed_cache_hit_ratio": cache_hit,
        **{key: round(value, 6) for key, value in metrics.items()},
        "by_operation_offered_rps": {key: round(value, 6) for key, value in by_operation.items()},
        "accepted_commit_tps": None, "measured_latency_ms": None, "measured_cost": None,
    }


def synthetic_arrivals(config, population):
    """Count a seeded virtual schedule. Time is arithmetic; this function never sleeps."""
    rng = random.Random(config["seed"] + population)
    bins = Counter()
    counts = allocate(population, config["roles"])
    horizon = config["horizon_seconds"]
    for role in config["roles"]:
        cycle = role["cycle_seconds"]
        for _agent in range(counts[role["role"]]):
            start = rng.uniform(0, cycle)
            while start < horizon:
                interval = cycle * rng.uniform(.75, 1.25)
                for _kind, calls, phase in role["requests"]:
                    for item in range(calls):
                        arrival = start + interval * phase + item * .025
                        if arrival < horizon:
                            bins[int(arrival)] += 1
                start += interval
    values = sorted(bins.get(i, 0) for i in range(horizon))
    return {
        "synthetic_calls": sum(values),
        "synthetic_mean_offered_rps": round(sum(values)/horizon, 6),
        "synthetic_max_offered_calls_in_one_second": max(values),
        "synthetic_p95_offered_calls_in_one_second": values[math.ceil(.95*horizon)-1],
        "note": "Traffic schedule counts only; p95 here is an arrival count, not response latency.",
    }


def closed_sensitivity(config, population, extra_seconds):
    counts = allocate(population, config["roles"])
    rate = 0
    for role in config["roles"]:
        calls = sum(item[1] for item in role["requests"])
        rate += counts[role["role"]] * calls / (role["cycle_seconds"] + calls*extra_seconds)
    return round(rate, 6)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=HERE/"workload_model.json")
    parser.add_argument("--output-dir", type=Path, default=HERE)
    args = parser.parse_args()
    raw = args.config.read_bytes()
    config = json.loads(raw)
    if not math.isclose(sum(r["share"] for r in config["roles"]), 1):
        raise ValueError("ROLE_SHARES_MUST_SUM_TO_ONE")
    rows = []
    detail = []
    for population in config["agent_populations"]:
        cache_rows = {}
        for cache in config["cache_profiles"]:
            row = summary(config, population, cache["cache_hit_ratio"])
            row["cache_profile"] = cache["name"]
            cache_rows[cache["name"]] = row
            rows.append({k:v for k,v in row.items() if k not in {"role_counts","by_operation_offered_rps"}})
        offered = cache_rows["cold"]["agent_tool_rps"]
        sensitivity = [
            {"extra_seconds_per_tool": sec,
             "closed_loop_offered_rps": closed_sensitivity(config, population, sec),
             "open_loop_scheduled_rps_at_1x": offered}
            for sec in config["latency_sensitivity_added_seconds_per_tool"]
        ]
        detail.append({
            "population":population, "cache_cases":cache_rows,
            "seeded_schedule":synthetic_arrivals(config,population),
            "open_loop_targets_rps":{str(m):round(offered*m,6) for m in config["open_loop_multipliers"]},
            "closed_vs_open_arithmetic":sensitivity,
        })
    pool = config["pool_budget_example"]
    client_max = pool["api_processes"]*(pool["api_pool_size"]+pool["api_max_overflow"]) + (
        pool["worker_processes"]*(pool["worker_pool_size"]+pool["worker_max_overflow"]))
    result = {
        "status":"MODEL_CALCULATED_NOT_SERVICE_BENCHMARK",
        "python":platform.python_version(), "input_sha256":hashlib.sha256(raw).hexdigest(),
        "seed":config["seed"], "scope":"No target service, SQL, network or LLM calls were made.",
        "checks":{
            "role_allocation_preserves_population":all(sum(allocate(n,config["roles"]).values())==n for n in config["agent_populations"]),
            "warm_profile_reduces_estimated_reads":all(d["cache_cases"]["warm"]["database_read_statement_demand_rps"] < d["cache_cases"]["cold"]["database_read_statement_demand_rps"] for d in detail),
            "closed_loop_slows_with_added_latency":all(d["closed_vs_open_arithmetic"][0]["closed_loop_offered_rps"] > d["closed_vs_open_arithmetic"][-1]["closed_loop_offered_rps"] for d in detail),
            "open_loop_schedule_is_unchanged":all(len({r["open_loop_scheduled_rps_at_1x"] for r in d["closed_vs_open_arithmetic"]})==1 for d in detail),
        },
        "illustrative_pool_budget":{"application_connection_cap":client_max,
            "including_reserved":client_max+pool["maintenance_reserved"],
            "configured_pg_limit":pool["configured_postgres_max_connections"],
            "fits_arithmetic_budget":client_max+pool["maintenance_reserved"]<=pool["configured_postgres_max_connections"],
            "actual_connection_occupancy":None},
        "cases":detail,
        "limitations":[
            "These are proposed traffic and fanout assumptions, not measured user traces.",
            "No accepted throughput, latency, CPU, memory, I/O, correctness or provider capacity was measured.",
            "Batching, retries, queue wait, failed calls and cache invalidation alter real demand.",
            "In-flight model demand assumes steady state before quotas; real quota queues must be observed.",
        ],
    }
    args.output_dir.mkdir(parents=True,exist_ok=True)
    (args.output_dir/"load_model_result.json").write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n")
    with (args.output_dir/"load_model_table.csv").open("w",newline="",encoding="utf-8") as handle:
        writer=csv.DictWriter(handle,fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    if not all(result["checks"].values()):
        raise SystemExit("MODEL_CHECK_FAILED")
    print(json.dumps({"status":result["status"],"checks":result["checks"],"populations":len(detail),"cache_rows":len(rows)},ensure_ascii=False))


if __name__=="__main__":
    main()
