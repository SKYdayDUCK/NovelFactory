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
    elif scenario in {"authorization_revoked", "budget_exhausted", "review_exhausted", "dependencies", "needs_input", "review_questions",
                      "review_mcp_exhausted", "verification_unknown"}:
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


def test_legal_needs_input_holds_without_accepting_payload_or_retrying_its_call():
    simulation = Simulation(scenario="needs_input", review_pass_rate=1)
    report = simulation.run()
    assert report["status"] == "PASS" and report["workflow_status"] == "STOPPED_WITH_BLOCKERS"
    held = next(row for row in report["trace"] if row["event"] == "INPUT_REQUIRED")
    task = simulation.jobs[held["task"]]
    assert task.state == "PAUSED" and task.reason == "INPUT_REQUIRED" and task.approved is None
    assert task.id not in simulation.acceptances
    assert sum(row["event"] == "HARNESS_REQUEST" and row["task"] == task.id for row in report["trace"]) == 1
    assert not any(row["event"] == "RETRY_SCHEDULED" and row["task"] == task.id for row in report["trace"])
    assert simulation.ledger[held["attempt"]]["state"] == "SUCCEEDED"
    assert report["frozen_chapters"] == report["frozen_books"] == report["unknown_reservations_held"] == 0


def test_succeeded_review_with_outer_questions_pauses_without_acceptance_retry_or_freeze():
    simulation = Simulation(scenario="review_questions", review_pass_rate=1)
    report = simulation.run()
    assert len(SCENARIOS) == len(set(SCENARIOS))
    assert report["status"] == "PASS" and report["workflow_status"] == "STOPPED_WITH_BLOCKERS"
    held = next(row for row in report["trace"] if row["event"] == "REVIEW_REQUIRED")
    task = simulation.jobs[held["task"]]
    assert task.stage == "review" and task.state == "PAUSED" and task.approved is None
    assert task.reason == "REVIEW_REQUIRED" and task.id not in simulation.acceptances
    assert sum(row["event"] == "HARNESS_REQUEST" and row["task"] == task.id for row in report["trace"]) == 1
    assert not any(row["event"] == "RETRY_SCHEDULED" and row["task"] == task.id for row in report["trace"])
    response = next(row for row in report["trace"] if row["event"] == "HARNESS_RESPONSE" and row["task"] == task.id)
    assert response["result_status"] == "SUCCEEDED"
    call = simulation.ledger[held["attempt"]]
    assert call["state"] == "SUCCEEDED" and call["unresolved_questions"] == held["unresolved_questions"]
    assert held["unresolved_questions"] == ["DRY_RUN_OUTSIDE_REVIEW_DECISION_REQUIRED"]
    assert report["frozen_chapters"] == report["frozen_books"] == report["unknown_reservations_held"] == 0


@pytest.mark.parametrize("scenario", ["internal_question_reread", "internal_question_strategy"])
def test_internal_question_runs_bound_query_and_verification_before_continuing_to_book_freeze(scenario):
    simulation = Simulation(scenario=scenario, chapters=1, review_pass_rate=1)
    report = simulation.run()
    assert report["status"] == "PASS" and report["frozen_books"] == 1
    trace = report["trace"]
    question = next(row for row in trace if row["event"] == "INTERNAL_QUESTION_ENROLLED")
    query = next(row for row in trace if row["event"] == "SIMULATED_QUERY_VERIFICATION")
    resolved = next(row for row in trace if row["event"] == "INTERNAL_QUESTION_RESOLVED")
    assert question["task"] == query["source_task"] == resolved["task"]
    assert query["source_ref"] == resolved["source_ref"] and resolved["world_effects_applied"] == 0
    assert trace.index(query) < trace.index(resolved) < next(i for i, row in enumerate(trace) if row["event"] == "CHAPTER_FROZEN")
    assert simulation.jobs[question["task"]].state == "CANCELLED"
    assert question["task"] not in simulation.acceptances
    if scenario == "internal_question_reread":
        reread = next(row for row in trace if row["event"] == "SAME_DRAFT_REREAD_ENROLLED")
        assert resolved["resolution"] == "NO_CONFLICT" and reread["source_ref"] == query["source_ref"]
        assert reread["task"] != question["task"] and report["stage_counts"]["draft"] == 1
    else:
        assert resolved["resolution"] == "CONFIRMED_CONFLICT" and report["stage_counts"]["strategy"] == 1
        assert report["stage_counts"]["draft"] == report["stage_counts"]["epistemic"] == 2
        assert report["chapter_manifests"][0]["revision"] == 1


@pytest.mark.parametrize("scenario", ["review_mcp_recovery", "review_mcp_exhausted"])
def test_missing_review_mcp_evidence_allows_only_two_rereads_and_keeps_known_call_receipts(scenario):
    simulation = Simulation(scenario=scenario, chapters=1, review_pass_rate=1)
    report = simulation.run()
    missing = [row for row in report["trace"] if row["event"] == "REVIEW_MCP_EVIDENCE_MISSING"]
    task = simulation.jobs[missing[0]["task"]]
    assert task.tries == 3
    assert sum(row["event"] == "REVIEW_MCP_REREAD_ENROLLED" for row in report["trace"]) == 2
    calls = [row for row in simulation.ledger.values() if row["task"] == task.id]
    assert len(calls) == 3 and all(row["state"] == "SUCCEEDED" and row["reserved"] == 0 for row in calls)
    if scenario == "review_mcp_recovery":
        assert len(missing) == 2 and task.state == "ACCEPTED" and report["frozen_books"] == 1
    else:
        assert len(missing) == 3 and task.state == "PAUSED" and task.approved is None
        assert task.reason == "REVIEW_QUERY_RETRIES_EXHAUSTED" and task.id not in simulation.acceptances
        assert report["frozen_chapters"] == report["frozen_books"] == 0


def test_unknown_verification_call_is_held_without_resend_or_manufactured_resolution():
    simulation = Simulation(scenario="verification_unknown", chapters=1, review_pass_rate=1)
    report = simulation.run()
    held = next(row for row in report["trace"] if row["event"] == "UNKNOWN_HELD")
    task = simulation.jobs[held["task"]]
    assert task.stage == "internal_verification" and task.state == "PAUSED" and task.tries == 1
    assert simulation.ledger[held["attempt"]]["state"] == "UNKNOWN" and report["unknown_reservations_held"] == 1
    assert sum(row["event"] == "HARNESS_REQUEST" and row["task"] == task.id for row in report["trace"]) == 1
    assert not any(row["event"] == "INTERNAL_QUESTION_RESOLVED" for row in report["trace"])
    assert report["frozen_books"] == report["frozen_chapters"] == 0


def test_query_receipt_from_another_source_cannot_resolve_an_internal_question():
    class WrongSource(Simulation):
        def _dispatch(self, job):
            super()._dispatch(job)
            if job.stage == "query_verification":
                job.query_receipt["source_ref"] = {"artifact_id": "another-draft"}
                job.query_receipt["sha256"] = self._receipt_hash(job.query_receipt)

    simulation = WrongSource(scenario="internal_question_reread", chapters=1, review_pass_rate=1)
    report = simulation.run()
    verifier = next(job for job in simulation.jobs.values() if job.stage == "internal_verification")
    assert verifier.state == "PAUSED" and verifier.id not in simulation.acceptances
    assert "INTERNAL_RESOLUTION_WITHOUT_QUERY_PROOF" in report["invariant_failures"]
    assert not any(row["event"] == "INTERNAL_QUESTION_RESOLVED" for row in report["trace"])


def test_hash_valid_but_wrong_source_review_mcp_receipt_does_not_pass_the_gate():
    class WrongReviewSource(Simulation):
        def _invoke(self, job_id, generation, attempt, outcome):
            response = super()._invoke(job_id, generation, attempt, outcome)
            receipt = response.raw_response.get("simulated_review_mcp_receipt")
            if receipt:
                receipt["source_ref"] = {"artifact_id": "another-draft"}
                receipt["sha256"] = self._receipt_hash(receipt)
            return response

    simulation = WrongReviewSource(scenario="review_mcp_recovery", chapters=1, review_pass_rate=1)
    report = simulation.run()
    assert report["status"] == "PASS" and report["workflow_status"] == "STOPPED_WITH_BLOCKERS"
    assert any(job.reason == "REVIEW_QUERY_RETRIES_EXHAUSTED" for job in simulation.jobs.values())
    assert report["frozen_books"] == 0


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


@pytest.mark.parametrize("mismatch", ["other_book", "older_edition"])
def test_book_freeze_rejects_approved_readers_of_a_different_final_edition(mismatch):
    simulation = Simulation(books=2, chapters=1, review_pass_rate=1)
    assert simulation.run()["workflow_status"] == "COMPLETED"
    release = next(job for job in simulation.jobs.values() if job.stage == "book_freeze" and job.book == 0)
    editor = simulation.jobs[release.dependencies[0]]
    if mismatch == "other_book":
        editor.dependencies = [job.id for job in simulation.jobs.values()
                               if job.book == 1 and job.stage in {"reader_1", "reader_2"}]
    else:
        # A new final-editor task was incorrectly wired to the approved old reads.
        newer = simulation.add("final_editor", 0, revision=1, dependencies=editor.dependencies, state="ACCEPTED")
        newer.approved = True
        release.dependencies = [newer.id]
    simulation.releases.pop(0)
    release.state = "READY"
    simulation._dispatch(release)
    assert 0 not in simulation.releases
    assert release.state == "FAILED" and release.reason == "BOOK_FREEZE_GATE_REJECTED"


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
