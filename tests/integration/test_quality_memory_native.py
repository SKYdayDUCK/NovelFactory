"""Only completed, independent readings of a frozen repair become reusable experience.

All model returns and journals here are synthetic, with live HTTP/native boundaries blocked.
"""
import json
from dataclasses import replace

import pytest
from native_review_helpers import materialize_review_input, run_review_mcp
from test_native_delivery import NativeProtocolFixture
from test_p6_postpaid_delivery import fake_transport

from agent_runtime import native_codex, native_review, providers
from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.quality_memory import (
    METHODS,
    plan_repair,
    recall,
    reflect_repair,
    task_scope,
    verify_method,
)
from novel_factory.application.runtime_tasks import RuntimeTaskService
from novel_factory.fixtures import load_fixture, point

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


class NativeReading(NativeProtocolFixture):
    @property
    def configuration(self):
        return {**super().configuration, "model": "gpt-6.1-sol"}

    def generate(self, request):
        model_view = json.loads(request.messages[1]["content"])
        with self.journal.kernel.db.uow(write=False) as repo:
            call = repo.one("call_attempts", {"attempt_id": request.attempt_id})
            job = repo.one("supervisor_jobs", {"task_id": call["task_id"]})
        if job and job["config"].get("required_review_mcp"):
            original = self.mode
            self.mode = "incomplete"
            try:
                response = super().generate(request)
            finally:
                self.mode = original
            refs = run_review_mcp(self.journal.kernel, request, "thread-" + request.attempt_id, "turn-" + request.attempt_id)
            model_view = materialize_review_input(self.journal.kernel, model_view, refs)
            self.journal.record(request.attempt_id, "thread-" + request.attempt_id, "turn-" + request.attempt_id, "COMPLETED")
            response = replace(response, raw_response={**response.raw_response, "review_mcp_receipt_refs": refs})
        else:
            response = super().generate(request)
        envelope = json.loads(response.text)
        envelope["payload"] = {"summary": "Synthetic independent reading", "issues": [],
                               "counterinterpretations": [], "evidence_refs": []}
        if hasattr(self, "respond"):
            envelope["payload"] = self.respond(model_view)
        if self.mode == "unresolved":
            envelope["unresolved_questions"] = ["Synthetic missing prerequisite"]
        if self.mode == "blocker":
            from uuid import uuid4
            envelope["payload"]["issues"] = [{"issue_id": str(uuid4()), "category": "CONTINUITY", "severity": "BLOCKER",
                "subject_ref": json.loads(json.loads(request.messages[1]["content"])["assignment"])["candidate_ref"],
                "description": "Synthetic unresolved defect", "evidence_refs": [], "suggested_change": "Repair",
                "disposition": "OPEN", "issue_kind": "CHANGE_REQUEST"}]
        return replace(response, text=json.dumps(envelope))


def dispatch(kernel, gateway, owner, wr, world, *, role, kind, adapter, assignment, refs=()):
    prepared = RuntimeTaskService(kernel, gateway).prepare(owner, wr,
        role_id=role, task_kind=kind, story_time=point(1), subject_ids=[world["ids"]["sword"]],
        predicates=["owner"], assignment=assignment, adapter_id=adapter, input_artifacts=refs)
    result = gateway.execute(prepared.principal, {"schema_version": "2.1", "task_id": prepared.task["task_id"],
        "generation": prepared.task["generation"], "worker_id": prepared.worker_id,
        "input_ref": prepared.agent_input["input_ref"], "budget_id": kernel.tasks.create_budget(wr["work_id"], "1"),
        "call_key": "supervised-candidate", "reserve_amount": "0.1", "token_reservation": 100000})
    assert result["status"] == "RESULT_STORED"
    return result


@pytest.mark.parametrize("mode", ["valid", "deepseek_review", "missing_journal", "incomplete", "wrong_model",
                                  "wrong_turn", "wrong_revision", "same_principal", "missing_wire",
                                  "wrong_generation", "wrong_request", "unresolved", "blocker",
                                  "unused_method", "wrong_plan_error"])
def test_flash_repair_native_reading_requires_exact_independent_delivery(kernel_env, monkeypatch, mode):
    kernel = kernel_env["kernel"]
    world = load_fixture(kernel)
    owner, wr = world["principal"], world["world_ref"]
    def reply(packet):
        if packet["context"]["role_id"] == "reviewer":
            return {"summary": "Synthetic DeepSeek reading", "issues": [], "counterinterpretations": [], "evidence_refs": []}
        return {"text": "合成修订正文", "important_deltas": [], "event_refs": [], "evidence_refs": []}
    flash, requests = fake_transport(kernel, monkeypatch, "defer", reply)
    synthetic_post = providers.post
    def persisted_post(**kwargs):
        from agent_runtime._http_worker import _persist
        body, receipt, window = synthetic_post(**kwargs)
        _persist(kwargs, 200, body, True, receipt, window)
        return body, receipt, window
    monkeypatch.setattr(providers, "post", persisted_post)
    # Synthetic transport is installed before dispatch. Ordinary tests do not inherit live authorization.
    monkeypatch.delenv("NF_RUNTIME_LIVE_APPROVED", raising=False)
    monkeypatch.setattr(providers.Profile, "preflight", lambda _: None)
    forbidden = lambda *args, **kwargs: pytest.fail("Synthetic experience test reached native transport")
    monkeypatch.setattr(native_codex, "generate", forbidden)
    monkeypatch.setattr(native_codex, "start_host", forbidden)
    monkeypatch.setattr(native_review, "generate", forbidden)
    monkeypatch.setattr(native_review, "start_host", forbidden)
    native = NativeReading(kernel, mode)
    gateway = AgentGateway(kernel, {"flash": flash, "native": native})
    fixed = dispatch(kernel, gateway, owner, wr, world, role="writer", kind="targeted_revision",
                     adapter="flash", assignment=METHODS["extent"][0] if mode == "unused_method" else METHODS["editorial"][0])
    other = kernel.put_artifact(owner, wr["work_id"], {"synthetic": "another revision"}, "TEXT")
    addressed = other if mode == "wrong_revision" else fixed["output_ref"]
    reviewed = dispatch(kernel, gateway, owner, wr, world, role="reviewer", kind="literary",
        adapter="flash" if mode == "deepseek_review" else "native",
        assignment=json.dumps({"candidate_ref": addressed, "candidate": reply({"context": {"role_id": "writer"}})}),
        refs=[fixed["output_ref"], other])
    with kernel.objects.lock(), kernel.db.uow() as repo:
        fix_audit = gateway._read_audit(repo, fixed["attempt_id"])["payload"]
        review_audit = gateway._read_audit(repo, reviewed["attempt_id"])["payload"]
        if mode == "same_principal":
            writer = repo.one("tasks", {"task_id": fix_audit["task_id"]})
            repo.change("tasks", {"task_id": review_audit["task_id"]}, {"principal_id": writer["principal_id"]})
        if mode == "missing_wire":
            from pathlib import Path

            from novel_factory.infrastructure.wire_receipts import destination
            Path(destination(kernel.objects, fixed["attempt_id"])).unlink()
        evidence = {**fix_audit}
        if mode == "wrong_generation":
            evidence["generation"] += 1
        if mode == "wrong_request":
            evidence["request_sha256"] = "f" * 64
        plan = reflect_repair(kernel, repo, owner, wr, fix_audit["task_id"], fixed["attempt_id"],
                              fixed["evidence_ref"], plan_repair("editorial", ["synthetic finding"], []), "SYNTHETIC")
        if mode == "wrong_plan_error":
            plan["error_ref"] = other
        call_scope = {"task_id": [fix_audit["task_id"], review_audit["task_id"]]}
        calls_before = repo.many("call_attempts", call_scope)
        budgets_before = repo.many("budgets", {"work_id": wr["work_id"]})
        verify_method(kernel, repo, owner, wr, plan, evidence, "targeted_revision", fixed["output_ref"],
                      reviewed["attempt_id"], reviewed["output_ref"])
        memories = repo.many("artifacts", {"work_id": wr["work_id"], "kind": "P6_EXPERIENCE_MEMORY"})
        if mode in {"valid", "deepseek_review"}:
            assert len(memories) == 1
            memory = memories[0]["payload"]
            assert memory["status"] == "VERIFIED" and memory["method"] == METHODS["editorial"][0]
            assert memory["validation_scope"] == "INDEPENDENT_REVIEW_OF_EXACT_REVISION"
            assert memory.get("fix_delivery_kind", "deepseek") == "deepseek"
            assert memory.get("review_delivery_kind", "deepseek") == ("deepseek" if mode == "deepseek_review" else "codex-cli")
            assert len(memory["review_wire_sha256"]) == len(memory["fix_wire_sha256"]) == 64
            frozen = kernel.artifact(repo, fix_audit["input_ref"], wr["work_id"])["payload"]
            scope = task_scope(wr, "writer", "targeted_revision", frozen["runtime_sha256"],
                               frozen["output_contract"], frozen["model_config_sha256"])
            assert recall(kernel, repo, wr, scope)["method"] == plan["method"]
            if mode == "valid":
                recorded = repo.many("artifacts", {"work_id": wr["work_id"]})
                selected = repo.many("selections", {"work_id": wr["work_id"]})
                verify_method(kernel, repo, owner, wr, plan, evidence, "targeted_revision", fixed["output_ref"],
                              reviewed["attempt_id"], reviewed["output_ref"])
                assert repo.many("artifacts", {"work_id": wr["work_id"]}) == recorded
                assert repo.many("selections", {"work_id": wr["work_id"]}) == selected
        else:
            assert memories == []
            reflections = repo.many("artifacts", {"work_id": wr["work_id"], "kind": "P6_REFLECTION_RECORD"})
            assert any(r["payload"].get("status") == "LOCAL_VALIDATION_ONLY" for r in reflections)
        assert repo.many("call_attempts", call_scope) == calls_before
        assert repo.many("budgets", {"work_id": wr["work_id"]}) == budgets_before
        assert all(call["state"] == "UNKNOWN" for call in calls_before)
        assert len(requests) == (2 if mode == "deepseek_review" else 1)


def test_flash_scene_revision_codex_reading_promotes_only_repaired_draft(kernel_env, monkeypatch):
    from uuid import uuid4

    from test_p6_rolling_production import answer, request
    from test_release import setup_batch

    from novel_factory.application import production_pipeline as boards
    from novel_factory.application.rolling_production import register_rolling, rolling_status
    from novel_factory.application.supervisor import HostJournal, RuntimeSupervisor
    from novel_factory.infrastructure.runtime_providers import RuntimeModelAdapter

    kernel = kernel_env["kernel"]
    fixture = setup_batch(kernel, count=1)
    owner, wr = fixture["principal"], fixture["world_ref"]
    writes = []
    def writing(packet):
        result = answer(packet)
        assert packet["context"]["role_id"] == "writer"
        if writes:
            result["text"] = "屋檐下落了几滴雨。"
        writes.append(result["text"])
        return result
    flash, requests = fake_transport(kernel, monkeypatch, "defer", writing)
    synthetic_post = providers.post
    def persisted_post(**kwargs):
        from agent_runtime._http_worker import _persist
        body, receipt, window = synthetic_post(**kwargs)
        _persist(kwargs, 200, body, True, receipt, window)
        return body, receipt, window
    monkeypatch.setattr(providers, "post", persisted_post)
    monkeypatch.delenv("NF_RUNTIME_LIVE_APPROVED", raising=False)
    monkeypatch.setattr(providers.Profile, "preflight", lambda _: None)
    forbidden = lambda *args, **kwargs: pytest.fail("Synthetic scene test reached native transport")
    monkeypatch.setattr(native_codex, "generate", forbidden)
    monkeypatch.setattr(native_codex, "start_host", forbidden)
    monkeypatch.setattr(native_review, "generate", forbidden)
    monkeypatch.setattr(native_review, "start_host", forbidden)
    native = NativeReading(kernel, "valid")
    def reading(packet):
        from novel_factory.domain.reconciliation import span_for
        result = answer(packet)
        if len(writes) == 1 and "人物所知" in packet["assignment"]:
            source = json.loads(packet["authorized_materials"][0]["content"])
            result["issues"] = [{"issue_id": str(uuid4()), "category": "CONTINUITY", "severity": "BLOCKER",
                "subject_ref": source["draft_ref"], "description": "Synthetic unsupported observation",
                "evidence_refs": [{"kind": "ARTIFACT", "artifact_ref": source["text_ref"],
                                   "span": span_for(source["text"], 0, len(source["text"]))}],
                "suggested_change": "Synthetic exact visible detail",
                "disposition": "OPEN", "issue_kind": "CHANGE_REQUEST"}]
        return result
    native.respond = reading
    native_route = RuntimeModelAdapter(providers.Profile("codex-cli", "gpt-6.1-sol", "SYNTHETIC_NO_CREDENTIAL", 1024,
        auth_mode="host-login", model_output_ceiling=1024), HostJournal(kernel))
    monkeypatch.setattr(native_route, "generate", native.generate)
    gateway = AgentGateway(kernel, {"flash": flash, "native": native_route},
                           material_reader=boards.production_material_reader(kernel))
    params = request(kernel, fixture, 1)
    params["profile"]["overrides"]["production_mode"] = "quality"
    params["routes"] = {"draft": "flash", "review": "native", "plan": "native"}
    with kernel.db.uow() as repo:
        kernel._receipt(repo, owner, wr, "P6_IDEA_PROVENANCE", "initial", {},
            {"creative_input": {"length": "short"}, "model_routes": {"creative": "flash", "review": "native"}})
    register_rolling(kernel, gateway, owner, **params)
    events = []
    for _ in range(48):
        event = RuntimeSupervisor(kernel, gateway).run_once(owner, wr["work_id"], wr["branch_id"])
        events.append((event["status"], event.get("reason")))
        cursor = rolling_status(kernel, owner, wr["work_id"], wr["branch_id"])
        if cursor["frozen_chapters"] or event["status"] == "IDLE":
            break
    assert len(writes) == len(requests) == 2, events
    assert cursor["frozen_chapters"] == 1
    with kernel.db.uow(write=False) as repo:
        memory = repo.one("artifacts", {"work_id": wr["work_id"], "kind": "P6_EXPERIENCE_MEMORY"})["payload"]
        board = boards._load(kernel, repo, wr["work_id"], wr["branch_id"])
        writer = next(node for node in board["nodes"].values() if node["stage"] == "draft")
        assert memory["actual_fix"] == writer["output_ref"] != writer["history"][0]["output_ref"]
        assert memory["review_delivery_kind"] == "codex-cli"
        assert memory["scope"]["task_kind"] == "targeted_revision"
        assert repo.one("call_attempts", {"attempt_id": memory["fix_attempt_id"]})["state"] == "UNKNOWN"
        assert repo.one("call_attempts", {"attempt_id": memory["review_attempt_id"]})["state"] == "UNKNOWN"
