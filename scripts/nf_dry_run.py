"""Run the offline scheduler and normal Harness with explicit dry_run model requests."""

import argparse
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=("all", "novel", "idle", "random", "normal", "review_return", "review_exhausted",
        "rate_limit", "timeout", "unknown_reconciled", "crash_before_send", "crash_after_send", "invalid_json",
        "truncated", "lost_notification", "duplicate_notification", "duplicate_result", "late_result", "lease_expired",
        "restart", "authorization_revoked", "budget_exhausted", "dependencies", "storage_disconnect",
        "worker_pool_failure", "context_mismatch", "book_review_return"), default="novel")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--runs", type=int, default=1, help="Repeat with consecutive seeds")
    parser.add_argument("--books", type=int, default=1)
    parser.add_argument("--chapters", type=int, default=3)
    parser.add_argument("--agents", type=int, default=1000, help="Logical identities, never resident processes")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--review-pass-rate", type=float, default=0.7)
    parser.add_argument("--max-reworks", type=int, default=3)
    parser.add_argument("--max-steps", type=int, default=100000)
    parser.add_argument("--trace", action="store_true", help="Include every simulated state transition")
    parser.add_argument("--format", choices=("summary", "json"), default="summary")
    parser.add_argument("--output", type=Path, help="Write a new JSON report; never overwrite an existing path")
    parser.add_argument("--require-complete", action="store_true", help="Also fail if the configured workflow stops with blockers")
    args = parser.parse_args(argv)
    if (not 1 <= args.books <= 100 or not 1 <= args.chapters <= 1000 or not 1 <= args.agents <= 100000
            or not 1 <= args.workers <= 16 or not 1 <= args.runs <= 1000 or not 0 <= args.max_reworks <= 20
            or not 1 <= args.max_steps <= 2000000 or not math.isfinite(args.review_pass_rate)
            or not 0 <= args.review_pass_rate <= 1):
        parser.error("Invalid simulation bounds or review probability")

    # A process-level tripwire: an accidental fallback must fail, not start a
    # real provider CLI, open a database socket, or silently use HTTP.
    blocked = []
    def offline_only(event, _arguments):
        if event in {"socket.__new__", "socket.connect", "socket.getaddrinfo", "subprocess.Popen", "os.system"}:
            blocked.append(event)
            raise RuntimeError("DRY_RUN_EXTERNAL_IO_FORBIDDEN")
    sys.addaudithook(offline_only)
    from novel_factory.simulation.scheduler import SCENARIOS, Simulation

    scenarios = SCENARIOS if args.scenario == "all" else (("novel",) if args.scenario == "random" else (args.scenario,))
    if args.runs * len(scenarios) * args.books * (1 + args.chapters * (3 + 2 * args.max_reworks)) > 500000:
        parser.error("Simulation batch is too large; split the runs or reduce books/chapters/reworks")
    results = []
    for offset in range(args.runs):
        for scenario in scenarios:
            simulation = Simulation(scenario=scenario, seed=args.seed + offset,
                books=args.books, chapters=args.chapters, workers=args.workers, logical_agents=args.agents,
                review_pass_rate=args.review_pass_rate, max_reworks=args.max_reworks, max_steps=args.max_steps)
            result = simulation.run()
            if not args.trace:
                result.pop("trace")
            results.append(result)
    forbidden = [name for name in ("sqlalchemy", "psycopg", "redis", "sqlite3", "agent_runtime.credentials", "agent_runtime.native_codex")
                 if name in sys.modules]
    failures = [result["scenario"] for result in results if result["status"] != "PASS"]
    completed = all(result["workflow_status"] == "COMPLETED" for result in results)
    require_complete = args.require_complete or args.scenario in {"novel", "random"}
    passed = not (failures or blocked or forbidden) and (completed or not require_complete)
    report = {"schema_version": "nf.dry-run.v1", "status": "PASS" if passed else "FAIL",
        "mode": "DRY_RUN", "seed": args.seed, "runs": args.runs, "scenario_count": len(results),
        "harness_requests_total": sum(row["harness_requests"] for row in results),
        "success_goal": "BOOK_RELEASE_FREEZE", "completion_required": require_complete,
        "storage_backend": "IN_MEMORY", "upstream_model_calls": 0, "database_connections": 0,
        "external_io_attempts": blocked, "forbidden_imports": forbidden,
        "failed_scenarios": failures, "results": results,
        "scope": "Program scheduling and Harness simulation; no production DB, billing or literary acceptance is proven"}
    rendered = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if args.output:
        try:
            with args.output.open("x", encoding="utf-8") as target:
                target.write(rendered)
        except OSError:
            parser.error("Cannot create report; choose a new file in an existing writable directory")
    if args.format == "json":
        print(rendered, end="")
    else:
        print(f"{report['status']} | DRY_RUN | seed={args.seed} | scenarios={len(results)} | DB=0 | upstream=0")
        if len(results) <= 40:
            for result in results:
                print(f"{result['status']:4} {result['scenario']:24} {result['workflow_status']:21} "
                      f"harness_calls={result['harness_requests']:3} books_frozen={result['frozen_books']}/{result['requested_books']} "
                      f"unknown_held={result['unknown_reservations_held']}")
        else:
            for scenario in scenarios:
                rows = [row for row in results if row["scenario"] == scenario]
                count = sum(row["status"] == "PASS" for row in rows)
                completed_count = sum(row["workflow_status"] == "COMPLETED" for row in rows)
                print(f"{scenario:24} checks_passed={count}/{len(rows)} workflows_completed={completed_count}/{len(rows)}")
        print(report["scope"])
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
