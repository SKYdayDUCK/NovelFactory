"""Legacy CLEAR migration never manufactures query proof or resends an old call."""
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import text
from test_production_strategy import drive, synthetic_production

from novel_factory.application import production_pipeline as boards
from novel_factory.application.authorization import Principal
from novel_factory.application.reading_mcp_upgrade import ReadingMcpUpgrade, upgrade_reading_policy
from novel_factory.application.supervisor import RuntimeSupervisor
from novel_factory.application.volume_successor import _selected
from novel_factory.domain.errors import DomainError

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def legacy_clear(kernel, monkeypatch):
    original = RuntimeSupervisor.install_job

    def legacy_job(repo, task, config):
        config = dict(config)
        config.pop("required_review_mcp", None)
        return original(repo, task, config)

    monkeypatch.setattr(RuntimeSupervisor, "install_job", staticmethod(legacy_job))
    gateway, owner, wr, calls, _ = synthetic_production(kernel, monkeypatch)
    board, _ = drive(kernel, gateway, owner, wr, until=lambda board: any(
        n["state"] == "KNOWLEDGE_CLEAR" for n in board["nodes"].values()))
    monkeypatch.setattr(RuntimeSupervisor, "install_job", staticmethod(original))
    writer = next(n for n in board["nodes"].values() if n["stage"] == "draft")
    with kernel.db.uow(write=False) as repo:
        task = repo.one("tasks", {"task_id": writer["task_id"]})
        ref = _selected(repo, wr["work_id"], boards._key(wr["branch_id"]))
    request = ReadingMcpUpgrade(wr["work_id"], wr["branch_id"], ref["sha256"], task["task_id"],
                               task["generation"], writer["output_ref"], "2" * 40)
    return gateway, owner, wr, calls, board, writer, request


def test_legacy_clear_upgrade_preserves_history_and_ledger_then_really_queries(kernel_env, monkeypatch):
    kernel = kernel_env["kernel"]
    gateway, owner, wr, calls, board, writer, request = legacy_clear(kernel, monkeypatch)
    with kernel.db.uow(write=False) as repo:
        tasks = repo.many("tasks", {"work_id": wr["work_id"]})
        old_calls = repo.many("call_attempts", {"task_id": [t["task_id"] for t in tasks]})
        budgets = repo.many("budgets", {"work_id": wr["work_id"]})
        old_reading = next(n for n in board["nodes"].values() if n["stage"] == "epistemic")
        accepted = repo.one("tasks", {"task_id": old_reading["task_id"]})
        assert accepted["state"] == "ACCEPTED"
        assert not _selected(repo, wr["work_id"], "MCP_EPISTEMIC:" + request.draft_ref["artifact_revision_id"])
    before = len(calls)
    preview = upgrade_reading_policy(kernel, gateway, owner, request)
    assert preview["status"] == "READING_MCP_UPGRADE_PREVIEW" and len(calls) == before
    applied = upgrade_reading_policy(kernel, gateway, owner, request, apply=True)
    assert upgrade_reading_policy(kernel, gateway, owner, request, apply=True) == applied
    with pytest.raises(DomainError) as second_migration:
        upgrade_reading_policy(kernel, gateway, owner, replace(request, source_revision="3" * 40), apply=True)
    assert second_migration.value.code == "IDEMPOTENCY_MISMATCH"
    assert len(calls) == before
    with kernel.db.uow(write=False) as repo:
        current = boards._load(kernel, repo, wr["work_id"], wr["branch_id"])
        assert current["nodes"][next(k for k, n in board["nodes"].items() if n is writer)] == writer
        assert current["budget_id"] == board["budget_id"]
        assert repo.many("call_attempts", {"task_id": [t["task_id"] for t in tasks]}) == old_calls
        assert repo.many("budgets", {"work_id": wr["work_id"]}) == budgets
        assert repo.one("tasks", {"task_id": accepted["task_id"]}) == accepted
        for identity in applied["nodes"]:
            assert current["nodes"][identity]["state"] == "PENDING"
            assert current["nodes"][identity]["reason"] == "READING_POLICY_UPGRADED"
            assert current["nodes"][identity]["history"][-1] == {k: v for k, v in board["nodes"][identity].items() if k != "history"}
        assert not _selected(repo, wr["work_id"], "MCP_EPISTEMIC:" + request.draft_ref["artifact_revision_id"])
    final, cursor = drive(kernel, gateway, owner, wr)
    assert cursor["frozen_chapters"] == 1
    final_writer = next(n for n in final["nodes"].values() if n["stage"] == "draft")
    assert final_writer == writer
    assert len([identity for identity, _, _ in calls if identity["role_id"] == "writer"]) == 1
    with kernel.db.uow(write=False) as repo:
        proof = _selected(repo, wr["work_id"], "MCP_EPISTEMIC:" + request.draft_ref["artifact_revision_id"])
        assert proof and proof != old_reading["output_ref"]
        record = kernel.artifact(repo, proof, wr["work_id"])["payload"]
        new_task = repo.one("tasks", {"task_id": record["reader_task"]})
        job = repo.one("supervisor_jobs", {"task_id": new_task["task_id"]})
        assert job["config"]["required_review_mcp"]
        assert new_task["principal_id"] != accepted["principal_id"] and new_task["grant_id"] != accepted["grant_id"]


def test_upgrade_requires_controller_exact_cas_and_legacy_clear(kernel_env, monkeypatch):
    kernel = kernel_env["kernel"]
    gateway, owner, wr, calls, board, _, request = legacy_clear(kernel, monkeypatch)
    impostor = kernel.issue_grant(owner, wr["work_id"], role_id="operator", capabilities=["ADMIN"],
                                  branch_ids=[wr["branch_id"]])["principal"]
    for actor, changed, expected in [
        (impostor, request, "VIEW_DENIED"),
        (Principal(owner.grant_id, impostor.principal_id), request, "VIEW_DENIED"),
        (owner, replace(request, expected_board_sha256="0" * 64), "STALE_GENERATION"),
        (owner, replace(request, expected_writer_generation=request.expected_writer_generation + 1), "STALE_GENERATION"),
        (owner, replace(request, source_revision="2" * 64), "INVALID_ARGUMENT"),
    ]:
        with pytest.raises(DomainError) as denied:
            upgrade_reading_policy(kernel, gateway, actor, changed, apply=True)
        assert denied.value.code == expected
    with kernel.db.uow(write=False) as repo:
        assert boards._load(kernel, repo, wr["work_id"], wr["branch_id"]) == board
    assert len(calls) == 2  # Writer and original independent epistemic reader only.


def test_upgrade_refuses_frozen_or_accepted_prose_and_non_native_reading_routes(kernel_env, monkeypatch):
    from novel_factory.application.planning import PlanningService

    kernel = kernel_env["kernel"]
    gateway, owner, wr, calls, board, _, request = legacy_clear(kernel, monkeypatch)
    writer_id = next(k for k, n in board["nodes"].items() if n["stage"] == "draft")
    scene_id = writer_id.removeprefix("draft:")
    with kernel.db.uow() as repo:
        PlanningService(kernel)._select(repo, wr, "P6_FROZEN_SCENE:" + scene_id, request.draft_ref)
    with pytest.raises(DomainError) as frozen:
        upgrade_reading_policy(kernel, gateway, owner, request, apply=True)
    assert frozen.value.code == "NARRATIVE_SELECTION_CONFLICT"
    with kernel.db.uow() as repo:
        repo.conn.execute(text(
            "DELETE FROM nf_selections WHERE work_id=:w AND selection_kind=:k"),
            {"w": wr["work_id"], "k": "P6_FROZEN_SCENE:" + scene_id})
        board["routes"]["review"] = "creative"
        selected = boards._save(kernel, repo, owner, board)
    request = replace(request, expected_board_sha256=selected["sha256"])
    with pytest.raises(DomainError) as route:
        upgrade_reading_policy(kernel, gateway, owner, request, apply=True)
    assert route.value.code == "MODEL_UNAVAILABLE"
    assert len(calls) == 2


@pytest.mark.parametrize("mode", ["SENT", "UNKNOWN", "CANCELLED_UNKNOWN", "LEASE"])
def test_inflight_original_reading_must_be_drained_before_upgrade(kernel_env, monkeypatch, mode):
    kernel = kernel_env["kernel"]
    gateway, owner, wr, calls, board, _, request = legacy_clear(kernel, monkeypatch)
    # Create the next native reading but block all actual sends. Durable state
    # fault injection models an abandoned original call in this isolated DB.
    node_id = next(k for k, n in board["nodes"].items() if n["stage"] == "review")
    with kernel.objects.lock(), kernel.db.uow() as repo:
        boards._enroll(kernel.in_transaction(repo), repo, gateway.with_kernel(kernel.in_transaction(repo)), owner, board, node_id)
        ref = boards._save(kernel, repo, owner, board)
    request = replace(request, expected_board_sha256=ref["sha256"])
    node = board["nodes"][node_id]
    # Admit and complete one wholly synthetic Host call, then alter its durable
    # status to each failure cut, retaining the immutable returned evidence.
    RuntimeSupervisor(kernel, gateway).admit_model(owner, wr["work_id"], wr["branch_id"])
    with kernel.db.uow() as repo:
        task = repo.one("tasks", {"task_id": node["task_id"]})
        call = repo.one("call_attempts", {"task_id": task["task_id"]})
        assert call
        updates = {"state": "CANCELLED" if mode == "CANCELLED_UNKNOWN" else "PAUSED",
                   "lease_owner": None, "lease_expiry": None, "current_result_id": None}
        if mode == "LEASE":
            updates.update(lease_owner="synthetic-active-worker", lease_expiry=repo.now() + timedelta(seconds=600))
        repo.change("tasks", {"task_id": task["task_id"]}, updates)
        repo.change("call_attempts", {"attempt_id": call["attempt_id"]}, {"state": "SENT" if mode == "SENT" else "UNKNOWN"})
        if mode in {"UNKNOWN", "CANCELLED_UNKNOWN"}:
            repo.change("host_turns", {"attempt_id": call["attempt_id"]}, {"phase": "TURN_STARTED"})
        old_call = repo.one("call_attempts", {"attempt_id": call["attempt_id"]})
        old_budget = repo.one("budgets", {"budget_id": call["budget_id"]})
    before = len(calls)
    if mode == "CANCELLED_UNKNOWN":
        applied = upgrade_reading_policy(kernel, gateway, owner, request, apply=True)
        assert applied["cancelled_unknown_attempts"][0]["attempt_id"] == call["attempt_id"]
    else:
        with pytest.raises(DomainError) as denied:
            upgrade_reading_policy(kernel, gateway, owner, request, apply=True)
        assert denied.value.code == "CALL_RECONCILIATION_REQUIRED"
    assert len(calls) == before
    with kernel.db.uow(write=False) as repo:
        assert repo.one("call_attempts", {"attempt_id": call["attempt_id"]}) == old_call
        assert repo.one("budgets", {"budget_id": call["budget_id"]}) == old_budget
