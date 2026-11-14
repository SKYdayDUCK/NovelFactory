"""Lost Flash output recovery never retries, settles or accepts the abandoned Attempt."""
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from test_p6_rolling_production import request
from test_release import setup_batch, write_scene

from agent_runtime import native_codex, providers
from novel_factory.application import production_pipeline as boards
from novel_factory.application import rolling_production as rolling
from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.lost_draft import LostDraftRequest, replace_lost_draft
from novel_factory.application.lost_review import LostReviewRequest, replace_lost_review
from novel_factory.application.supervisor import HostJournal, RuntimeSupervisor
from novel_factory.application.volume_successor import _selected
from novel_factory.domain.errors import DomainError
from novel_factory.infrastructure.model_gateway import ModelCallFailure
from novel_factory.infrastructure.runtime_providers import RuntimeModelAdapter

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def lost_draft(kernel, monkeypatch, *, kind="scene_draft", model="deepseek-flash"):
    fixture = setup_batch(kernel, count=1)
    owner, wr = fixture["principal"], fixture["world_ref"]
    forbidden = lambda *args, **kwargs: pytest.fail("Lost-output fixture reached a live transport")
    monkeypatch.setattr(native_codex, "generate", forbidden)
    monkeypatch.setattr(native_codex, "start_host", forbidden)
    monkeypatch.setattr(providers, "post", forbidden)
    monkeypatch.setattr(providers.Profile, "preflight", lambda _: None)
    adapter = RuntimeModelAdapter(providers.Profile("deepseek", model, "SYNTHETIC_NO_CREDENTIAL", 2048,
                                                    defer_invoice=True), HostJournal(kernel))
    calls = []
    def lost(packet):
        from agent_runtime._http_worker import _persist
        from novel_factory.infrastructure.wire_receipts import destination
        calls.append(packet.attempt_id)
        _persist({"request_hash": packet.request_sha256, "receipt_target": {"attempt_id": packet.attempt_id,
            "path": destination(kernel.objects, packet.attempt_id)}}, 200, b"", False, "synthetic-headers", {}, headers_only=True)
        raise ModelCallFailure("MODEL_TRANSPORT_UNKNOWN")
    monkeypatch.setattr(adapter, "generate", lost)
    gateway = AgentGateway(kernel, {"fixture": adapter}, material_reader=boards.production_material_reader(kernel))
    rolling.register_rolling(kernel, gateway, owner, **request(kernel, fixture, 1))
    if kind == "targeted_revision":
        prior = write_scene(kernel, fixture, 0, "合成先前正文。")
        feedback = kernel.put_artifact(owner, wr["work_id"], {"synthetic": "revision instruction"}, "TEXT")
        with kernel.db.uow() as repo:
            board = boards._load(kernel, repo, wr["work_id"], wr["branch_id"])
            node = next(node for node in board["nodes"].values() if node["stage"] == "draft")
            node.update(prior_draft_ref=prior["draft_ref"], revision_feedback={"report_ref": feedback, "findings": []})
            boards._save(kernel, repo, owner, board)
    for _ in range(8):
        RuntimeSupervisor(kernel, gateway).run_once(owner, wr["work_id"], wr["branch_id"])
        if calls:
            break
    assert len(calls) == 1
    with kernel.db.uow() as repo:
        call = repo.one("call_attempts", {"attempt_id": calls[0]})
        repo.change("call_attempts", {"attempt_id": calls[0]}, {"created_at": repo.now() - timedelta(minutes=11)})
        call = repo.one("call_attempts", {"attempt_id": calls[0]})
        task = repo.one("tasks", {"task_id": call["task_id"]})
        assert task["state"] == "PAUSED" and task["task_kind"] == kind
        ref = _selected(repo, wr["work_id"], boards._key(wr["branch_id"]))
        budget = repo.one("budgets", {"budget_id": call["budget_id"]})
    recovery = LostDraftRequest(wr["work_id"], wr["branch_id"], calls[0], call["generation"], ref["sha256"], True)
    return gateway, owner, recovery, task, call, budget, calls


@pytest.mark.parametrize("kind", ["scene_draft", "targeted_revision"])
def test_lost_flash_draft_replacement_preserves_frozen_input_and_fences_late_output(kernel_env, monkeypatch, kind):
    kernel = kernel_env["kernel"]
    gateway, owner, recovery, task, call, budget, calls = lost_draft(kernel, monkeypatch, kind=kind)
    preview = replace_lost_draft(kernel, gateway, owner, recovery)
    assert preview["status"] == "REPLACEMENT_PREVIEW" and preview["provider_calls_sent"] == 0
    with pytest.raises(DomainError) as acknowledgement:
        replace_lost_draft(kernel, gateway, owner, replace(recovery, acknowledge_unknown_cost=False), apply=True)
    assert acknowledgement.value.code == "INVALID_ARGUMENT"
    result = replace_lost_draft(kernel, gateway, owner, recovery, apply=True)
    assert replace_lost_draft(kernel, gateway, owner, recovery, apply=True) == result
    with kernel.db.uow(write=False) as repo:
        assert repo.one("call_attempts", {"attempt_id": call["attempt_id"]}) == call
        assert repo.one("budgets", {"budget_id": budget["budget_id"]}) == budget
        assert repo.one("tasks", {"task_id": task["task_id"]})["state"] == "CANCELLED"
        fresh = repo.one("tasks", {"task_id": result["new_task_id"]})
        assert fresh["state"] == "READY" and fresh["task_kind"] == kind
        assert fresh["principal_id"] != task["principal_id"] and fresh["grant_id"] != task["grant_id"]
        assert not repo.many("call_attempts", {"task_id": fresh["task_id"]})
        inputs = []
        for source in (task, fresh):
            job = repo.one("supervisor_jobs", {"task_id": source["task_id"]})
            inputs.append(kernel.artifact(repo, job["config"]["dispatch"]["input_ref"], recovery.work_id)["payload"])
        for field in ("role_id", "task_kind", "assignment", "material_refs", "output_contract", "runtime_sha256", "model_config_sha256"):
            assert inputs[0][field] == inputs[1][field]
        cursor = rolling._load(kernel, repo, recovery.work_id, recovery.branch_id)
        assert cursor["allowance"]["budget_id"] == result["new_budget_id"]
        assert len(cursor["lost_draft_replacements"]) == 1
    late = {"schema_version": "2.0", "task_id": task["task_id"], "generation": task["generation"],
        "attempt_id": task["attempt_id"], "result_id": str(uuid4()), "context_bundle_id": task["context_bundle_id"],
        "dependency_fingerprint": task["dependency_fingerprint"], "status": "SUCCEEDED", "artifacts": [],
        "safe_summary": "Synthetic late draft output"}
    with pytest.raises(DomainError):
        kernel.tasks.store_result(late, worker_id=task["lease_owner"])
    with kernel.db.uow(write=False) as repo:
        assert repo.one("task_results", {"result_id": late["result_id"]})["disposition"] == "LATE"
        assert repo.one("tasks", {"task_id": task["task_id"]})["current_result_id"] is None
    assert len(calls) == 1


@pytest.mark.parametrize("defect,code", [("board", "STALE_GENERATION"), ("generation", "CALL_RECONCILIATION_REQUIRED"),
    ("recent", "CALL_RECONCILIATION_REQUIRED"), ("lease", "CALL_RECONCILIATION_REQUIRED"),
    ("returned", "CALL_RECONCILIATION_REQUIRED"), ("wire", "CALL_RECONCILIATION_REQUIRED"),
    ("frozen", "NARRATIVE_SELECTION_CONFLICT"), ("used", "CAPACITY_LIMIT"),
    ("model", "MODEL_UNAVAILABLE"), ("rebound", "TASK_REBIND_REQUIRED")])
def test_lost_draft_replacement_rejects_active_returned_frozen_or_changed_work(kernel_env, monkeypatch, defect, code):
    kernel = kernel_env["kernel"]
    gateway, owner, recovery, task, call, budget, calls = lost_draft(kernel, monkeypatch,
        model="deepseek-v4-pro" if defect == "model" else "deepseek-flash")
    with kernel.objects.lock(), kernel.db.uow() as repo:
        if defect == "board":
            recovery = replace(recovery, expected_board_sha256="0" * 64)
        if defect == "generation":
            recovery = replace(recovery, expected_generation=recovery.expected_generation + 1)
        if defect == "recent":
            repo.change("call_attempts", {"attempt_id": call["attempt_id"]}, {"created_at": repo.now()})
        if defect == "lease":
            repo.conn.execute(text("UPDATE nf_tasks SET lease_expiry=clock_timestamp()+interval '1 minute' WHERE task_id=:t"), {"t": task["task_id"]})
        if defect == "returned":
            from novel_factory.application.quality_memory import _record
            _record(kernel, repo, recovery.work_id, owner.principal_id, call["attempt_id"], "response", {"text": "synthetic"}, "AGENT_RESPONSE")
        if defect == "wire":
            from agent_runtime._http_worker import _persist
            from novel_factory.infrastructure.wire_receipts import destination
            _persist({"request_hash": call["request_hash"], "receipt_target": {"attempt_id": call["attempt_id"],
                "path": destination(kernel.objects, call["attempt_id"])}}, 200, b"Synthetic durable return", True, "fixture", {})
        if defect in {"frozen", "used"}:
            board = boards._load(kernel, repo, recovery.work_id, recovery.branch_id)
            node = next(node for node in board["nodes"].values() if node.get("task_id") == task["task_id"])
            if defect == "used":
                node["lost_draft_replacements"] = [{"synthetic": "already used"}]
                board_ref = boards._save(kernel, repo, owner, board)
                recovery = replace(recovery, expected_board_sha256=board_ref["sha256"])
            else:
                scene = kernel.artifact(repo, node["scene_ref"], recovery.work_id)["payload"]
                repo.add("selections", {"work_id": recovery.work_id, "selection_kind": "NARRATIVE_SCENE:" + scene["scene_contract"]["scene_id"],
                    "artifact_ref": node["scene_ref"], "epoch": 1})
    if defect == "rebound":
        gateway.adapters["fixture"].profile = replace(gateway.adapters["fixture"].profile, max_output_tokens=2049)
    with pytest.raises(DomainError) as rejected:
        replace_lost_draft(kernel, gateway, owner, recovery, apply=True)
    assert rejected.value.code == code
    with kernel.db.uow(write=False) as repo:
        assert repo.one("tasks", {"task_id": task["task_id"]})["state"] == "PAUSED"
        assert repo.one("budgets", {"budget_id": budget["budget_id"]}) == budget
    assert len(calls) == 1


def test_review_recovery_stays_closed_to_writer_attempts(kernel_env, monkeypatch):
    kernel = kernel_env["kernel"]
    gateway, owner, recovery, task, _, _, calls = lost_draft(kernel, monkeypatch)
    with pytest.raises(DomainError) as denied:
        replace_lost_review(kernel, gateway, owner, LostReviewRequest(**vars(recovery)), apply=True)
    assert denied.value.code == "VIEW_DENIED"
    with kernel.db.uow(write=False) as repo:
        assert repo.one("tasks", {"task_id": task["task_id"]})["state"] == "PAUSED"
    assert len(calls) == 1


def test_lost_draft_replacement_requires_admin_and_only_one_new_dispatch(kernel_env, monkeypatch):
    from novel_factory.fixtures import point
    kernel = kernel_env["kernel"]
    gateway, owner, recovery, task, _, budget, calls = lost_draft(kernel, monkeypatch)
    reader = kernel.issue_grant(owner, recovery.work_id, role_id="writer", capabilities=["READ"],
        branch_ids=[recovery.branch_id], planes=["WORLD"], max_story_time=point(100))["principal"]
    with pytest.raises(DomainError) as unauthorized:
        replace_lost_draft(kernel, gateway, reader, recovery, apply=True)
    assert unauthorized.value.code == "VIEW_DENIED"
    result = replace_lost_draft(kernel, gateway, owner, recovery, apply=True)
    for _ in range(8):
        RuntimeSupervisor(kernel, gateway).run_once(owner, recovery.work_id, recovery.branch_id)
        if len(calls) == 2:
            break
    assert len(calls) == 2
    with kernel.db.uow() as repo:
        new_call = repo.one("call_attempts", {"task_id": result["new_task_id"]})
        repo.change("call_attempts", {"attempt_id": new_call["attempt_id"]}, {"created_at": repo.now() - timedelta(minutes=11)})
        ref = _selected(repo, recovery.work_id, boards._key(recovery.branch_id))
        fresh = repo.one("tasks", {"task_id": result["new_task_id"]})
    repeat = LostDraftRequest(recovery.work_id, recovery.branch_id, new_call["attempt_id"], fresh["generation"], ref["sha256"], True)
    with pytest.raises(DomainError) as exhausted:
        replace_lost_draft(kernel, gateway, owner, repeat, apply=True)
    assert exhausted.value.code == "CAPACITY_LIMIT"
    with kernel.db.uow(write=False) as repo:
        assert repo.one("budgets", {"budget_id": budget["budget_id"]}) == budget
        assert repo.one("tasks", {"task_id": task["task_id"]})["state"] == "CANCELLED"
        assert repo.one("tasks", {"task_id": fresh["task_id"]})["state"] == "PAUSED"
