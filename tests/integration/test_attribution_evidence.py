"""PG17 attribution acceptance requires real task-bound MCP transport evidence.

The native model callback is synthetic and every real provider boundary is
forbidden by reviewed(); the MCP requests, audit rows and journal are real.
"""
from uuid import uuid4

import pytest
from test_harness_context import EnvelopeFixture
from test_narrative import arrange, draft_for
from test_release import setup_batch, write_scene
from test_review_mcp import reviewed

from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.candidate_delivery import CandidateDelivery
from novel_factory.application.narrative import NarrativeService
from novel_factory.application.planning import PlanningService
from novel_factory.application.production_pipeline import _artifact
from novel_factory.application.runtime_tasks import RuntimeTaskService
from novel_factory.domain.errors import DomainError
from novel_factory.domain.reconciliation import span_for
from novel_factory.fixtures import load_fixture, point, run_command

pytestmark = [pytest.mark.postgres, pytest.mark.integration]
TEXT = "合成稿件：他说，消息尚未核实。"


def extraction(kernel, owner, wr, draft_ref):
    gateway = AgentGateway(kernel, {"fixture": EnvelopeFixture()})
    with kernel.db.uow(write=False) as repo:
        source = kernel.artifact(repo, draft_ref, wr["work_id"], kind="NARRATIVE_DRAFT")["payload"]
        scene = kernel.artifact(repo, source["scene_ref"], wr["work_id"], kind="SCENE_PLAN")["payload"]
        subjects = sorted({check["subject_id"] for check in [*scene["entry_checks"], *scene["exit_checks"]]}
                          | {scene["scene_contract"]["pov_actor_id"]})
    job = RuntimeTaskService(kernel, gateway).prepare(owner, wr, role_id="evidence_maintainer", task_kind="assertion_extract",
        story_time=point(12), subject_ids=subjects, predicates=["possessed_by"], assignment="Synthetic attribution extraction",
        adapter_id="fixture", material_refs=[draft_ref])
    narrative = NarrativeService(kernel)
    narrative.bind_reader_task(owner, job.task["task_id"], job.task["generation"], [draft_ref], "bind:" + job.task["task_id"])
    delivery = narrative.read_text(job.principal, job.task["task_id"], job.task["generation"], draft_ref)
    claim = {"assertion_id": str(uuid4()), "classification": "ATTRIBUTED_STATEMENT", "span": span_for(delivery["text"]),
             "summary": "Only the attributed saying in this exact text; no claim that its content is true",
             "event_ref": None, "world_check": None, "knowledge_use": None, "proposed_command": None,
             "disclosure_id": None, "declared_delta_index": None}
    reference = narrative.submit_extraction(job.principal, job.task["task_id"], job.task["generation"], draft_ref,
        delivery["delivery_ref"], [claim], "extract:" + job.task["task_id"])
    return reference


def select_reading(kernel, gateway, result, owner, prepared, draft):
    wr = prepared.spec["world_ref"]
    with kernel.objects.lock(), kernel.db.uow() as repo:
        task = repo.one("tasks", {"task_id": prepared.task["task_id"]})
        evidence, _ = CandidateDelivery(kernel, gateway).audited_candidate(repo, task, production_accounting=True)
        payload = kernel.artifact(repo, draft["draft_ref"], wr["work_id"], kind="NARRATIVE_DRAFT")["payload"]
        reading = {"world_ref": wr, "draft_ref": draft["draft_ref"], "text_ref": payload["text_ref"],
                   "reader_task": task["task_id"], "generation": task["generation"], "attempt_id": result["attempt_id"],
                   "candidate_ref": evidence["output_ref"], "status": "CLEAR"}
        reference = _artifact(kernel, repo, wr["work_id"], owner, "EPISTEMIC_READING", reading)
        kernel.tasks.accept_in_transaction(repo.conn, task["task_id"], task["generation"])
        PlanningService(kernel)._select(repo, wr, "MCP_EPISTEMIC:" + draft["draft_ref"]["artifact_revision_id"], reference)
    return reference


def reconcile(kernel, owner, wr, draft_ref):
    extracted = extraction(kernel, owner, wr, draft_ref)
    return NarrativeService(kernel).reconcile(owner, draft_ref, extracted, wr, "reconcile:" + extracted["artifact_revision_id"])


def test_attribution_without_independent_mcp_reading_is_unresolved_and_cannot_be_accepted(kernel_env):
    kernel = kernel_env["kernel"]
    fixture = setup_batch(kernel, count=1)
    owner, wr = fixture["principal"], fixture["world_ref"]
    draft = write_scene(kernel, fixture, 0, TEXT)
    report = reconcile(kernel, owner, wr, draft["draft_ref"])
    assert report["status"] == "BLOCKED"
    with kernel.db.uow(write=False) as repo:
        payload = kernel.artifact(repo, report["reconciliation_ref"], wr["work_id"])["payload"]
    assert {item["code"] for item in payload["hard_blockers"]} == {"UNRESOLVED_KNOWLEDGE"}
    with pytest.raises(DomainError):
        NarrativeService(kernel).accept_narrative(owner, draft["draft_ref"], report["reconciliation_ref"], wr, "forbidden-accept")


@pytest.mark.parametrize("mode", ["valid", "no_query", "no_material"])
def test_exact_independent_native_review_needs_actual_query_and_original_material_receipts(kernel_env, monkeypatch, mode):
    kernel = kernel_env["kernel"]
    gateway, result, owner, prepared, _, _, _, draft = reviewed(kernel, monkeypatch,
        task_kind="epistemic", text=TEXT, mode=mode)
    wr = prepared.spec["world_ref"]
    select_reading(kernel, gateway, result, owner, prepared, draft)
    if mode != "valid":
        with pytest.raises(DomainError) as error:
            reconcile(kernel, owner, wr, draft["draft_ref"])
        assert error.value.code == "REVIEW_QUERY_REQUIRED"
        return
    with kernel.db.uow(write=False) as repo:
        original_head = kernel.head(repo, wr["work_id"], wr["branch_id"])
    report = reconcile(kernel, owner, wr, draft["draft_ref"])
    assert report["status"] == "CLEAR"
    accepted = NarrativeService(kernel).accept_narrative(owner, draft["draft_ref"], report["reconciliation_ref"], wr, "attribution-accept")
    with kernel.db.uow(write=False) as repo:
        receipt = kernel.artifact(repo, accepted, wr["work_id"], kind="NARRATIVE_ACCEPTANCE")["payload"]
        assert receipt["world_effects_applied"] == 0 and kernel.head(repo, wr["work_id"], wr["branch_id"]) == original_head


def test_another_drafts_reading_cannot_authorize_attribution(kernel_env, monkeypatch):
    kernel = kernel_env["kernel"]
    gateway, result, owner, prepared, _, _, _, draft = reviewed(kernel, monkeypatch, task_kind="epistemic", text=TEXT)
    wr = prepared.spec["world_ref"]
    reading_ref = select_reading(kernel, gateway, result, owner, prepared, draft)
    with kernel.db.uow(write=False) as repo:
        original = kernel.artifact(repo, draft["draft_ref"], wr["work_id"], kind="NARRATIVE_DRAFT")["payload"]
    replacement = NarrativeService(kernel).store_draft(draft["writer"], draft["task"]["task_id"], draft["task"]["generation"],
        original["scene_ref"], TEXT + "另一版。", "replacement-candidate", handoff_refs=original["handoff_refs"])
    with kernel.objects.lock(), kernel.db.uow() as repo:
        PlanningService(kernel)._select(repo, wr, "MCP_EPISTEMIC:" + replacement["artifact_revision_id"], reading_ref)
    with pytest.raises(DomainError) as error:
        reconcile(kernel, owner, wr, replacement)
    assert error.value.code == "INVALID_EVIDENCE"


def test_attribution_with_mcp_proof_cannot_cover_declared_important_world_changes(kernel_env, monkeypatch):
    kernel = kernel_env["kernel"]
    gateway, result, owner, prepared, _, _, _, draft = reviewed(kernel, monkeypatch,
        task_kind="epistemic", text=TEXT, declared_deltas=["Synthetic formal ownership change"])
    wr = prepared.spec["world_ref"]
    select_reading(kernel, gateway, result, owner, prepared, draft)
    report = reconcile(kernel, owner, wr, draft["draft_ref"])
    assert report["status"] == "BLOCKED"
    with kernel.db.uow(write=False) as repo:
        payload = kernel.artifact(repo, report["reconciliation_ref"], wr["work_id"])["payload"]
    assert any(item["code"] == "DECLARED_DELTA_COVERAGE_CONFLICT" for item in payload["hard_blockers"])


def test_attribution_with_mcp_proof_cannot_replace_required_formal_event_presentation(kernel_env, monkeypatch):
    kernel = kernel_env["kernel"]
    fixture = load_fixture(kernel, "white_bridge")
    ids = fixture["ids"]
    transfer = run_command(kernel, fixture["principal"], fixture["world_ref"], "RESOURCE_TRANSFER", {
        "from_account": ids["warehouse"], "to_account": ids["relief"], "resource_id": ids["grain"],
        "quantity": "10", "unit": "bag", "at": point(2)})
    fixture = arrange(kernel, {**fixture, "world_ref": transfer["world_ref"]})
    event = transfer["receipt"]["accepted_event_refs"][0]
    source = draft_for(kernel, fixture, TEXT, event_refs=[event])
    gateway, result, owner, prepared, _, _, _, draft = reviewed(kernel, monkeypatch,
        task_kind="epistemic", work=fixture, source_draft=source)
    wr = prepared.spec["world_ref"]
    select_reading(kernel, gateway, result, owner, prepared, draft)
    report = reconcile(kernel, owner, wr, draft["draft_ref"])
    assert report["status"] == "BLOCKED"
    with kernel.db.uow(write=False) as repo:
        payload = kernel.artifact(repo, report["reconciliation_ref"], wr["work_id"])["payload"]
    assert payload["event_refs"] == []
    assert any(item["code"] == "IMPORTANT_EFFECT_COVERAGE_CONFLICT" for item in payload["hard_blockers"])
