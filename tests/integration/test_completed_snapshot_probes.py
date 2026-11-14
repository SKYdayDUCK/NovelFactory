"""Completed harness snapshots retain exact same-body acceptance evidence."""
import json
from decimal import Decimal

import pytest
import test_returned_edition_acceptance as returned
from sqlalchemy import text
from test_production_strategy import drive, synthetic_production
from test_release import write_scene

from agent_runtime.protocol import ModelResponse
from novel_factory.application import frozen_editing, rolling_production
from novel_factory.application.candidate_delivery import CandidateDelivery
from novel_factory.application.creative_context_store import load_snapshot
from novel_factory.application.supervisor import RuntimeSupervisor
from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import fixture_definition
from novel_factory.infrastructure.runtime_providers import RuntimeModelAdapter

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def _foreign_child(kernel, owner, wr, draft_ref):
    with kernel.db.uow(write=False) as repo:
        draft = kernel.artifact(repo, draft_ref, wr["work_id"], kind="NARRATIVE_DRAFT")["payload"]
        scene = kernel.artifact(repo, draft["scene_ref"], wr["work_id"], kind="SCENE_PLAN")["payload"]
        plan = kernel.artifact(repo, scene["plan_ref"], wr["work_id"], kind="PLAN_REVISION")["payload"]
    fixture = {"principal": owner, "world_ref": wr, "ids": fixture_definition("r1_sword")["ids"],
               "plan_ref": scene["plan_ref"], "plan": plan, "volume_id": plan["volume_id"],
               "batch_ref": scene["batch_ref"], "scene_refs": [draft["scene_ref"]], "scenes": [scene]}
    return write_scene(kernel, fixture, 0, "另一任务的未分配后继。", prior_draft_ref=draft_ref)


def test_accepted_native_readers_keep_proof_after_their_exact_source_is_accepted(kernel_env, monkeypatch):
    kernel = kernel_env["kernel"]
    gateway, owner, wr, _, _ = synthetic_production(kernel, monkeypatch, harness=True)
    board, cursor = drive(kernel, gateway, owner, wr)
    assert cursor["state"] == "FROZEN"
    readers = []
    with kernel.db.uow(write=False) as repo:
        for stage in ("epistemic", "facts", "review"):
            node = next(node for node in board["nodes"].values() if node["stage"] == stage)
            task = repo.one("tasks", {"task_id": node["task_id"]})
            assert task["state"] == "ACCEPTED"
            audit = gateway._read_audit(repo, repo.one("call_attempts", {
                "task_id": task["task_id"], "call_key": "supervised-candidate"})["attempt_id"])["payload"]
            assert audit["creative_context_ref"]
            CandidateDelivery(kernel, gateway).audited_candidate(repo, task, production_accounting=True)
            readers.append(task["task_id"])
            with pytest.raises(DomainError) as live:
                load_snapshot(kernel, repo, task, audit["input_ref"], required=True)
            assert live.value.code == "TASK_REBIND_REQUIRED"
    draft_ref = next(node["output_ref"] for node in board["nodes"].values() if node["stage"] == "draft")
    _foreign_child(kernel, owner, wr, draft_ref)
    for task_id in readers:
        with kernel.db.uow(write=False) as repo, pytest.raises(DomainError) as foreign:
            task = repo.one("tasks", {"task_id": task_id})
            CandidateDelivery(kernel, gateway).audited_candidate(repo, task, production_accounting=True)
        assert foreign.value.code == "TASK_REBIND_REQUIRED"


def test_returned_child_writer_keeps_proof_after_edition_scoped_acceptance(kernel_env, monkeypatch):
    import traceback
    from pathlib import Path

    from novel_factory.application import creative_context_store

    kernel = kernel_env["kernel"]
    original = returned.synthetic_production
    source_check = creative_context_store._check_sources
    source_errors = []

    def checked(*args, **kwargs):
        try:
            return source_check(*args, **kwargs)
        except Exception as error:
            source_errors.append({"code": getattr(error, "code", None), "message": str(error),
                "role": args[2]["role_id"], "kind": args[2]["task_kind"], "historical": bool(kwargs.get("completed_audit")),
                "stack": [(Path(frame.filename).name, frame.lineno, frame.name) for frame in traceback.extract_tb(error.__traceback__)]})
            raise

    monkeypatch.setattr(creative_context_store, "_check_sources", checked)

    def harness_production(bound, patch, **kwargs):
        return original(bound, patch, harness=True, **kwargs)

    monkeypatch.setattr(returned, "synthetic_production", harness_production)
    gateway, owner, wr, _, _ = returned.returned_fixture(kernel, monkeypatch)
    model = RuntimeModelAdapter.generate

    def creative_writer(adapter, packet):
        user = json.loads(packet.messages[1]["content"])
        if adapter.profile.kind != "deepseek" or "creative_materials" not in user:
            return model(adapter, packet)
        identity = json.loads(packet.messages[-1]["content"].split("\nIdentity: ", 1)[1].splitlines()[0])
        adapter.journal.authorize_send(packet.attempt_id)
        payload = {"text": user["creative_materials"]["prose"][-1] + "字", "important_deltas": [],
                   "event_refs": [], "evidence_refs": []}
        return ModelResponse(text=json.dumps({**identity, "status": "SUCCEEDED", "payload": payload,
            "unresolved_questions": []}), request_sha256=packet.request_sha256,
            provider_receipt="synthetic-creative-child-" + packet.attempt_id, input_tokens=30, output_tokens=40,
            actual_amount=Decimal(0), currency="USD", raw_response={"model": adapter.profile.model})

    monkeypatch.setattr(RuntimeModelAdapter, "generate", creative_writer)
    steps = []
    for _ in range(140):
        step = RuntimeSupervisor(kernel, gateway).run_once(owner, wr["work_id"], wr["branch_id"])
        if step["status"] != "IDLE":
            steps.append({key: step[key] for key in ("status", "reason", "task_id") if key in step})
        status = frozen_editing.status(kernel, owner, wr["work_id"], wr["branch_id"])
        if status["status"] in {"EDITION_ACCEPTED", "EDITION_REVISION_BLOCKED", "EDITING_BLOCKED"}:
            break
    if status["status"] != "EDITION_ACCEPTED":
        with kernel.db.uow(write=False) as repo:
            full = frozen_editing._state(kernel, repo, wr["work_id"], wr["branch_id"])
            task = repo.one("tasks", {"task_id": full["active"]}) if full.get("active") else None
            conflicts = [dict(row) for row in repo.conn.execute(text("""
                SELECT conflict_type,strategy,details->>'detection_method' AS detection_method,details->'scopes' AS scopes
                FROM nf_conflict_cases WHERE work_id=:work
            """), {"work": wr["work_id"]}).mappings()]
            diagnostic = {"status": status["status"], "reason": status.get("reason"),
                          "phase": full["edition"]["phase"], "task_state": task and task["state"],
                          "task_kind": task and task["task_kind"], "conflicts": conflicts,
                          "source_errors": source_errors[-4:], "steps": steps[-8:],
                          "calls": repo.many("call_attempts", {"task_id": task["task_id"]}) if task else []}
            diagnostic["calls"] = [{key: row[key] for key in ("state", "generation", "call_key")} for row in diagnostic["calls"]]
        pytest.fail(str(diagnostic))
    with kernel.db.uow(write=False) as repo:
        cursor = rolling_production._load(kernel, repo, wr["work_id"], wr["branch_id"])
        checkpoint = kernel.artifact(repo, cursor["last_checkpoint"], wr["work_id"])["payload"]
        edition = kernel.artifact(repo, checkpoint["edition_ref"], wr["work_id"])["payload"]
        child = edition["candidates"][0]
        task = repo.one("tasks", {"task_id": child["writer_task_id"]})
        assert task["state"] == "ACCEPTED"
        audit = gateway._read_audit(repo, repo.one("call_attempts", {
            "task_id": task["task_id"], "call_key": "supervised-candidate"})["attempt_id"])["payload"]
        assert audit["creative_context_ref"]
        CandidateDelivery(kernel, gateway).audited_candidate(repo, task, production_accounting=True)
        reader = repo.one("tasks", {"task_id": child["literary_task_id"]})
        reading, _ = CandidateDelivery(kernel, gateway).audited_candidate(repo, reader, production_accounting=True)
        snapshot = kernel.artifact(repo, reading["creative_context_ref"], wr["work_id"])["payload"]
        assigned = snapshot["source_versions"]["draft_ids"]
        assert child["parent_ref"]["artifact_revision_id"] in assigned
        assert child["draft_ref"]["artifact_revision_id"] in assigned
        ids = [task["task_id"], reader["task_id"]]
    # The input already contained parent and child; a third version created by
    # another real task was not assigned and remains a freshness conflict.
    _foreign_child(kernel, owner, wr, child["draft_ref"])
    for task_id in ids:
        with kernel.db.uow(write=False) as repo, pytest.raises(DomainError) as foreign:
            task = repo.one("tasks", {"task_id": task_id})
            CandidateDelivery(kernel, gateway).audited_candidate(repo, task, production_accounting=True)
        assert foreign.value.code == "TASK_REBIND_REQUIRED"
