"""Native protocol fixtures, real PG/MCP and final book gates; every live send is forbidden."""
import json
from decimal import Decimal

import pytest
from native_review_helpers import run_review_mcp
from test_p6_rolling_production import answer
from test_production_strategy import synthetic_production

from agent_runtime.protocol import ModelResponse
from novel_factory.application import (
    book_review,
    final_book_automation,
    frozen_editing,
    model_routing,
    production_pipeline,
    rolling_production,
)
from novel_factory.application.rolling_production import rolling_status
from novel_factory.application.supervisor import RuntimeSupervisor
from novel_factory.domain.book_review import RUBRIC
from novel_factory.infrastructure.runtime_providers import RuntimeModelAdapter

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def _editorial(phase, text):
    if phase in {"SCENE", "CHAPTER", "VOLUME"}:
        return {"summary": "Synthetic independent reading.", "issues": [], "counterinterpretations": [], "evidence_refs": []}
    if phase == "APPROVAL":
        return {"decision": "APPROVED", "reason": "Synthetic frozen edition checked.",
                "deferred_questions": [], "evidence_refs": []}
    return {"decision": "APPROVED", "summary": "Synthetic editorial disposition.",
        "scene_functions": [{"scene": 1, "function": "Synthetic scene.", "quote": text}] if phase == "STRUCTURE" else [],
        "considered_options": [{"summary": "Retain", "disposition": "SELECTED", "reason": "Synthetic choice.",
                                "benefits": ["Continuity"], "costs": ["No variation"]},
                               {"summary": "Reorder", "disposition": "REJECTED", "reason": "One scene.",
                                "benefits": ["Variation"], "costs": ["Continuity"]}] if phase == "STRUCTURE" else [],
        "issue_decisions": []}


@pytest.mark.parametrize("missing_first", [False, True, "book_rework", "format_error", "policy"])
def test_native_editing_blind_reading_restart_and_final_freeze(kernel_env, monkeypatch, missing_first):
    k = kernel_env["kernel"]
    gateway, owner, wr, _, _ = synthetic_production(k, monkeypatch, lengths=[7], target=(6, 10))
    work, branch = wr["work_id"], wr["branch_id"]
    if missing_first == "policy":
        model_routing.activate(k, owner, wr, model_routing.idea_policy(wr, {"creative": "creative", "review": "review"}),
                               gateway.adapters, expected_revision=0)
        with k.db.uow() as repo:
            cursor = rolling_production._load(k, repo, work, branch)
            cursor["allowance"]["routes"] = dict.fromkeys(("draft", "review", "plan"), "@policy")
            rolling_production._save(k, repo, owner, cursor)
            board = production_pipeline._load(k, repo, work, branch)
            if board:
                board["routes"] = dict(cursor["allowance"]["routes"])
                production_pipeline._save(k, repo, owner, board)
    for _ in range(60):
        RuntimeSupervisor(k, gateway).run_once(owner, work, branch)
        if rolling_status(k, owner, work, branch)["state"] == "FROZEN":
            break
    assert rolling_status(k, owner, work, branch)["state"] == "FROZEN"
    sent, native_sent, blind_seen, missing, scored, malformed = [], [], [], [], [], []

    def generate(adapter, request):
        identity = json.loads(request.messages[-1]["content"].split("\nIdentity: ", 1)[1].splitlines()[0])
        tid = identity["task_id"]
        with k.db.uow(write=False) as repo:
            job = repo.one("supervisor_jobs", {"task_id": tid})["config"]
        native = adapter.profile.kind == "codex-cli"
        assert job.get("required_review_mcp") if native else adapter.profile.model == "deepseek-flash"
        adapter.journal.authorize_send(request.attempt_id)
        thread, turn = "final-thread-" + request.attempt_id, "final-turn-" + request.attempt_id
        if native:
            adapter.journal.record(request.attempt_id, thread, None, "THREAD_STARTED")
            adapter.journal.record(request.attempt_id, thread, turn, "TURN_STARTED")
        if missing_first is True and not missing:
            refs = []
            missing.append(tid)
        else:
            refs = run_review_mcp(k, request, thread, turn) if native else []
        if native:
            adapter.journal.record(request.attempt_id, thread, turn, "COMPLETED")
            native_sent.append(request.attempt_id)
        user = json.loads(request.messages[1]["content"])
        if job.get("review_mcp_blind"):
            assert set(user) == {"packet_ref", "instruction"} and "no tools" not in request.messages[0]["content"].lower()
            with k.db.uow(write=False) as repo:
                receipts = [k.artifact(repo, ref, work)["payload"] for ref in refs]
                packet = next(r["result"]["structuredContent"] for r in receipts if r["tool"] == "nf_review_material")
            assert all(r["tool"] != "nf_query_batch" for r in receipts)
            context = next(r["result"]["structuredContent"] for r in receipts if r["tool"] == "nf_context_get")
            assert context["blind_review"] and not (set(context) & {"facts", "beliefs", "reviews", "scores"})
            blind_seen.append(tid)
            if "passages" in packet:
                summary = [{"scene": p["scene"], "start": p["start"], "quote": p["text"][:1],
                            "observation": "Synthetic reading note."} for p in packet["passages"]]
            else:
                citation = {key: packet["observations"][0][key] for key in ("scene", "start", "quote")}
                low = missing_first == "book_rework" and len(scored) < 2
                scored.append(tid)
                summary = {"scores": {key: {**citation, "score": maximum // 2 if low else maximum,
                                           "reason": "Synthetic assessment.", "improvement": "Synthetic precise revision."}
                                      for key, maximum in RUBRIC.items()}, "vetoes": []}
            payload = {"summary": json.dumps(summary), "issues": [], "counterinterpretations": [], "evidence_refs": []}
        elif job.get("returned_edition"):
            if not native:
                before = next(json.loads(row["content"])["text"] for row in user["authorized_materials"]
                              if "text" in json.loads(row["content"]))
                payload = {"text": before + "字", "important_deltas": [], "event_refs": [], "evidence_refs": []}
            else:
                with k.db.uow(write=False) as repo:
                    reads = [k.artifact(repo, ref, work)["payload"] for ref in refs]
                delivered = {json.dumps(r["arguments"]["artifact_ref"], sort_keys=True): r["result"]["structuredContent"]
                             for r in reads if r["tool"] == "nf_review_material"}
                user["authorized_materials"] = [{"artifact_ref": item["artifact_ref"],
                    "content": json.dumps(delivered[json.dumps(item["artifact_ref"], sort_keys=True)])}
                    for item in user["authorized_materials"]]
                user["context"] = identity
                payload = answer(user)
        else:
            if refs:
                with k.db.uow(write=False) as repo:
                    readings = [k.artifact(repo, ref, work)["payload"] for ref in refs]
                material = next(r["result"]["structuredContent"] for r in readings if r["tool"] == "nf_review_material")
            else:
                # This deliberately deficient return is saved and rejected; no private file/model fallback.
                material = {"text": "Synthetic missing evidence."}
            phase = json.loads(user["assignment"])["phase"]
            payload = ({"decision": "RETURNED", "reason": "Synthetic bounded correction of the quoted blind-review finding.",
                        "deferred_questions": [], "evidence_refs": []} if phase == "BOOK_REWORK"
                       else _editorial(phase, material["text"]))
        sent.append(request.attempt_id)
        rendered = json.dumps({**identity, "status": "SUCCEEDED", "payload": payload, "unresolved_questions": []})
        if missing_first == "format_error" and not malformed:
            rendered = "{"
            malformed.append(tid)
        return ModelResponse(text=rendered,
            provider_receipt=turn, request_sha256=request.request_sha256, input_tokens=25, output_tokens=50,
            cached_input_tokens=0, actual_amount=None if native else Decimal(0), currency=None if native else "USD",
            raw_response={"model": adapter.profile.model, **({"auth_mode": "host-login", "billing": "SUBSCRIPTION_AMOUNT_UNKNOWN",
                          "codex_thread_id": thread, "codex_turn_id": turn, "review_mcp_receipt_refs": refs} if native else {})})

    monkeypatch.setattr(RuntimeModelAdapter, "generate", generate)
    frozen_editing.start(k, owner, work, branch)
    for _ in range(100):
        # Fresh supervisor each tick reproduces Host/process reconstruction.
        RuntimeSupervisor(k, gateway).run_once(owner, work, branch)
        edit = frozen_editing.status(k, owner, work, branch)
        assert edit["status"] != "EDITING_BLOCKED", edit
        if edit["status"] == "EDITORIAL_APPROVED":
            break
    assert edit["status"] == "EDITORIAL_APPROVED", edit
    # A tiny fixture proves routing/identity, not the production prose length target.
    monkeypatch.setattr("novel_factory.application.production_extent.measure_submission", lambda *args: {"source": "TEST_FIXTURE"})
    started = final_book_automation.advance(k, gateway, owner, work, branch)
    assert started["status"] == "FINAL_BOOK_REVIEW_ENROLLED"
    for _ in range(70):
        RuntimeSupervisor(k, gateway).run_once(owner, work, branch)
        state = book_review.review_status(k, owner, work, branch)
        assert state["state"] != "BLOCKED", state
        if state["state"] == "ASSESSED":
            break
    assert state["state"] == "ASSESSED", state
    assert len(blind_seen) == len(set(blind_seen)) == 4
    if missing_first == "book_rework":
        assert state["result"]["status"] == "P6_QUALITY_NOT_MET"
        assert final_book_automation.advance(k, gateway, owner, work, branch)["status"] == "BOOK_REWORK_ENROLLED"
        for _ in range(150):
            RuntimeSupervisor(k, gateway).run_once(owner, work, branch)
            edit = frozen_editing.status(k, owner, work, branch)
            assert edit["status"] not in {"EDITING_BLOCKED", "EDITION_REVISION_BLOCKED"}, edit
            if edit["status"] == "EDITORIAL_APPROVED":
                break
        assert edit["status"] == "EDITORIAL_APPROVED", edit
        assert final_book_automation.advance(k, gateway, owner, work, branch)["status"] == "FINAL_BOOK_REVIEW_ENROLLED"
        for _ in range(70):
            RuntimeSupervisor(k, gateway).run_once(owner, work, branch)
            state = book_review.review_status(k, owner, work, branch)
            assert state["state"] != "BLOCKED", state
            if state["state"] == "ASSESSED":
                break
        assert state["state"] == "ASSESSED" and state["round"] == 2
        assert state["result"]["status"] == "QUALITY_TARGET_MET" and len(blind_seen) == 8
    frozen = final_book_automation.advance(k, gateway, owner, work, branch)
    assert frozen["status"] == "BOOK_RELEASE_FROZEN"
    before = list(sent)
    assert final_book_automation.advance(k, gateway, owner, work, branch) == frozen
    assert sent == before
    with k.db.uow(write=False) as repo:
        assert all(repo.one("call_attempts", {"attempt_id": attempt})["state"] == "UNKNOWN" for attempt in native_sent)
        if missing_first is True:
            assert repo.one("tasks", {"task_id": missing[0]})["state"] == "WAITING_REVIEW"
