"""Real task-bound MCP memory transport; native Host and all provider I/O are synthetic."""
import asyncio
import json
from datetime import timedelta
from uuid import uuid4

import pytest
from test_release import setup_batch, write_scene

from agent_runtime import native_codex, native_review, providers
from agent_runtime.protocol import ModelResponse
from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.narrative import NarrativeService
from novel_factory.application.quality_memory import _record
from novel_factory.application.review_mcp import (
    RECEIPT_KIND,
    ReviewMcpBroker,
    require_review_queries,
)
from novel_factory.application.runtime_tasks import RuntimeTaskService
from novel_factory.application.supervisor import HostJournal
from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import point
from novel_factory.infrastructure.creative_config import CreativeHarnessConfig
from novel_factory.infrastructure.runtime_providers import RuntimeModelAdapter

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def reviewed(kernel, monkeypatch, *, mode="valid", materials=True, task_kind="literary", text="合成稿件：窗边有雨。",
             declared_deltas=(), work=None, source_draft=None):
    fixture = work or setup_batch(kernel, count=1)
    owner, wr = fixture["principal"], fixture["world_ref"]
    editor = mode == "editor"
    plan = mode == "plan_switch" or editor
    belief = mode in {"belief", "belief_outside_actor"}
    role = "literary_editor" if editor else "chief_editor" if plan else "reviewer"
    if editor or plan or belief:
        task_kind = "seam" if editor else "editorial_decision" if plan else "verification"
    draft = (source_draft or write_scene(kernel, fixture, 0, text, declared_important_deltas=declared_deltas)) if materials else None
    monkeypatch.setattr(providers.Profile, "preflight", lambda _: None)
    forbidden = lambda *args, **kwargs: pytest.fail("Synthetic MCP test reached a live model boundary")
    monkeypatch.setattr(native_codex, "generate", forbidden)
    monkeypatch.setattr(native_codex, "start_host", forbidden)
    monkeypatch.setattr(native_review, "generate", forbidden)
    monkeypatch.setattr(native_review, "start_host", forbidden)
    monkeypatch.setattr(providers, "post", forbidden)
    journal = HostJournal(kernel)
    adapter = RuntimeModelAdapter(providers.Profile("codex-cli", "gpt-6.1-sol", "SYNTHETIC_NO_CREDENTIAL", 2048,
        auth_mode="host-login", model_output_ceiling=2048), journal)
    calls, returned, tool_outputs = [], [], []
    def generate(request):
        journal.authorize_send(request.attempt_id)
        thread, turn = "synthetic-thread-" + request.attempt_id, "synthetic-turn-" + request.attempt_id
        journal.record(request.attempt_id, thread, None, "THREAD_STARTED")
        journal.record(request.attempt_id, thread, turn, "TURN_STARTED")
        calls.append(request)
        async def read():
            async with ReviewMcpBroker.from_attempt(kernel, request) as broker:
                broker.bind(thread, turn)
                context = {"threadId": thread, "turnId": turn, "callId": "context", "tool": "nf_context_get", "arguments": {}}
                tool_outputs.append(await broker.call(context))
                if mode in {"wrong_host_thread", "expired_lease", "write_tool"}:
                    rejected = {**context, "callId": "denied"}
                    if mode == "wrong_host_thread":
                        rejected["threadId"] = "unrelated-thread"
                    if mode == "write_tool":
                        rejected["tool"] = "nf_artifact_put"
                    if mode == "expired_lease":
                        with kernel.db.uow() as repo:
                            live = repo.one("tasks", {"task_id": broker.scope["task_id"]})
                            repo.change("tasks", {"task_id": live["task_id"]}, {"lease_expiry": repo.now() - timedelta(seconds=1)})
                    with pytest.raises(DomainError):
                        await broker.call(rejected)
                    if mode == "expired_lease":
                        with kernel.db.uow() as repo:
                            repo.change("tasks", {"task_id": live["task_id"]}, {"lease_expiry": live["lease_expiry"]})
                if mode == "duplicate_call":
                    assert await broker.call(context) == tool_outputs[-1]
                if mode != "no_query":
                    if "nf_query_batch" in broker.allowed_tools:
                        lookup = {"subject_id": str(uuid4()) if mode == "denied_query" else fixture["ids"]["sword"], "predicate": "possessed_by"}
                        if mode == "world_history":
                            lookup["at"] = point(1)
                        if mode == "future_at":
                            lookup["at"] = point(13)
                        if belief:
                            lookup.update(plane="BELIEF", actor_id=str(uuid4()) if mode == "belief_outside_actor" else fixture["ids"]["protagonist"],
                                          at=point(1), known_at=point(1))
                        if mode == "plan_switch":
                            lookup["plane"] = "WORLD"
                        tool_outputs.append(await broker.call({"threadId": thread, "turnId": turn, "callId": "query", "tool": "nf_query_batch",
                                                              "arguments": {"lookups": [lookup]}}))
                    else:
                        with pytest.raises(DomainError):
                            await broker.call({"threadId": thread, "turnId": turn, "callId": "forbidden-world-query", "tool": "nf_query_batch",
                                "arguments": {"lookups": [{"subject_id": fixture["ids"]["sword"], "predicate": "possessed_by"}]}})
                if draft and mode != "no_material":
                    tool_outputs.append(await broker.call({"threadId": thread, "turnId": turn, "callId": "material",
                        "tool": "nf_review_material", "arguments": {"artifact_ref": draft["draft_ref"]}}))
                if mode == "limit":
                    for index in range(13):
                        await broker.call({"threadId": thread, "turnId": turn, "callId": "bounded-query-" + str(index),
                            "tool": "nf_query_batch", "arguments": {"lookups": [{"subject_id": fixture["ids"]["sword"], "predicate": "possessed_by"}]}})
                    with pytest.raises(DomainError) as bounded:
                        await broker.call({**context, "callId": "seventeenth"})
                    assert bounded.value.code == "CAPACITY_LIMIT"
                return list(broker.receipt_refs)
        refs = asyncio.run(read())
        if mode in {"missing_audit", "wrong_thread", "wrong_world", "wrong_worker", "wrong_grant", "wrong_result"}:
            with kernel.objects.lock(), kernel.db.uow() as repo:
                original = kernel.artifact(repo, refs[0], wr["work_id"])["payload"]
                altered = {**original}
                if mode == "missing_audit":
                    altered["invocation_id"] = str(uuid4())
                if mode == "wrong_thread":
                    altered["thread_id"] = "another-thread"
                if mode == "wrong_world":
                    altered["world_ref"] = {**wr, "world_revision_id": str(uuid4())}
                if mode == "wrong_worker":
                    altered["worker_id"] = "another-worker"
                if mode == "wrong_grant":
                    altered["grant_id"] = str(uuid4())
                if mode == "wrong_result":
                    altered["result"] = {"isError": False, "structuredContent": {"fiction": "model claim"}}
                refs[0] = _record(kernel, repo, wr["work_id"], owner.principal_id, request.attempt_id, "synthetic-forgery:" + mode, altered, RECEIPT_KIND)
        returned.extend(refs)
        journal.record(request.attempt_id, thread, turn, "COMPLETED")
        packet = json.loads(request.messages[1]["content"])
        ctx = packet["context"]
        envelope = {"schema_version": "2.1", **{k: ctx[k] for k in
            ("task_id", "generation", "context_bundle_id", "world_ref", "role_id", "actor_id")},
            "status": "SUCCEEDED", "unresolved_questions": [], "payload": {
                "summary": "Synthetic independently queried review", "issues": [], "counterinterpretations": [], "evidence_refs": []}}
        if editor:
            envelope["payload"] = {"text": "Synthetic literary edit", "important_deltas": [], "event_refs": [], "evidence_refs": []}
        elif role == "chief_editor" or task_kind == "verification":
            envelope["payload"] = {"decision": "Synthetic query test", "reason": "Synthetic scope fixture", "deferred_questions": [], "evidence_refs": []}
        return ModelResponse(text=json.dumps(envelope), request_sha256=request.request_sha256, provider_receipt=turn,
            input_tokens=25, output_tokens=40, cached_input_tokens=0, actual_amount=None, currency=None,
            raw_response={"codex_thread_id": thread, "codex_turn_id": turn, "model": adapter.profile.model,
                "auth_mode": "host-login", "billing": "SUBSCRIPTION_AMOUNT_UNKNOWN", "review_mcp_receipt_refs": refs})
    monkeypatch.setattr(adapter, "generate", generate)
    gateway = AgentGateway(kernel, {"native": adapter}, creative_harness=CreativeHarnessConfig(),
        material_reader=lambda principal, **kw: NarrativeService(kernel).read_agent_material(principal, **kw))
    extra = {}
    if plan:
        from novel_factory.application.volume_successor import _selected
        with kernel.db.uow(write=False) as repo:
            manifest = _selected(repo, wr["work_id"], "PLAN_MANIFEST:" + fixture["volume_id"])
        extra.update(knowledge_cut={"plane": "PLAN", "plan_manifest_ref": manifest}, input_artifacts=[manifest])
    prepared = RuntimeTaskService(kernel, gateway).prepare(owner, wr, role_id=role, task_kind=task_kind, story_time=point(12),
        subject_ids=[fixture["ids"]["sword"], fixture["ids"]["protagonist"]], predicates=["possessed_by"], assignment="Synthetic MCP-backed reading",
        adapter_id="native", material_refs=[draft["draft_ref"]] if draft else [], review_belief=belief, **extra)
    if draft:
        NarrativeService(kernel).bind_reader_task(owner, prepared.task["task_id"], prepared.task["generation"], [draft["draft_ref"]], "synthetic-reading")
    result = gateway.execute(prepared.principal, {"schema_version": "2.1", "task_id": prepared.task["task_id"], "generation": prepared.task["generation"],
        "worker_id": prepared.worker_id, "input_ref": prepared.agent_input["input_ref"], "budget_id": kernel.tasks.create_budget(wr["work_id"], "1"),
        "call_key": "supervised-candidate", "reserve_amount": "0.1", "token_reservation": 100000})
    assert result["status"] == "RESULT_STORED", result
    return gateway, result, owner, prepared, returned, tool_outputs, calls, draft


@pytest.mark.parametrize("mode", ["valid", "duplicate_call", "wrong_host_thread", "expired_lease", "write_tool", "limit"])
def test_actual_mcp_reads_have_receipts_and_remain_verifiable_after_acceptance(kernel_env, monkeypatch, mode):
    kernel = kernel_env["kernel"]
    gateway, result, _owner, prepared, refs, outputs, calls, draft = reviewed(kernel, monkeypatch, mode=mode)
    with kernel.db.uow(write=False) as repo:
        task = repo.one("tasks", {"task_id": prepared.task["task_id"]})
        evidence = gateway._read_audit(repo, result["attempt_id"])["payload"]
        call_before = repo.one("call_attempts", {"attempt_id": result["attempt_id"]})
        budget_before = repo.one("budgets", {"budget_id": call_before["budget_id"]})
        proof = require_review_queries(kernel, repo, task, evidence)
        assert {r["tool"] for r in proof} == {"nf_context_get", "nf_query_batch", "nf_review_material"}
        assert len(refs) == len(proof) == (16 if mode == "limit" else 3)
        material = next(r for r in proof if r["tool"] == "nf_review_material")["result"]["structuredContent"]
        assert material["draft_ref"] == draft["draft_ref"] and material["text"] == draft["text"]
        assert material["passages"] and material["passages"][0]["span"]
        for out, ref in zip(outputs, refs):
            assert json.loads(out["contentItems"][0]["text"])["receipt_ref"] == ref
        assert repo.one("call_attempts", {"attempt_id": result["attempt_id"]}) == call_before
        assert repo.one("budgets", {"budget_id": budget_before["budget_id"]}) == budget_before
    with kernel.db.uow() as repo:
        kernel.tasks.accept_in_transaction(repo.conn, prepared.task["task_id"], prepared.task["generation"])
    with kernel.db.uow(write=False) as repo:
        accepted = repo.one("tasks", {"task_id": prepared.task["task_id"]})
        assert accepted["state"] == "ACCEPTED" and accepted["lease_owner"] is None
        assert len(require_review_queries(kernel, repo, accepted, evidence)) == len(refs)
    assert len(calls) == 1


@pytest.mark.parametrize("mode", ["no_query", "denied_query", "no_material", "missing_audit", "wrong_thread", "wrong_world",
                                  "wrong_worker", "wrong_grant", "wrong_result"])
def test_mcp_claims_or_unrelated_receipts_cannot_clear_review(kernel_env, monkeypatch, mode):
    kernel = kernel_env["kernel"]
    gateway, result, _, prepared, _, _, _, _ = reviewed(kernel, monkeypatch, mode=mode)
    with kernel.db.uow(write=False) as repo:
        task = repo.one("tasks", {"task_id": prepared.task["task_id"]})
        evidence = gateway._read_audit(repo, result["attempt_id"])["payload"]
        with pytest.raises(DomainError) as rejected:
            require_review_queries(kernel, repo, task, evidence)
        assert rejected.value.code == "REVIEW_QUERY_REQUIRED"


def test_mcp_receipts_cannot_be_rebound_to_another_attempt_or_revoked_grant(kernel_env, monkeypatch):
    kernel = kernel_env["kernel"]
    first = reviewed(kernel, monkeypatch, materials=False)
    second = reviewed(kernel, monkeypatch, materials=False)
    gateway, result, owner, prepared = second[:4]
    with kernel.db.uow(write=False) as repo:
        task = repo.one("tasks", {"task_id": prepared.task["task_id"]})
        evidence = gateway._read_audit(repo, result["attempt_id"])["payload"]
        unrelated = first[0]._read_audit(repo, first[1]["attempt_id"])["payload"]
        with pytest.raises(DomainError):
            require_review_queries(kernel, repo, task, unrelated)
        with pytest.raises(DomainError):
            require_review_queries(kernel, repo, task, evidence, receipt_refs=first[4])
    kernel.revoke_grant(owner, prepared.principal.grant_id)
    with kernel.db.uow(write=False) as repo, pytest.raises(DomainError):
        require_review_queries(kernel, repo, task, evidence)


@pytest.mark.parametrize("mode", ["world_history", "belief", "editor"])
def test_mcp_query_selectors_and_editor_material_only_scope(kernel_env, monkeypatch, mode):
    kernel = kernel_env["kernel"]
    gateway, result, _, prepared, _, outputs, _, _ = reviewed(kernel, monkeypatch, mode=mode)
    with kernel.db.uow(write=False) as repo:
        task = repo.one("tasks", {"task_id": prepared.task["task_id"]})
        evidence = gateway._read_audit(repo, result["attempt_id"])["payload"]
        receipts = require_review_queries(kernel, repo, task, evidence)
    if mode == "editor":
        assert {r["tool"] for r in receipts} == {"nf_context_get", "nf_review_material"}
        assert task["role_id"] == "literary_editor"
    else:
        query = next(r for r in receipts if r["tool"] == "nf_query_batch")
        assert query["arguments"]["lookups"][0]["at"] == point(1)
        if mode == "belief":
            assert query["arguments"]["lookups"][0]["known_at"] == point(1)
            assert '"knowledge_status": "UNKNOWN"' in json.dumps(query["result"])
            assert all(r["result"]["isError"] is False for r in receipts)
    assert all(json.loads(output["contentItems"][0]["text"])["result"] for output in outputs)


@pytest.mark.parametrize("mode", ["belief_outside_actor", "future_at", "plan_switch"])
def test_mcp_query_selectors_reject_outside_actor_future_and_plan_to_world(kernel_env, monkeypatch, mode):
    kernel = kernel_env["kernel"]
    gateway, result, _, prepared, _, _, _, _ = reviewed(kernel, monkeypatch, mode=mode, materials=False)
    with kernel.db.uow(write=False) as repo:
        task = repo.one("tasks", {"task_id": prepared.task["task_id"]})
        evidence = gateway._read_audit(repo, result["attempt_id"])["payload"]
        with pytest.raises(DomainError) as denied:
            require_review_queries(kernel, repo, task, evidence)
        assert denied.value.code == "REVIEW_QUERY_REQUIRED"
