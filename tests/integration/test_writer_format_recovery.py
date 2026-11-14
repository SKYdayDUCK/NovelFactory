"""Actual PG/wire/failed-result replay, with the outbound provider boundary replaced."""
import json
from copy import deepcopy
from uuid import uuid4

import pytest
from test_p6_postpaid_delivery import fake_transport
from test_p6_rolling_production import answer, connect, request
from test_release import accept_draft, setup_batch

from agent_runtime import providers
from agent_runtime._http_worker import _persist
from novel_factory.application import writer_format_normalization as normalization
from novel_factory.application.authorization import Principal
from novel_factory.application.candidate_delivery import CandidateDelivery
from novel_factory.application.narrative import NarrativeService
from novel_factory.application.production_pipeline import (
    _artifact,
    _key,
    _load,
    _save,
    advance_pipeline,
    pipeline_status,
)
from novel_factory.application.rolling_production import register_rolling
from novel_factory.application.supervisor import RuntimeSupervisor
from novel_factory.application.technical_repair_budget import repair_count
from novel_factory.application.volume_successor import _selected
from novel_factory.application.writer_format_recovery import (
    recover_writer_format,
    verify_derived_writer_result,
)
from novel_factory.domain.canonical import canonical_bytes, fingerprint
from novel_factory.domain.errors import DomainError

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def _failed_writer(kernel, monkeypatch):
    fixture = setup_batch(kernel, count=1)
    wr, owner = fixture["world_ref"], fixture["principal"]
    args = request(kernel, fixture, 1)
    args["profile"]["overrides"]["production_mode"] = "quality"
    adapter, calls = fake_transport(kernel, monkeypatch, "defer", answer)
    post = providers.post

    def malformed(**kwargs):
        body, receipt, window = post(**kwargs)
        value = json.loads(body)
        envelope = value["choices"][0]["message"]["content"]
        needle = '"work_id": "' + wr["work_id"] + '"'
        assert envelope.count(needle) == 1
        value["choices"][0]["message"]["content"] = envelope.replace(needle, needle[:-1])
        body = json.dumps(value).encode()
        _persist(kwargs, 200, body, True, receipt, window)
        return body, receipt, window

    monkeypatch.setattr(providers, "post", malformed)
    gateway = connect(kernel, adapter)
    register_rolling(kernel, gateway, owner, **args)
    # Reproduce the legacy Gateway that rejected these complete original bytes.
    # The current Gateway's future normalization is separately tested.
    with monkeypatch.context() as legacy:
        def reject(*_args, **_kwargs):
            raise DomainError("INVALID_MODEL_OUTPUT", "Legacy strict parser rejected the missing metadata quote")
        legacy.setattr(normalization, "derive_completed_writer", reject)
        for _ in range(8):
            RuntimeSupervisor(kernel, gateway).run_once(owner, wr["work_id"], wr["branch_id"])
            with kernel.db.uow(write=False) as repo:
                board = _load(kernel, repo, wr["work_id"], wr["branch_id"])
                node_id, node = next((key, node) for key, node in board["nodes"].items() if node["stage"] == "draft")
                task = repo.one("tasks", {"task_id": node.get("task_id")}) if node.get("task_id") else None
            if task and task["state"] == "FAILED":
                break
    assert task["state"] == "FAILED" and len(calls) == 1
    # Exhausted legacy lifetime history is retained; this recovery adds no
    # repair allowance and does not ask the provider for the text again.
    with kernel.objects.lock(), kernel.db.uow() as repo:
        board = _load(kernel, repo, wr["work_id"], wr["branch_id"])
        node = board["nodes"][node_id]
        node["schema_repairs"] = [{"family": "schema", "diagnostic": {"keyword": "jsonSyntax"}} for _ in range(6)]
        node.update(state="STOPPED", reason="TECHNICAL_REPAIR_BUDGET_EXHAUSTED")
        for other in board["nodes"].values():
            if other.get("parents") == [node_id]:
                other.update(state="STOPPED", reason="PREDECESSOR_STOPPED")
        board_ref = _save(kernel, repo, owner, board)
    kwargs = {"task_id": task["task_id"], "generation": task["generation"],
              "expected_board_sha256": board_ref["sha256"], "idempotency_key": "derive-original-writer",
              "source_revision": "a" * 40}
    return fixture, gateway, calls, node_id, kwargs


def _history(kernel, gateway, task_id):
    with kernel.db.uow(write=False) as repo:
        task = repo.one("tasks", {"task_id": task_id})
        call = repo.one("call_attempts", {"task_id": task_id, "generation": task["generation"], "call_key": "supervised-candidate"})
        audit = gateway._read_audit(repo, call["attempt_id"])
        return {"task": task, "call": call, "audit": audit,
                "response": kernel.artifact(repo, audit["payload"]["response_ref"], task["work_id"]),
                "result": repo.one("task_results", {"result_id": task["current_result_id"]}),
                "budget": repo.one("budgets", {"budget_id": call["budget_id"]}),
                "head": kernel.head(repo, task["work_id"], task["branch_id"]),
                "board_ref": _selected(repo, task["work_id"], _key(task["branch_id"]))}


def test_failed_writer_derivation_preserves_history_budget_and_normal_acceptance(kernel_env, monkeypatch):
    kernel = kernel_env["kernel"]
    fixture, gateway, calls, node_id, kwargs = _failed_writer(kernel, monkeypatch)
    wr, owner = fixture["world_ref"], fixture["principal"]
    before = _history(kernel, gateway, kwargs["task_id"])
    preview = recover_writer_format(kernel, gateway, owner, wr, **kwargs)
    assert preview["status"] == "WRITER_FORMAT_DERIVABLE" and preview["provider_calls_sent"] == 0
    assert _history(kernel, gateway, kwargs["task_id"]) == before
    adopted = recover_writer_format(kernel, gateway, owner, wr, **kwargs, apply=True)
    assert adopted["status"] == "WRITER_FORMAT_DERIVED" and len(calls) == 1
    assert recover_writer_format(kernel, gateway, owner, wr, **kwargs, apply=True) == adopted
    after = _history(kernel, gateway, kwargs["task_id"])
    assert all(before[key] == after[key] for key in ("call", "audit", "response", "budget", "head"))
    assert all(before["task"][key] == after["task"][key] for key in
               ("attempt_id", "generation", "deadline", "lease_owner", "lease_expiry", "grant_id", "context_bundle_id", "dependency_fingerprint"))
    with kernel.db.uow(write=False) as repo:
        assert repo.one("task_results", {"result_id": before["result"]["result_id"]}) == before["result"]
        task = repo.one("tasks", {"task_id": kwargs["task_id"]})
        evidence, candidate = CandidateDelivery(kernel, gateway).audited_candidate(repo, task, production_accounting=True)
        assert evidence["outcome"] == "REJECTED" and evidence["result_status"] == "FAILED"
        assert evidence["delivery_kind"] == "PROGRAM_DERIVED" and candidate["text"] == "北窗的雨声很轻。"
        board = _load(kernel, repo, wr["work_id"], wr["branch_id"])
        assert repair_count(board["nodes"][node_id]) == 6
        assert all(node.get("reason") != "PREDECESSOR_STOPPED" for node in board["nodes"].values())
    # The ordinary pipeline stores the exact candidate and closes the writer;
    # a different actual reader then supplies the normal extraction/acceptance.
    RuntimeSupervisor(kernel, gateway).run_once(owner, wr["work_id"], wr["branch_id"])
    board = pipeline_status(kernel, owner, wr["work_id"], wr["branch_id"])
    node = board["nodes"][node_id]
    assert node["state"] == "DRAFT_STORED" and len(calls) == 1
    accepted = accept_draft(kernel, fixture, {"draft_ref": node["output_ref"], "text": "北窗的雨声很轻。"})
    assert accepted["acceptance_ref"]
    with kernel.db.uow(write=False) as repo:
        task = repo.one("tasks", {"task_id": kwargs["task_id"]})
        assert task["state"] == "ACCEPTED"
        assert repo.one("task_results", {"result_id": before["result"]["result_id"]}) == before["result"]
        assert repo.one("call_attempts", {"attempt_id": before["call"]["attempt_id"]}) == before["call"]
        assert verify_derived_writer_result(kernel, repo, gateway, task)[1]["text"] == "北窗的雨声很轻。"


@pytest.mark.parametrize("fault", ["board", "generation", "authority", "epoch", "missing_wire", "foreign_scene"])
def test_failed_writer_derivation_rejects_changed_qualification(kernel_env, monkeypatch, fault):
    kernel = kernel_env["kernel"]
    fixture, gateway, calls, node_id, kwargs = _failed_writer(kernel, monkeypatch)
    wr, owner = fixture["world_ref"], fixture["principal"]
    before = _history(kernel, gateway, kwargs["task_id"])
    if fault == "board":
        kwargs["expected_board_sha256"] = "0" * 64
    elif fault == "generation":
        kwargs["generation"] += 1
    elif fault == "authority":
        owner = Principal(before["task"]["grant_id"], before["task"]["principal_id"])
    elif fault == "epoch":
        with kernel.db.uow() as repo:
            grant = repo.one("grants", {"grant_id": before["task"]["grant_id"]})
            repo.change("grants", {"grant_id": grant["grant_id"]}, {"epoch": grant["epoch"] + 1})
    elif fault == "missing_wire":
        monkeypatch.setattr(normalization, "read_receipt", lambda *_args: None)
    else:
        with kernel.db.uow() as repo:
            board = _load(kernel, repo, wr["work_id"], wr["branch_id"])
            scene_ref = board["nodes"][node_id]["scene_ref"]
            scene = kernel.artifact(repo, scene_ref, wr["work_id"])["payload"]
            foreign = _artifact(kernel, repo, wr["work_id"], owner, "SCENE_PLAN", {**scene, "literary_purpose": "其他固定写作卡"})
            repo.change("selections", {"work_id": wr["work_id"], "selection_kind": "SCENE:" + scene["scene_contract"]["scene_id"]}, {"artifact_ref": foreign})
    with pytest.raises(DomainError):
        recover_writer_format(kernel, gateway, owner, wr, **kwargs, apply=True)
    after = _history(kernel, gateway, kwargs["task_id"])
    assert after["task"] == before["task"] and len(calls) == 1
    assert all(before[key] == after[key] for key in ("call", "audit", "response", "result", "budget", "head", "board_ref"))


def test_forged_derived_result_without_administrative_receipt_is_rejected(kernel_env, monkeypatch):
    kernel = kernel_env["kernel"]
    fixture, gateway, calls, _node_id, kwargs = _failed_writer(kernel, monkeypatch)
    wr, owner = fixture["world_ref"], fixture["principal"]
    adopted = recover_writer_format(kernel, gateway, owner, wr, **kwargs, apply=True)
    with kernel.objects.lock(), kernel.db.uow() as repo:
        proof = deepcopy(kernel.artifact(repo, adopted["derivation_ref"], wr["work_id"])["payload"])
        proof["idempotency_key"] = "forged-no-real-administration"
        proof["derived_result_id"] = str(uuid4())
        forged = _artifact(kernel, repo, wr["work_id"], owner, "WRITER_FORMAT_DERIVATION", proof)
        old = repo.one("task_results", {"result_id": adopted["derived_result_id"]})
        result = {**old["payload"], "result_id": proof["derived_result_id"], "artifacts": [forged, *old["payload"]["artifacts"][1:]]}
        result_ref = kernel.objects.put(canonical_bytes(result))
        kernel.tasks._register_object(repo.conn, result_ref, "task_result", result["result_id"])
        repo.add("task_results", {"result_id": result["result_id"], "task_id": kwargs["task_id"], "generation": kwargs["generation"],
                                 "attempt_id": old["attempt_id"], "payload_hash": fingerprint(result), "payload": result, "disposition": "CURRENT"})
        repo.change("tasks", {"task_id": kwargs["task_id"]}, {"current_result_id": result["result_id"]})
        task = repo.one("tasks", {"task_id": kwargs["task_id"]})
        # Keep the deterministic result marker aligned so the missing real
        # administrative receipt, rather than a basic UUID mismatch, is tested.
        with pytest.raises(DomainError) as denied:
            verify_derived_writer_result(kernel, repo, gateway, task)
        assert denied.value.code == "INVALID_EVIDENCE"
    assert len(calls) == 1


@pytest.mark.parametrize("change", ["revoke", "epoch"])
def test_derived_writer_still_requires_live_authority_to_store(kernel_env, monkeypatch, change):
    kernel = kernel_env["kernel"]
    fixture, gateway, calls, node_id, kwargs = _failed_writer(kernel, monkeypatch)
    wr, owner = fixture["world_ref"], fixture["principal"]
    before = _history(kernel, gateway, kwargs["task_id"])
    adopted = recover_writer_format(kernel, gateway, owner, wr, **kwargs, apply=True)
    actor = Principal(before["task"]["grant_id"], before["task"]["principal_id"])
    with kernel.db.uow() as repo:
        grant = repo.one("grants", {"grant_id": actor.grant_id})
        repo.change("grants", {"grant_id": actor.grant_id},
                    {"active": False} if change == "revoke" else {"epoch": grant["epoch"] + 1})
        node = _load(kernel, repo, wr["work_id"], wr["branch_id"])["nodes"][node_id]
        candidate = kernel.artifact(repo, adopted["candidate_ref"], wr["work_id"])["payload"]
    with pytest.raises(DomainError) as refused:
        NarrativeService(kernel).store_draft(actor, kwargs["task_id"], kwargs["generation"], node["scene_ref"],
            candidate["text"], "revoked-derived-draft", agent_output_ref=adopted["candidate_ref"],
            declared_important_deltas=candidate["important_deltas"], handoff_refs=node["handoff_refs"])
    assert refused.value.code == "VIEW_DENIED"
    advance_pipeline(kernel, gateway, owner, wr["work_id"], wr["branch_id"])
    with kernel.db.uow(write=False) as repo:
        node = _load(kernel, repo, wr["work_id"], wr["branch_id"])["nodes"][node_id]
        assert node["state"] == "STOPPED" and node["reason"] == "VIEW_DENIED" and not node.get("output_ref")
        assert not repo.many("artifacts", {"work_id": wr["work_id"], "kind": "NARRATIVE_DRAFT"})
        assert repo.one("task_results", {"result_id": before["result"]["result_id"]}) == before["result"]
        assert repo.one("call_attempts", {"attempt_id": before["call"]["attempt_id"]}) == before["call"]
        assert repo.one("budgets", {"budget_id": before["call"]["budget_id"]}) == before["budget"]
        assert repair_count(node) == 6
    assert len(calls) == 1
