"""A lost review can be replaced once while its unknown invoice stays untouched."""

from dataclasses import replace
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import text
from test_p5_supervisor import backend
from test_p6_rolling_production import answer, connect, request
from test_release import setup_batch

from novel_factory.application import production_pipeline as boards
from novel_factory.application import rolling_production as rolling
from novel_factory.application.lost_review import LostReviewRequest, replace_lost_review
from novel_factory.application.supervisor import RuntimeSupervisor
from novel_factory.application.volume_successor import _selected
from novel_factory.domain.errors import DomainError
from novel_factory.infrastructure.model_gateway import ModelCallFailure

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def lost_review(kernel):
    fixture = setup_batch(kernel, count=1)
    wr, owner = fixture["world_ref"], fixture["principal"]
    with backend() as (adapter, _calls, server):
        server.packet_candidate = answer
        generate = adapter.generate

        def send(packet):
            # This explicit failure injection represents a request already sent.
            if '"role_id":"reviewer"' in packet.messages[-1]["content"]:
                raise ModelCallFailure("MODEL_TRANSPORT_UNKNOWN")
            return replace(generate(packet), actual_amount=Decimal(0), currency="USD")

        adapter.generate = send
        gateway = connect(kernel, adapter)
        rolling.register_rolling(kernel, gateway, owner, **request(kernel, fixture, 1))
        for _ in range(30):
            RuntimeSupervisor(kernel, gateway).run_once(owner, wr["work_id"], wr["branch_id"])
            with kernel.db.uow(write=False) as repo:
                pending = repo.conn.execute(text("""SELECT c.* FROM nf_call_attempts c
                    JOIN nf_tasks t USING(task_id) WHERE t.work_id=:w AND c.state='UNKNOWN'"""),
                    {"w": wr["work_id"]}).mappings().all()
            if pending:
                break
        assert len(pending) == 1
        call = dict(pending[0])
        with kernel.db.uow() as repo:
            repo.conn.execute(text("UPDATE nf_call_attempts SET created_at=clock_timestamp()-interval '11 minutes' WHERE attempt_id=:a"),
                              {"a": call["attempt_id"]})
            task = repo.one("tasks", {"task_id": call["task_id"]})
            budget = repo.one("budgets", {"budget_id": call["budget_id"]})
            ref = _selected(repo, wr["work_id"], boards._key(wr["branch_id"]))
        recovery = LostReviewRequest(wr["work_id"], wr["branch_id"], str(call["attempt_id"]),
                                     call["generation"], ref["sha256"], True)
        return gateway, owner, recovery, task, budget


def test_replacement_preserves_unknown_ledger_and_fences_late_result(kernel_env):
    kernel = kernel_env["kernel"]
    gateway, owner, recovery, task, budget = lost_review(kernel)
    preview = replace_lost_review(kernel, gateway, owner, recovery)
    assert preview["status"] == "REPLACEMENT_PREVIEW" and preview["provider_calls_sent"] == 0
    with pytest.raises(DomainError, match="acknowledge|Acknowledge"):
        replace_lost_review(kernel, gateway, owner, replace(recovery, acknowledge_unknown_cost=False), apply=True)
    result = replace_lost_review(kernel, gateway, owner, recovery, apply=True)
    assert result["status"] == "REPLACEMENT_ENROLLED"
    assert replace_lost_review(kernel, gateway, owner, recovery, apply=True) == result
    with kernel.db.uow(write=False) as repo:
        assert repo.one("budgets", {"budget_id": budget["budget_id"]}) == budget
        old = repo.one("call_attempts", {"attempt_id": recovery.attempt_id})
        assert old["state"] == "UNKNOWN" and old["actual_amount"] is old["actual_tokens"] is None
        assert repo.one("tasks", {"task_id": task["task_id"]})["state"] == "CANCELLED"
        assert repo.one("tasks", {"task_id": result["new_task_id"]})["state"] == "READY"
        assert not repo.many("call_attempts", {"task_id": result["new_task_id"]})
        cursor = rolling._load(kernel, repo, recovery.work_id, recovery.branch_id)
        assert cursor["allowance"]["budget_id"] == result["new_budget_id"]
    late = {"schema_version": "2.0", "task_id": task["task_id"], "generation": task["generation"],
            "attempt_id": task["attempt_id"], "result_id": str(uuid4()),
            "context_bundle_id": task["context_bundle_id"], "dependency_fingerprint": task["dependency_fingerprint"],
            "status": "SUCCEEDED", "artifacts": [], "safe_summary": "Injected late result"}
    with pytest.raises(DomainError):
        kernel.tasks.store_result(late, worker_id=task["lease_owner"])
    with kernel.db.uow(write=False) as repo:
        assert repo.one("tasks", {"task_id": task["task_id"]})["current_result_id"] is None
        assert repo.one("task_results", {"result_id": late["result_id"]})["disposition"] == "LATE"


def test_recent_or_changed_handoff_cannot_be_replaced(kernel_env):
    kernel = kernel_env["kernel"]
    gateway, owner, recovery, task, budget = lost_review(kernel)
    with pytest.raises(DomainError) as changed:
        replace_lost_review(kernel, gateway, owner, replace(recovery, expected_board_sha256="0" * 64), apply=True)
    assert changed.value.code == "STALE_GENERATION"
    with kernel.db.uow() as repo:
        repo.conn.execute(text("UPDATE nf_call_attempts SET created_at=clock_timestamp() WHERE attempt_id=:a"),
                          {"a": recovery.attempt_id})
    with pytest.raises(DomainError) as recent:
        replace_lost_review(kernel, gateway, owner, recovery, apply=True)
    assert recent.value.code == "CALL_RECONCILIATION_REQUIRED"
    with kernel.db.uow(write=False) as repo:
        assert repo.one("budgets", {"budget_id": budget["budget_id"]}) == budget
        assert repo.one("tasks", {"task_id": task["task_id"]})["state"] == "PAUSED"
