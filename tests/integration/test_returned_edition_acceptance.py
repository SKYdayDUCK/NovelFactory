"""PG17 child editions traverse native MCP checks and formal narrative acceptance."""
import json
from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
import test_production_strategy as production_fixture
from native_review_helpers import materialize_review_input, run_review_mcp
from test_native_final_book import _editorial
from test_p6_rolling_production import answer
from test_p6_rolling_production import request as rolling_request
from test_production_strategy import synthetic_production
from test_release import setup_batch

from agent_runtime.protocol import ModelResponse
from novel_factory.application import frozen_editing, rolling_production
from novel_factory.application.narrative import NarrativeService
from novel_factory.application.production_pipeline import _artifact
from novel_factory.application.returned_edition_acceptance import commit_edition
from novel_factory.application.supervisor import RuntimeSupervisor
from novel_factory.domain.errors import DomainError
from novel_factory.domain.reconciliation import span_for
from novel_factory.infrastructure.runtime_providers import RuntimeModelAdapter

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def returned_fixture(kernel, monkeypatch, *, unchanged=False, scenes=1, failures=None):
    if scenes > 1:
        monkeypatch.setattr(production_fixture, "setup_batch", lambda bound, count=1: setup_batch(bound, count=scenes))
        def grouped_request(bound, fixture, size):
            request = rolling_request(bound, fixture, size)
            chapters = request["volumes"][0]["chapters"]
            request["volumes"][0]["chapters"] = [{"chapter_id": chapters[0]["chapter_id"],
                "scenes": [scene for chapter in chapters for scene in chapter["scenes"]]}]
            return request
        monkeypatch.setattr(production_fixture, "request", grouped_request)
    gateway, owner, wr, _, _ = synthetic_production(kernel, monkeypatch, lengths=[7] * scenes, target=(6 * scenes, 10 * scenes))
    work, branch = wr["work_id"], wr["branch_id"]
    for _ in range(60):
        RuntimeSupervisor(kernel, gateway).run_once(owner, work, branch)
        if rolling_production.rolling_status(kernel, owner, work, branch)["state"] == "FROZEN":
            break
    approvals, sent, phase_calls = [], [], {}
    failures = failures or {}
    with kernel.db.uow(write=False) as repo:
        parent = rolling_production._load(kernel, repo, work, branch)["last_checkpoint"]

    def generate(adapter, request):
        identity = json.loads(request.messages[-1]["content"].split("\nIdentity: ", 1)[1].splitlines()[0])
        with kernel.db.uow(write=False) as repo:
            job = repo.one("supervisor_jobs", {"task_id": identity["task_id"]})["config"]
        adapter.journal.authorize_send(request.attempt_id)
        native = adapter.profile.kind == "codex-cli"
        thread, turn = "child-thread-" + request.attempt_id, "child-turn-" + request.attempt_id
        metadata = {"model": adapter.profile.model}
        if native:
            adapter.journal.record(request.attempt_id, thread, None, "THREAD_STARTED")
            adapter.journal.record(request.attempt_id, thread, turn, "TURN_STARTED")
            assert job["required_review_mcp"]
            metadata["review_mcp_receipt_refs"] = run_review_mcp(kernel, request, thread, turn)
            if not (job.get("returned_edition", {}).get("phase") == "epistemic"
                    and phase_calls.get("epistemic", 0) < failures.get("incomplete", 0)):
                adapter.journal.record(request.attempt_id, thread, turn, "COMPLETED")
            metadata.update(codex_thread_id=thread, codex_turn_id=turn, auth_mode="host-login", billing="SUBSCRIPTION_AMOUNT_UNKNOWN")
        else:
            assert adapter.profile.model == "deepseek-flash" and job["returned_edition"]["phase"] == "write"
        user = json.loads(request.messages[1]["content"])
        if native:
            user = materialize_review_input(kernel, user, metadata["review_mcp_receipt_refs"])
        user["context"] = identity
        mark = job.get("returned_edition")
        if mark:
            phase = mark["phase"]
            phase_calls[phase] = phase_calls.get(phase, 0) + 1
            if mark["phase"] == "write":
                before = [json.loads(row["content"])["text"] for row in user["authorized_materials"]
                          if "text" in json.loads(row["content"])][-1]
                feedback = json.loads(user["authorized_materials"][-1]["content"])
                if "failed_draft_ref" in feedback:
                    assert feedback["failed_draft_ref"] and feedback["findings"] and feedback["report_ref"]
                payload = {"text": before + ("" if unchanged else "字"), "important_deltas": [], "event_refs": [], "evidence_refs": []}
            elif phase == "verify":
                request_case = json.loads(user["authorized_materials"][-1]["content"])["case"]
                with kernel.db.uow(write=False) as repo:
                    query = next(ref for ref in metadata["review_mcp_receipt_refs"]
                        if kernel.artifact(repo, ref, work)["payload"]["tool"] == "nf_query_batch")
                citations = [{"kind": "ARTIFACT", "artifact_ref": query},
                             {"kind": "ARTIFACT", "artifact_ref": request_case["report_ref"]}]
                answers = [{"index": item["index"], "status": "NO_CONFLICT", "explanation": "Synthetic current-draft inquiry.",
                            "correction": None, "evidence_refs": citations} for item in request_case["questions"]]
                payload = {"decision": json.dumps({"case_hash": request_case["case_hash"], "answers": answers}),
                           "reason": "Synthetic independent inquiry.", "deferred_questions": [], "evidence_refs": citations}
            else:
                payload = answer(user)
                if phase == "epistemic" and phase_calls[phase] <= failures.get("epistemic", 0):
                    read = json.loads(user["authorized_materials"][0]["content"])
                    with kernel.db.uow(write=False) as repo:
                        query = next(ref for ref in metadata["review_mcp_receipt_refs"]
                            if kernel.artifact(repo, ref, work)["payload"]["tool"] == "nf_query_batch")
                    citations = [{"kind": "ARTIFACT", "artifact_ref": read["text_ref"], "span": span_for(read["text"], start, start + 1)}
                                 for start in range(2)] + [{"kind": "ARTIFACT", "artifact_ref": query}]
                    payload["issues"] = [{"issue_id": str(uuid4()), "category": "HARD_CONSISTENCY", "severity": "BLOCKER",
                        "subject_ref": read["draft_ref"], "description": "Synthetic independently anchored conflict.",
                        "evidence_refs": citations, "suggested_change": "Synthetic repair the two anchored clauses.",
                        "disposition": "OPEN", "issue_kind": "HARD_CONFLICT"}]
                if phase == "facts":
                    document = json.loads(payload["text"])
                    if phase_calls[phase] <= failures.get("facts", 0):
                        document["assertions"][0]["classification"] = "UNCERTAIN"
                    if phase_calls[phase] <= failures.get("facts_hard", 0):
                        document["assertions"][0].update(classification="READER_DISCLOSURE_CHANGE", disclosure_id=str(uuid4()))
                    payload["text"] = json.dumps(document)
                    if phase_calls[phase] <= failures.get("bad_extraction", 0):
                        payload["text"] = "{"
                if phase == "epistemic" and phase_calls[phase] <= failures.get("missing_mcp", 0):
                    metadata["review_mcp_receipt_refs"] = []
        else:
            assignment = json.loads(user["assignment"])
            text = json.loads(user["authorized_materials"][0]["content"])["text"]
            payload = (_editorial("SCENE", text) if identity["role_id"] == "reviewer"
                       else _editorial(assignment["phase"], text))
            if identity["role_id"] == "chief_editor" and assignment["phase"] == "STRUCTURE":
                payload["scene_functions"] = [{"scene": index + 1, "function": "Synthetic scene function",
                    "quote": json.loads(row["content"])["text"]} for index, row in enumerate(user["authorized_materials"])]
            if assignment["phase"] == "APPROVAL":
                if not approvals:
                    payload["decision"] = "RETURNED"
                    payload["reason"] = "Synthetic explicit child revision request."
                approvals.append(identity["task_id"])
        sent.append((identity["task_id"], request.attempt_id))
        output = json.dumps({**identity, "status": "SUCCEEDED", "payload": payload, "unresolved_questions": []})
        if mark and mark["phase"] == "epistemic" and phase_calls["epistemic"] <= failures.get("malformed", 0):
            output = "{"
        return ModelResponse(text=output,
            request_sha256=request.request_sha256, provider_receipt=turn, input_tokens=30, output_tokens=40,
            actual_amount=None if native else Decimal(0), currency=None if native else "USD", raw_response=metadata)

    monkeypatch.setattr(RuntimeModelAdapter, "generate", generate)
    frozen_editing.start(kernel, owner, work, branch)
    return gateway, owner, wr, parent, sent


def drive_returned(kernel, gateway, owner, wr, *, ready_only=False):
    for _ in range(130):
        RuntimeSupervisor(kernel, gateway).run_once(owner, wr["work_id"], wr["branch_id"])
        state = frozen_editing.status(kernel, owner, wr["work_id"], wr["branch_id"])
        assert state["status"] not in {"EDITING_BLOCKED", "EDITION_REVISION_BLOCKED"}, state
        with kernel.db.uow(write=False) as repo:
            full = frozen_editing._state(kernel, repo, wr["work_id"], wr["branch_id"])
        if ready_only and full.get("edition", {}).get("phase") == "accept":
            return full
        if full["status"] == "EDITION_ACCEPTED":
            return full
    pytest.fail(f"Child edition did not finish its bounded formal acceptance path: {state!r}; {full.get('edition')!r}")


def test_returned_child_is_formally_accepted_with_immutable_parent_and_new_editing_session(kernel_env, monkeypatch):
    kernel = kernel_env["kernel"]
    gateway, owner, wr, parent, sent = returned_fixture(kernel, monkeypatch)
    state = drive_returned(kernel, gateway, owner, wr)
    assert state["status"] == "EDITION_ACCEPTED"
    with kernel.db.uow(write=False) as repo:
        cursor = rolling_production._load(kernel, repo, wr["work_id"], wr["branch_id"])
        assert cursor["last_checkpoint"] != parent and cursor["frozen_chapters"] == 1
        current = kernel.artifact(repo, cursor["last_checkpoint"], wr["work_id"], kind="ROLLING_CHECKPOINT")["payload"]
        historical = kernel.artifact(repo, parent, wr["work_id"], kind="ROLLING_CHECKPOINT")["payload"]
        assert current["parent_checkpoint"] == parent and current["previous"] == historical["previous"]
        child = current["chapters"][0]["scenes"][0]
        old = historical["chapters"][0]["scenes"][0]
        assert child["draft_ref"] != old["draft_ref"] and child["acceptance_ref"] != old["acceptance_ref"]
        formal = kernel.artifact(repo, child["acceptance_ref"], wr["work_id"], kind="NARRATIVE_ACCEPTANCE")["payload"]
        assert formal["draft_ref"] == child["draft_ref"] and formal["world_effects_applied"] == 0
        assert kernel.head(repo, wr["work_id"], wr["branch_id"]) == wr
    assert frozen_editing.start(kernel, owner, wr["work_id"], wr["branch_id"])["status"] == "EDITING_ACTIVE"
    assert len(sent) == len({attempt for _, attempt in sent})


def test_child_cannot_replace_a_frozen_scene_through_the_ordinary_acceptance_route(kernel_env, monkeypatch):
    kernel = kernel_env["kernel"]
    gateway, owner, wr, _, _ = returned_fixture(kernel, monkeypatch)
    state = drive_returned(kernel, gateway, owner, wr, ready_only=True)
    child = state["edition"]["candidates"][0]
    with pytest.raises(DomainError) as error:
        NarrativeService(kernel).accept_narrative(owner, child["draft_ref"], child["reconciliation_ref"], wr, "ordinary-child-bypass",
                                                 alignment_ref=child["alignment_ref"])
    assert error.value.code == "SERIAL_PREFIX_PROTECTED"
    another = kernel.issue_grant(owner, wr["work_id"], role_id="control_operator", branch_ids=[wr["branch_id"]],
        capabilities=["READ", "ADMIN", "ACCEPT"], planes=["WORLD"])["principal"]
    with pytest.raises(DomainError) as delegated:
        NarrativeService(kernel).accept_narrative(another, child["draft_ref"], child["reconciliation_ref"], wr, "delegated-child-bypass",
            alignment_ref=child["alignment_ref"], edition_ref=state["edition_ref"])
    assert delegated.value.code == "VIEW_DENIED"
    with pytest.raises(DomainError) as wrong_manifest:
        NarrativeService(kernel).accept_narrative(owner, child["draft_ref"], child["reconciliation_ref"], wr, "wrong-child-manifest",
            alignment_ref=child["alignment_ref"], edition_ref=state["edition"]["editor_output_ref"])
    assert wrong_manifest.value.code == "VIEW_DENIED"


def test_two_scene_child_edition_rolls_back_second_acceptance_fault_and_resumes_once(kernel_env, monkeypatch):
    kernel = kernel_env["kernel"]
    gateway, owner, wr, parent, _ = returned_fixture(kernel, monkeypatch, scenes=2)
    state = drive_returned(kernel, gateway, owner, wr, ready_only=True)
    assert len(state["edition"]["candidates"]) == 2
    children = state["edition"]["candidates"]
    accept = NarrativeService.accept_narrative
    accepted_first = []
    def injected(service, principal, draft_ref, *args, **kwargs):
        if kwargs.get("edition_ref") and draft_ref == children[1]["draft_ref"]:
            raise DomainError("INJECTED_SECOND_ACCEPTANCE", "Synthetic transaction fault")
        result = accept(service, principal, draft_ref, *args, **kwargs)
        accepted_first.append(result)
        return result
    monkeypatch.setattr(NarrativeService, "accept_narrative", injected)
    with pytest.raises(DomainError) as fault, kernel.objects.lock(), kernel.db.uow() as repo:
        repo.one("works", {"work_id": wr["work_id"]}, lock="update")
        bound = kernel.in_transaction(repo)
        live = frozen_editing._state(bound, repo, wr["work_id"], wr["branch_id"])
        cursor = rolling_production._load(bound, repo, wr["work_id"], wr["branch_id"])
        commit_edition(bound, repo, owner, live, cursor)
    assert fault.value.code == "INJECTED_SECOND_ACCEPTANCE" and len(accepted_first) == 1
    with kernel.db.uow(write=False) as repo:
        cursor = rolling_production._load(kernel, repo, wr["work_id"], wr["branch_id"])
        assert cursor["last_checkpoint"] == parent
        for child in children:
            selected = repo.one("selections", {"work_id": wr["work_id"], "selection_kind": "NARRATIVE_SCENE:" + child["scene_id"]})
            original = kernel.artifact(repo, selected["artifact_ref"], wr["work_id"], kind="NARRATIVE_ACCEPTANCE")["payload"]
            assert original["draft_ref"] == child["parent_ref"]
    monkeypatch.setattr(NarrativeService, "accept_narrative", accept)
    # A fresh supervisor replays only the rolled-back acceptance transaction.
    RuntimeSupervisor(kernel, gateway).run_once(owner, wr["work_id"], wr["branch_id"])
    with kernel.db.uow(write=False) as repo:
        cursor = rolling_production._load(kernel, repo, wr["work_id"], wr["branch_id"])
        assert cursor["last_checkpoint"] != parent
        checkpoint = kernel.artifact(repo, cursor["last_checkpoint"], wr["work_id"], kind="ROLLING_CHECKPOINT")["payload"]
        assert len(checkpoint["chapters"]) == 1 and len(checkpoint["chapters"][0]["scenes"]) == 2
        reading = kernel.artifact(repo, checkpoint["chapters"][0]["reading_ref"], wr["work_id"],
                                  kind="RETURNED_EDITION_CHAPTER_READING")["payload"]
        assert reading["draft_refs"] == [child["draft_ref"] for child in children]
        assert reading["literary_quality"] == "PENDING_FRESH_VOLUME_EDIT"
        receipts = repo.many("receipts", {"work_id": wr["work_id"], "command_type": "NARRATIVE_ACCEPT"})
        new = [row for row in receipts if row["idempotency_key"].startswith("returned-child-accept:")]
        assert len(new) == 2


def test_unchanged_native_child_stops_without_replacing_parent_or_spending_again(kernel_env, monkeypatch):
    kernel = kernel_env["kernel"]
    gateway, owner, wr, parent, sent = returned_fixture(kernel, monkeypatch, unchanged=True)
    for _ in range(90):
        RuntimeSupervisor(kernel, gateway).run_once(owner, wr["work_id"], wr["branch_id"])
        status = frozen_editing.status(kernel, owner, wr["work_id"], wr["branch_id"])
        if status["status"] == "EDITION_REVISION_BLOCKED":
            break
    assert status["reason"] == "EDITORIAL_NO_PROGRESS"
    with kernel.db.uow(write=False) as repo:
        assert rolling_production._load(kernel, repo, wr["work_id"], wr["branch_id"])["last_checkpoint"] == parent
        assert not repo.many("artifacts", {"work_id": wr["work_id"], "kind": "RETURNED_EDITION_ACCEPTANCE"})
    count = len(sent)
    for _ in range(3):
        RuntimeSupervisor(kernel, gateway).run_once(owner, wr["work_id"], wr["branch_id"])
    assert len(sent) == count


@pytest.mark.parametrize("failure", ["epistemic", "facts_hard", "facts", "missing_mcp", "malformed", "bad_extraction"])
def test_child_review_failure_recovers_with_exact_evidence_and_bounded_independent_rereading(kernel_env, monkeypatch, failure):
    kernel = kernel_env["kernel"]
    gateway, owner, wr, parent, sent = returned_fixture(kernel, monkeypatch, failures={failure: 1})
    state = drive_returned(kernel, gateway, owner, wr)
    assert state["status"] == "EDITION_ACCEPTED"
    edition = state["edition"]
    if failure in {"epistemic", "facts_hard"}:
        assert edition["scene_reworks"] == {"0": 1} and len(edition["candidate_history"]) == 1
        failed = edition["candidate_history"][0]
        assert failed["parent_ref"] == edition["candidates"][0]["parent_ref"]
        with kernel.db.uow(write=False) as repo:
            feedback = kernel.artifact(repo, failed["failure_feedback_ref"], wr["work_id"], kind="RETURNED_EDITION_REVISION_FEEDBACK")["payload"]
            assert feedback["failed_draft_ref"] == failed["draft_ref"] and feedback["findings"]
            assert feedback["report_ref"]
    else:
        assert edition["scene_reworks"] == {} and edition["candidate_history"] == []
        if failure == "facts":
            assert edition["fact_rereads"] == 1
            with kernel.db.uow(write=False) as repo:
                assert len(repo.many("receipts", {"work_id": wr["work_id"], "command_type": "INTERNAL_REVIEW_RESOLUTION_CONSUME"})) == 1
        else:
            history = state[{"missing_mcp": "evidence_repairs", "malformed": "format_history", "bad_extraction": "technical_history"}[failure]]
            assert sum(len(rows) for rows in history.values()) == 1
    with kernel.db.uow(write=False) as repo:
        checkpoint = rolling_production._load(kernel, repo, wr["work_id"], wr["branch_id"])["last_checkpoint"]
        assert kernel.artifact(repo, checkpoint, wr["work_id"])["payload"]["parent_checkpoint"] == parent
    assert len(sent) == len({attempt for _, attempt in sent})


@pytest.mark.parametrize("failure,reason", [("epistemic", "EDITION_REWORK_EXHAUSTED"),
    ("facts", "EDITION_REWORK_EXHAUSTED"), ("missing_mcp", "REVIEW_EVIDENCE_REPAIR_EXHAUSTED"),
    ("malformed", "TECHNICAL_REPAIR_BUDGET_EXHAUSTED"), ("incomplete", "CALL_RECONCILIATION_REQUIRED")])
def test_child_repeated_failure_stops_with_frozen_parent_and_no_unknown_resend(kernel_env, monkeypatch, failure, reason):
    kernel = kernel_env["kernel"]
    gateway, owner, wr, parent, sent = returned_fixture(kernel, monkeypatch, failures={failure: 99})
    for _ in range(170):
        RuntimeSupervisor(kernel, gateway).run_once(owner, wr["work_id"], wr["branch_id"])
        status = frozen_editing.status(kernel, owner, wr["work_id"], wr["branch_id"])
        if status["status"] == "EDITION_REVISION_BLOCKED":
            break
    assert status["status"] == "EDITION_REVISION_BLOCKED" and status["reason"] == reason, status
    with kernel.db.uow(write=False) as repo:
        full = frozen_editing._state(kernel, repo, wr["work_id"], wr["branch_id"])
        assert rolling_production._load(kernel, repo, wr["work_id"], wr["branch_id"])["last_checkpoint"] == parent
        assert not repo.many("artifacts", {"work_id": wr["work_id"], "kind": "RETURNED_EDITION_ACCEPTANCE"})
        assert full["edition"]["scene_reworks"].get("0", 0) <= 2
        tasks = repo.many("tasks", {"work_id": wr["work_id"]})
        calls = repo.many("call_attempts", {"task_id": [row["task_id"] for row in tasks]})
        native = [row for row in calls if row["state"] == "UNKNOWN"]
        assert native
    count = len(sent)
    for _ in range(4):
        RuntimeSupervisor(kernel, gateway).run_once(owner, wr["work_id"], wr["branch_id"])
    assert len(sent) == count


def test_rejected_native_format_repair_rejects_a_different_attempt_even_with_rehashed_audit(kernel_env, monkeypatch):
    from novel_factory.application.frozen_format_recovery import recover
    kernel = kernel_env["kernel"]
    gateway, owner, wr, _, sent = returned_fixture(kernel, monkeypatch, failures={"malformed": 1})
    for _ in range(100):
        RuntimeSupervisor(kernel, gateway).run_once(owner, wr["work_id"], wr["branch_id"])
        with kernel.db.uow(write=False) as repo:
            state = frozen_editing._state(kernel, repo, wr["work_id"], wr["branch_id"])
            task = repo.one("tasks", {"task_id": state["active"]}) if state.get("active") else None
        if task and task["state"] == "FAILED" and state.get("edition", {}).get("phase") == "epistemic":
            break
    assert task["state"] == "FAILED"
    calls_before = len(sent)
    with kernel.objects.lock(), kernel.db.uow() as repo:
        repo.one("works", {"work_id": wr["work_id"]}, lock="update")
        bound = kernel.in_transaction(repo)
        call = repo.one("call_attempts", {"task_id": task["task_id"], "generation": task["generation"], "call_key": "supervised-candidate"})
        original = gateway._read_audit(repo, call["attempt_id"])
        assert original["payload"]["output_ref"] is None and call["state"] == "UNKNOWN"
        # A storage/evidence corruption fixture deliberately rehashes the fake
        # audit and current-result reference, leaving the authoritative call fixed.
        for corrupt in ("attempt", "reservation", "response_revision"):
            with pytest.raises(DomainError) as rejected, repo.conn.begin_nested():
                payload = deepcopy(original["payload"])
                if corrupt == "attempt":
                    payload["attempt_id"] = str(uuid4())
                elif corrupt == "response_revision":
                    response = bound.artifact(repo, payload["response_ref"], wr["work_id"], kind="AGENT_RESPONSE")["payload"]
                    payload["response_ref"] = _artifact(bound, repo, wr["work_id"], owner, "AGENT_RESPONSE", response)
                fake_ref = _artifact(bound, repo, wr["work_id"], owner, "AGENT_CALL_EVIDENCE", payload)
                fake_row = repo.one("artifacts", {"artifact_revision_id": fake_ref["artifact_revision_id"]})
                # Authoritative PG task_results are immutable. Inject a corrupt
                # read view, while leaving that database record untouched.
                class CorruptReadView:
                    def __init__(self, fault_ref, fault_kind):
                        self.fault_ref, self.fault_kind = fault_ref, fault_kind
                    def __getattr__(self, key):
                        return getattr(repo, key)
                    def one(self, table, filters, **kwargs):
                        row = repo.one(table, filters, **kwargs)
                        if table == "task_results":
                            return {**row, "payload": {**row["payload"], "artifacts": [self.fault_ref]}}
                        if table == "call_attempts" and self.fault_kind == "reservation":
                            return {**row, "reserved_tokens": row["reserved_tokens"] - 1}
                        return row
                fake_gateway = SimpleNamespace(_read_audit=lambda repo, attempt, row=fake_row: row)
                recover(bound, CorruptReadView(fake_ref, corrupt), fake_gateway, owner, deepcopy(state), task)
            assert rejected.value.code == "INVALID_EVIDENCE", corrupt
        assert repo.one("call_attempts", {"attempt_id": call["attempt_id"]}) == call
    assert len(sent) == calls_before
