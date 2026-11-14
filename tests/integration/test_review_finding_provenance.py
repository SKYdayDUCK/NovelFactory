"""A real task-bound MCP read exposes only eligible frozen fact-version metadata."""
import asyncio
import json
from copy import deepcopy
from uuid import uuid4

import pytest
import test_production_strategy as strategy_fixture
from test_production_strategy import drive, synthetic_production
from test_review_mcp import reviewed

from novel_factory.application import production_pipeline as boards
from novel_factory.application.narrative import NarrativeService
from novel_factory.application.queries import WorldQueryService
from novel_factory.application.review_mcp import (
    ReviewMcpBroker,
    _provenance_metadata,
    require_review_queries,
)
from novel_factory.application.runtime_tasks import RuntimeTaskService
from novel_factory.application.supervisor import RuntimeSupervisor
from novel_factory.domain.canonical import fingerprint
from novel_factory.domain.errors import DomainError
from novel_factory.domain.reconciliation import span_for
from novel_factory.fixtures import point

pytestmark = [pytest.mark.postgres, pytest.mark.integration]
SUMMARY = "Synthetic exact historical finding; its version must be independently checked"


def fact_report(kernel, gateway, owner, wr, draft_ref):
    """Use actual read/extraction/reconciliation services; no provider is invoked."""
    with kernel.db.uow(write=False) as repo:
        draft = kernel.artifact(repo, draft_ref, wr["work_id"], kind="NARRATIVE_DRAFT")["payload"]
        scene = kernel.artifact(repo, draft["scene_ref"], wr["work_id"], kind="SCENE_PLAN")["payload"]
    prepared = RuntimeTaskService(kernel, gateway).prepare(owner, wr,
        role_id="evidence_maintainer", task_kind="assertion_extract", story_time=scene["scene_contract"]["story_interval"]["end"],
        subject_ids=[scene["scene_contract"]["pov_actor_id"], scene["entry_checks"][0]["subject_id"]],
        predicates=[scene["entry_checks"][0]["predicate"]], assignment="Synthetic frozen fact report", adapter_id="review", material_refs=[draft_ref])
    service = NarrativeService(kernel)
    key = "provenance-fixture:" + prepared.task["task_id"]
    service.bind_reader_task(owner, prepared.task["task_id"], prepared.task["generation"], [draft_ref], key)
    delivered = service.read_text(prepared.principal, prepared.task["task_id"], prepared.task["generation"], draft_ref)
    assertion = {"assertion_id": str(uuid4()), "span": span_for(delivered["text"]), "classification": "UNCERTAIN",
        "summary": SUMMARY, "event_ref": None, "world_check": None, "knowledge_use": None,
        "proposed_command": None, "disclosure_id": None, "declared_delta_index": None}
    extracted = service.submit_extraction(prepared.principal, prepared.task["task_id"], prepared.task["generation"],
        draft_ref, delivered["delivery_ref"], [assertion], key)
    report = service.reconcile(owner, draft_ref, extracted, wr, key)
    with kernel.objects.lock(), kernel.db.uow() as repo:
        ref = boards._artifact(kernel, repo, wr["work_id"], owner, "ROLLING_FACT_REPORT", {
            **report, "extraction_ref": extracted, "text_delivery_ref": delivered["delivery_ref"]})
    return {"task_id": prepared.task["task_id"], "generation": prepared.task["generation"],
            "output_ref": ref, "state": "STOPPED", "reason": "NARRATIVE_ALIGNMENT_REQUIRED"}


def prepared_case(kernel, monkeypatch, *, duplicate_current=False):
    gateway, owner, wr, calls, _ = synthetic_production(kernel, monkeypatch,
        lengths=[1, 5, 3, 2, 7], diagnosis="needs_input")
    board, _ = drive(kernel, gateway, owner, wr, until=lambda board: any(
        node["stage"] == "reassessment" and node["state"] == "PENDING" for node in board["nodes"].values()))
    diagnosis_id, diagnosis = next((key, node) for key, node in board["nodes"].items() if node["stage"] == "reassessment")
    target = diagnosis["reassessment_request"]["target"]
    writer = board["nodes"][target]
    old_draft = writer["history"][0]["output_ref"]
    old = fact_report(kernel, gateway, owner, wr, old_draft)
    current = fact_report(kernel, gateway, owner, wr, writer["output_ref"]) if duplicate_current else None
    with kernel.objects.lock(), kernel.db.uow() as repo:
        board = boards._load(kernel, repo, wr["work_id"], wr["branch_id"])
        diagnosis = board["nodes"][diagnosis_id]
        diagnosis["reassessment_request"]["findings"].append({"reason": "Synthetic prior fact finding", "passage": SUMMARY})
        fact_node = board["nodes"]["facts:" + target.removeprefix("draft:")]
        fact_node.setdefault("history", []).append(old)
        if current:
            fact_node["history"].append(current)
        boards._save(kernel, repo, owner, board)
    board, _ = drive(kernel, gateway, owner, wr, until=lambda board: any(
        node["stage"] == "internal_verification" and node["state"] == "PENDING" for node in board["nodes"].values()))
    identity, node = next((key, node) for key, node in board["nodes"].items() if node["stage"] == "internal_verification")
    with kernel.objects.lock(), kernel.db.uow() as repo:
        boards._enroll(kernel.in_transaction(repo), repo, gateway.with_kernel(kernel.in_transaction(repo)), owner, board, identity)
        boards._save(kernel, repo, owner, board)
    return gateway, owner, wr, board, node, old, current, calls


@pytest.mark.parametrize("duplicate_current", [False, True])
def test_actual_mcp_provenance_lists_all_bound_versions_without_copying_old_text(kernel_env, monkeypatch, duplicate_current):
    kernel = kernel_env["kernel"]
    gateway, owner, wr, _board, node, old, current, _calls = prepared_case(kernel, monkeypatch, duplicate_current=duplicate_current)
    actual_read = strategy_fixture.run_review_mcp
    def reading_with_denied_old_report(kernel, packet, thread, turn, **query_options):
        refs = actual_read(kernel, packet, thread, turn, **query_options)
        async def denied():
            async with ReviewMcpBroker.from_attempt(kernel, packet) as broker:
                broker.bind(thread, turn)
                result = await broker.call({"threadId": thread, "turnId": turn, "callId": "unassigned-old-report",
                    "tool": "nf_review_material", "arguments": {"artifact_ref": old["output_ref"]}})
                assert not result["success"]
                return list(broker.receipt_refs)
        return [*refs, *asyncio.run(denied())]
    monkeypatch.setattr(strategy_fixture, "run_review_mcp", reading_with_denied_old_report)
    result = RuntimeSupervisor(kernel, gateway).admit_model(owner, wr["work_id"], wr["branch_id"])
    assert result["status"] == "RESULT_STORED", result
    with kernel.db.uow(write=False) as repo:
        task = repo.one("tasks", {"task_id": node["task_id"]})
        call = repo.one("call_attempts", {"task_id": task["task_id"]})
        evidence = gateway._read_audit(repo, call["attempt_id"])["payload"]
        receipts = require_review_queries(kernel, repo, task, evidence)
        material = next(receipt for receipt in receipts if receipt["tool"] == "nf_review_material"
            and receipt["arguments"]["artifact_ref"] == node["internal_case"]["report_ref"])
        value = material["result"]["structuredContent"]
        provenance = value["finding_provenance"]
        assert provenance["case_hash"] == node["internal_case"]["case_hash"]
        assert provenance["current_draft_ref"] == node["internal_case"]["source_draft_ref"]
        assert provenance["scene_story_interval"] == {"start": point(1), "end": point(1, 1), "bounds": "[)"}
        current_body = next(receipt["result"]["structuredContent"] for receipt in receipts
            if receipt["tool"] == "nf_review_material"
            and receipt["arguments"]["artifact_ref"] == node["internal_case"]["source_draft_ref"])
        assert current_body["scene_story_interval"] == provenance["scene_story_interval"]
        assert len(provenance["candidates"]) == (2 if duplicate_current else 1)
        assert {candidate["report_ref"]["artifact_revision_id"] for candidate in provenance["candidates"]} == {
            row["output_ref"]["artifact_revision_id"] for row in [old, *([current] if current else [])]}
        assert sum(candidate["matches_current"] for candidate in provenance["candidates"]) == int(duplicate_current)
        assert all(candidate["matched_finding_indices"] for candidate in provenance["candidates"])
        assert '"text":' not in json.dumps(provenance) and SUMMARY not in json.dumps(provenance)
        assert "STALE_REPORT" not in json.dumps(provenance)
        assert "ADMIN" not in repo.one("grants", {"grant_id": task["grant_id"]})["capabilities"]
        assert any(receipt["outcome"] == "ERROR" and receipt["arguments"].get("artifact_ref") == old["output_ref"]
                   for receipt in receipts)
        original_artifact = kernel.artifact
        def altered_result(repo, ref, work, **kwargs):
            row = original_artifact(repo, ref, work, **kwargs)
            if ref == material["receipt_ref"]:
                row = deepcopy(row)
                row["payload"]["result"]["structuredContent"]["finding_provenance"]["current_draft_ref"] = old["output_ref"]
                row["payload"]["result_sha256"] = fingerprint(row["payload"]["result"])
            return row
        monkeypatch.setattr(kernel, "artifact", altered_result)
        with pytest.raises(DomainError) as forged:
            require_review_queries(kernel, repo, task, evidence)
        assert forged.value.code == "REVIEW_QUERY_REQUIRED"


@pytest.mark.parametrize("poison", ["foreign_work", "wrong_writer_generation"])
def test_history_from_another_work_or_writer_version_is_rejected(kernel_env, monkeypatch, poison):
    kernel = kernel_env["kernel"]
    _gateway, owner, wr, board, node, _old, _current, _calls = prepared_case(kernel, monkeypatch)
    with kernel.objects.lock(), kernel.db.uow() as repo:
        target = node["internal_case"]["target"]
        if poison == "foreign_work":
            from novel_factory.fixtures import load_fixture
            other = load_fixture(kernel.in_transaction(repo))
            foreign = boards._artifact(kernel.in_transaction(repo), repo, other["world_ref"]["work_id"],
                                       other["principal"], "ROLLING_FACT_REPORT", {"synthetic": "foreign"})
            board["nodes"]["facts:" + target.removeprefix("draft:")]["history"][0]["output_ref"] = foreign
        else:
            board["nodes"][target]["history"][0]["generation"] += 1
        boards._save(kernel, repo, owner, board)
        task = repo.one("tasks", {"task_id": node["task_id"]})
        job = repo.one("supervisor_jobs", {"task_id": task["task_id"]})
        scope = {"input_ref": job["config"]["dispatch"]["input_ref"], "world_ref": wr}
        with pytest.raises(DomainError) as rejected:
            _provenance_metadata(kernel, repo, task, scope)
        assert rejected.value.code in {"VIEW_DENIED", "INVALID_EVIDENCE"}


@pytest.mark.parametrize("kind,code,accepted", [("verification", "ENTITY_NOT_FOUND", True),
    ("verification", "UNKNOWN_PREDICATE", True), ("verification", "VIEW_DENIED", False),
    ("literary", "ENTITY_NOT_FOUND", False)])
def test_only_verification_can_use_typed_registration_errors_from_actual_mcp(kernel_env, monkeypatch, kind, code, accepted):
    # The domain currently returns successful UNRESOLVED for missing state.
    # This synthetic query backend tests protocol-level typed ERROR outcomes;
    # it does not relabel a real UNKNOWN/PENDING answer as missing registration.
    def query_backend(self, principal, request):
        return {"all_items_succeeded": False, "items": [
            {"item_id": item["item_id"], "status": "ERROR",
             "error": DomainError(code, "Synthetic typed registration outcome").as_dict(str(uuid4()))}
            for item in request["items"]]}
    monkeypatch.setattr(WorldQueryService, "batch", query_backend)
    kernel = kernel_env["kernel"]
    gateway, result, _owner, prepared, _refs, _outputs, _calls, _draft = reviewed(kernel, monkeypatch, task_kind=kind)
    with kernel.db.uow(write=False) as repo:
        task = repo.one("tasks", {"task_id": prepared.task["task_id"]})
        evidence = gateway._read_audit(repo, result["attempt_id"])["payload"]
        if accepted:
            receipts = require_review_queries(kernel, repo, task, evidence)
            query = next(receipt for receipt in receipts if receipt["tool"] == "nf_query_batch")
            assert query["outcome"] == "OK" and query["result"]["structuredContent"]["all_items_succeeded"] is False
        else:
            with pytest.raises(DomainError) as denied:
                require_review_queries(kernel, repo, task, evidence)
            assert denied.value.code == "REVIEW_QUERY_REQUIRED"
