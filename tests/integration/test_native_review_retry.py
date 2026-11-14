"""Unproven native reviews reread unchanged prose within a finite controller budget."""
import json

import pytest
import test_production_strategy as strategy_tests

from novel_factory.application import production_pipeline as boards
from novel_factory.application.supervisor import RuntimeSupervisor

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def production_with_unqueried_review(kernel, monkeypatch, *, always_missing=False):
    actual_mcp = strategy_tests.run_review_mcp
    missing, attempts = [], []
    def simulated_model_read(kernel, packet, thread, turn):
        with kernel.db.uow(write=False) as repo:
            call = repo.one("call_attempts", {"attempt_id": packet.attempt_id})
            task = repo.one("tasks", {"task_id": call["task_id"]})
        if task["task_kind"] == "epistemic":
            supplied = json.loads(packet.messages[1]["content"])
            assert supplied["authorized_materials"]
            assert all(json.loads(item["content"])["delivery"] == "MCP_REQUIRED"
                       and "text" not in json.loads(item["content"]) for item in supplied["authorized_materials"])
            attempts.append(task["task_id"])
            if always_missing or not missing:
                missing.append(task["task_id"])
                return []
        return actual_mcp(kernel, packet, thread, turn)
    monkeypatch.setattr(strategy_tests, "run_review_mcp", simulated_model_read)
    gateway, owner, wr, calls, _ = strategy_tests.synthetic_production(kernel, monkeypatch)
    return gateway, owner, wr, calls, missing, attempts


def test_missing_mcp_rereads_review_without_rewriting_or_losing_completed_candidate(kernel_env, monkeypatch):
    kernel = kernel_env["kernel"]
    gateway, owner, wr, calls, missing, attempts = production_with_unqueried_review(kernel, monkeypatch)
    board, _ = strategy_tests.drive(kernel, gateway, owner, wr,
        until=lambda board: any(node.get("reason") == "REVIEW_EVIDENCE_REREAD" for node in board["nodes"].values()))
    node = next(node for node in board["nodes"].values() if node.get("reason") == "REVIEW_EVIDENCE_REREAD")
    assert node["state"] == "PENDING" and len(node["evidence_repairs"]) == 1
    rejected = node["evidence_repairs"][0]
    writer = next(node for node in board["nodes"].values() if node["stage"] == "draft")
    draft_ref = writer["output_ref"]
    with kernel.db.uow(write=False) as repo:
        old_task = repo.one("tasks", {"task_id": rejected["task_id"]})
        call = repo.one("call_attempts", {"attempt_id": rejected["attempt_id"]})
        audit = gateway._read_audit(repo, call["attempt_id"])["payload"]
        assert old_task["state"] == "WAITING_REVIEW" and old_task["current_result_id"]
        assert call["state"] == "UNKNOWN" and audit["output_ref"] == rejected["candidate_ref"]
        assert repo.one("host_turns", {"attempt_id": call["attempt_id"]})["phase"] == "COMPLETED"
        assert kernel.artifact(repo, rejected["candidate_ref"], wr["work_id"], kind="AGENT_CANDIDATE")
    final, cursor = strategy_tests.drive(kernel, gateway, owner, wr)
    assert cursor["frozen_chapters"] == 1 and len(missing) == 1 and len(attempts) == 2
    final_writer = next(node for node in final["nodes"].values() if node["stage"] == "draft")
    assert final_writer["output_ref"] == draft_ref and not final_writer.get("revision_count")
    assert len([identity for identity, _, _ in calls if identity["role_id"] == "writer"]) == 1
    with kernel.db.uow(write=False) as repo:
        assert repo.one("call_attempts", {"attempt_id": call["attempt_id"]}) == call
        assert kernel.artifact(repo, rejected["candidate_ref"], wr["work_id"], kind="AGENT_CANDIDATE")


def test_repeated_unqueried_reviews_exhaust_two_rereads_without_more_model_calls(kernel_env, monkeypatch):
    kernel = kernel_env["kernel"]
    gateway, owner, wr, calls, missing, attempts = production_with_unqueried_review(kernel, monkeypatch, always_missing=True)
    board, _ = strategy_tests.drive(kernel, gateway, owner, wr,
        until=lambda board: any(node.get("reason") == "REVIEW_EVIDENCE_REPAIR_EXHAUSTED" for node in board["nodes"].values()))
    stopped = next(node for node in board["nodes"].values() if node.get("reason") == "REVIEW_EVIDENCE_REPAIR_EXHAUSTED")
    assert stopped["state"] == "STOPPED" and len(stopped["evidence_repairs"]) == 2
    assert len(missing) == len(attempts) == 3  # Initial attempt plus two separately preserved rereads.
    before = len(calls)
    for _ in range(10):
        RuntimeSupervisor(kernel, gateway).run_once(owner, wr["work_id"], wr["branch_id"])
    assert len(calls) == before
    with kernel.db.uow(write=False) as repo:
        final = boards._load(kernel, repo, wr["work_id"], wr["branch_id"])
        writer = next(node for node in final["nodes"].values() if node["stage"] == "draft")
        assert not writer.get("revision_count")
        for task_id in attempts:
            call = repo.one("call_attempts", {"task_id": task_id})
            assert call["state"] == "UNKNOWN"
            assert gateway._read_audit(repo, call["attempt_id"])["payload"]["output_ref"]
