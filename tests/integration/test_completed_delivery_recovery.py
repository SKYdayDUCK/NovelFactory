"""A proved legacy memory collision may adopt its exact completed native return."""
from dataclasses import replace

import pytest
from test_production_strategy import synthetic_production

from novel_factory.application import delivery_recovery
from novel_factory.application import production_pipeline as boards
from novel_factory.application.authorization import Principal
from novel_factory.application.delivery_recovery import (
    CompletedDeliveryRecovery,
    resume_completed_delivery,
)
from novel_factory.application.quality_memory import _record, plan_repair, reflect_repair
from novel_factory.application.supervisor import RuntimeSupervisor
from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import point, run_command

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def held_reading(kernel, monkeypatch, *, legacy=True):
    gateway, owner, wr, _, _ = synthetic_production(kernel, monkeypatch)
    for _ in range(45):
        RuntimeSupervisor(kernel, gateway).run_once(owner, wr["work_id"], wr["branch_id"])
        with kernel.db.uow(write=False) as repo:
            board = boards._load(kernel, repo, wr["work_id"], wr["branch_id"])
            node = next(node for node in board["nodes"].values() if node["stage"] == "review")
            task = node.get("task_id") and repo.one("tasks", {"task_id": node["task_id"]})
            if task and task["state"] == "RESULT_STORED":
                break
    assert task and task["state"] == "RESULT_STORED"
    with kernel.objects.lock(), kernel.db.uow() as repo:
        board = boards._load(kernel, repo, wr["work_id"], wr["branch_id"])
        node = next(node for node in board["nodes"].values() if node.get("task_id") == task["task_id"])
        writer = board["nodes"][node["parents"][0]]
        call = repo.one("call_attempts", {"task_id": writer["task_id"], "call_key": "supervised-candidate"})
        audit = gateway._read_audit(repo, call["attempt_id"])
        plan = reflect_repair(kernel, repo, owner, wr, writer["task_id"], call["attempt_id"],
            {key: audit[key] for key in ("artifact_id", "artifact_revision_id", "sha256")},
            plan_repair("editorial", ["Synthetic historical finding"], []), "SYNTHETIC_LEGACY_COLLISION")
        writer.setdefault("repair_plans", []).append({**plan, "review_stage": "review"})
        previous = next(node for node in board["nodes"].values() if node["stage"] == "epistemic")
        reader = repo.one("tasks", {"task_id": previous["task_id"]})
        previous_call = repo.one("call_attempts", {"task_id": reader["task_id"], "call_key": "supervised-candidate"})
        previous_audit = gateway._read_audit(repo, previous_call["attempt_id"])["payload"]
        advice = boards._artifact(kernel, repo, wr["work_id"], owner, "CHAPTER_READING_ADVICE", {
            "draft_refs": [writer["output_ref"]], "generation": reader["generation"], "task_id": reader["task_id"],
            "candidate_ref": previous_audit["output_ref"], "literary_quality": "NOT_EVALUATED"})
        if legacy:
            _record(kernel, repo, wr["work_id"], owner.principal_id, call["attempt_id"], "reviewed-local-repair", {
                "world_ref": wr, "phase": "STAGE_COMPLETED", "attempt_id": call["attempt_id"],
                "error_ref": plan["error_ref"], "actual_fix": writer["output_ref"], "regression_result": advice,
                "status": "LOCAL_VALIDATION_ONLY", "reusable": False}, "P6_REFLECTION_RECORD")
        node.update(state="STOPPED", reason="VIEW_DENIED")
        board_ref = boards._save(kernel, repo, owner, board)
    request = CompletedDeliveryRecovery(wr, board_ref["sha256"], task["task_id"], task["generation"],
                                       task["current_result_id"], "1" * 40)
    return gateway, owner, wr, board, task, request


def state(kernel, wr, task_id):
    with kernel.db.uow(write=False) as repo:
        return {"board": boards._load(kernel, repo, wr["work_id"], wr["branch_id"]),
                "task": repo.one("tasks", {"task_id": task_id}), "calls": repo.many("call_attempts", {}),
                "budgets": repo.many("budgets", {"work_id": wr["work_id"]}), "head": kernel.head(repo, wr["work_id"], wr["branch_id"])}


def test_completed_native_return_resumes_once_without_model_or_accounting_change(kernel_env, monkeypatch):
    kernel = kernel_env["kernel"]
    gateway, owner, wr, _, task, request = held_reading(kernel, monkeypatch)
    before = state(kernel, wr, task["task_id"])
    preview = resume_completed_delivery(kernel, gateway, owner, request, idempotency_key="completed-read")
    assert preview["status"] == "COMPLETED_DELIVERY_RECOVERY_PREVIEW"
    assert state(kernel, wr, task["task_id"]) == before
    result = resume_completed_delivery(kernel, gateway, owner, request, idempotency_key="completed-read", apply=True)
    assert result["status"] == "COMPLETED_DELIVERY_RECOVERED" and result["state"] == "ADVISORY_REVIEW"
    assert result["provider_calls_sent"] == 0 and result["legacy_collision_refs"]
    after = state(kernel, wr, task["task_id"])
    assert after["task"]["state"] == "ACCEPTED"
    assert all(after[key] == before[key] for key in ("calls", "budgets", "head"))
    assert resume_completed_delivery(kernel, gateway, owner, request, idempotency_key="completed-read", apply=True) == result
    assert state(kernel, wr, task["task_id"]) == after


@pytest.mark.parametrize("failure,code", [("generation", "STALE_GENERATION"), ("board", "STALE_GENERATION"),
    ("result", "STALE_GENERATION"), ("cut", "BRANCH_CHANGED"), ("non_admin", "VIEW_DENIED"),
    ("incomplete_paid", "CALL_RECONCILIATION_REQUIRED"), ("no_collision", "INVALID_EVIDENCE")])
def test_recovery_rejects_changed_or_unproved_completed_return(kernel_env, monkeypatch, failure, code):
    from uuid import uuid4

    kernel = kernel_env["kernel"]
    gateway, owner, wr, board, task, request = held_reading(kernel, monkeypatch, legacy=failure != "no_collision")
    if failure == "generation":
        request = replace(request, expected_generation=task["generation"] + 1)
    elif failure == "board":
        request = replace(request, expected_board_sha256="0" * 64)
    elif failure == "result":
        request = replace(request, expected_result_id=str(uuid4()))
    elif failure == "cut":
        run_command(kernel, owner, wr, "SCHEDULE_ADVANCE", {"scope_ids": board["subject_ids"], "target": point(3, 1)})
    elif failure == "non_admin":
        owner = Principal(task["grant_id"], task["principal_id"])
    elif failure == "incomplete_paid":
        with kernel.db.uow() as repo:
            call = repo.one("call_attempts", {"task_id": task["task_id"], "call_key": "supervised-candidate"})
            repo.change("host_turns", {"attempt_id": call["attempt_id"]}, {"phase": "TURN_STARTED"})
    before = state(kernel, wr, task["task_id"])
    with pytest.raises(DomainError) as error:
        resume_completed_delivery(kernel, gateway, owner, request, idempotency_key="reject-completed-read", apply=True)
    assert error.value.code == code
    assert state(kernel, wr, task["task_id"]) == before


def test_failure_after_delivery_rolls_back_task_and_board(kernel_env, monkeypatch):
    kernel = kernel_env["kernel"]
    gateway, owner, wr, _, task, request = held_reading(kernel, monkeypatch)
    before = state(kernel, wr, task["task_id"])

    def fail_save(*_args, **_kwargs):
        raise DomainError("SYNTHETIC_SAVE_FAULT", "Synthetic failure after delivery")

    monkeypatch.setattr(delivery_recovery, "_save", fail_save)
    with pytest.raises(DomainError, match="Synthetic failure"):
        resume_completed_delivery(kernel, gateway, owner, request, idempotency_key="fail-completed-read", apply=True)
    assert state(kernel, wr, task["task_id"]) == before
