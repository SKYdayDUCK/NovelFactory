"""Offline control-flow gates, including expected failure and idempotent recovery."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from novel_factory.simulation.scheduler import SCENARIOS, Simulation


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_each_fault_path_preserves_scheduling_invariants(scenario):
    report = Simulation(scenario=scenario, seed=42, review_pass_rate=1).run()
    assert report["status"] == "PASS", report["invariant_failures"]
    assert report["max_inflight"] <= report["worker_limit"]
    assert report["idle_worker_pools"] == 0
    assert report["database_connections"] == report["upstream_model_calls"] == 0
    if scenario == "idle":
        assert report["harness_requests"] == 0 and report["worker_pool_events"] == []
    elif scenario in {"timeout", "crash_after_send"}:
        assert report["unknown_reservations_held"] == 1
        unknown = next(row for row in report["trace"] if row["event"] == "UNKNOWN_HELD")
        assert sum(row["event"] == "HARNESS_REQUEST" and row["task"] == unknown["task"] for row in report["trace"]) == 1
    elif scenario in {"authorization_revoked", "budget_exhausted", "review_exhausted", "dependencies"}:
        assert report["workflow_status"] == "STOPPED_WITH_BLOCKERS"
    else:
        assert report["workflow_status"] == "COMPLETED" and report["frozen_chapters"] == 3
        assert report["frozen_books"] == 1 and report["releases"][0]["status"] == "FROZEN"
    if scenario == "unknown_reconciled":
        assert report["unknown_reservations_held"] == 0
        assert sum(row["event"] == "SIMULATED_PROVIDER_NOT_EXECUTED_PROOF" for row in report["trace"]) == 1
    if scenario in {"late_result", "lease_expired"}:
        assert report["late_results_rejected"] == 1
    if scenario == "duplicate_result":
        assert report["duplicate_results_ignored"] > 0
    if scenario == "lost_notification":
        assert any(row["event"] == "DURABLE_SCAN" and row["source"] == "periodic" for row in report["trace"])


def test_fixed_seed_is_replayable_and_random_review_can_exhaust_its_limit():
    assert Simulation(seed=731).run() == Simulation(seed=731).run()
    report = Simulation(scenario="normal", review_pass_rate=0, max_reworks=2, chapters=1).run()
    assert report["status"] == "PASS" and report["workflow_status"] == "STOPPED_WITH_BLOCKERS"
    assert sum(row["event"] == "REWORK_ENROLLED" for row in report["trace"]) == 2
    assert report["frozen_chapters"] == report["frozen_books"] == 0


def test_real_model_routing_is_simulated_through_the_normal_adapter():
    report = Simulation(seed=42, review_pass_rate=1).run()
    calls = [row for row in report["trace"] if row["event"] == "HARNESS_REQUEST"]
    assert {row["model"] for row in calls} == {"deepseek-flash", "gpt-6.1-sol"}
    assert all(row["dry_run"] for row in calls)


def test_check_fails_for_deliberate_duplicate_acceptance():
    simulation = Simulation(review_pass_rate=1)
    report = simulation.run()
    assert report["status"] == "PASS"
    simulation.acceptances[next(iter(simulation.acceptances))] = 2
    simulation._check(final=True)
    assert simulation.report()["status"] == "FAIL"
    assert "DUPLICATE_ACCEPTANCE" in simulation.report()["invariant_failures"]


def test_cycle_cannot_silently_pass_as_a_normal_wait():
    simulation = Simulation(chapters=1)
    plan = next(job for job in simulation.jobs.values() if job.stage == "plan")
    freeze = next(job for job in simulation.jobs.values() if job.stage == "freeze")
    plan.dependencies, plan.state = [freeze.id], "BLOCKED"
    report = simulation.run()
    assert report["status"] == "FAIL" and "UNEXPECTED_DEADLOCK" in report["invariant_failures"]


def test_goal_includes_independent_whole_book_review_and_final_release():
    report = Simulation(scenario="novel", seed=42, books=3, chapters=5, workers=4).run()
    assert report["workflow_status"] == "COMPLETED"
    assert report["frozen_books"] == report["requested_books"] == 3
    assert report["frozen_chapters"] == 15
    calls = {row["task"]: row for row in report["trace"] if row["event"] == "HARNESS_REQUEST"}
    for release in report["releases"]:
        readers = [calls[key] for key in release["independent_readers"]]
        assert len({row["logical_agent_id"] for row in readers}) == 2
        assert all(row["source_ref"] == release["reviewed_edition_ref"] for row in readers)
        assert calls[release["editor_task_id"]]["model"] == "gpt-6.1-sol"
        assert len(release["chapter_manifests"]) == 5


def test_returned_book_revises_and_reaudits_before_new_independent_reviews():
    report = Simulation(scenario="book_review_return", chapters=1, review_pass_rate=1).run()
    assert report["frozen_books"] == 1 and report["releases"][0]["edition"] == 1
    assert report["stage_counts"]["book_revision"] == report["stage_counts"]["book_facts"] == 1
    assert report["stage_counts"]["reader_1"] == report["stage_counts"]["reader_2"] == 2


def test_book_cannot_freeze_before_chapters_and_final_review():
    simulation = Simulation(chapters=1)
    release = next(job for job in simulation.jobs.values() if job.stage == "book_freeze")
    simulation._dispatch(release)
    assert not simulation.releases
    assert "BOOK_FREEZE_GATE_REJECTED" in simulation.problems


def test_cli_without_site_packages_ignores_live_connections_and_is_offline(tmp_path):
    root = Path(__file__).resolve().parents[2]
    env = {**os.environ, "NF_DATABASE_URL": "postgresql://must-not-connect.invalid/forbidden",
           "NF_REDIS_URL": "redis://must-not-connect.invalid/0", "NF_RUNTIME_LIVE_APPROVED": "1",
           "NF_RUNTIME_PROFILES": str(tmp_path / "must-not-read.json")}
    completed = subprocess.run([sys.executable, "-S", str(root / "scripts/nf.py"), "dry-run", "--scenario", "all", "--seed", "42", "--format", "json"],
                               cwd=tmp_path, env=env, capture_output=True, text=True, timeout=20, check=False)
    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    assert report["scenario_count"] == len(SCENARIOS)
    assert report["external_io_attempts"] == report["forbidden_imports"] == []


def test_cli_can_require_completion_and_rejects_invalid_parameters():
    root = Path(__file__).resolve().parents[2]
    command = [sys.executable, "-S", str(root / "scripts/nf.py"), "dry-run", "--format", "json"]
    failed = subprocess.run([*command, "--scenario", "normal", "--review-pass-rate", "0", "--require-complete"],
                            capture_output=True, text=True, timeout=20, check=False)
    assert failed.returncode == 1 and json.loads(failed.stdout)["status"] == "FAIL"
    for bad in ("nan", "-0.1", "1.1"):
        invalid = subprocess.run([*command, "--review-pass-rate", bad], capture_output=True, text=True, timeout=20, check=False)
        assert invalid.returncode == 2
