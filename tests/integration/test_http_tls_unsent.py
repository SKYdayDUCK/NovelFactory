"""A local CA race refunds only proven unsent HTTP calls; no network/model I/O."""
from decimal import Decimal

import pytest
from sqlalchemy import text
from test_agent_gateway import FixtureAdapter, setup_gateway
from test_p5_process_resume import _https
from test_p5_supervisor import _job, backend

from agent_runtime import providers
from agent_runtime.protocol import ModelCallFailure, ModelResponse
from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.queries import WorldQueryService
from novel_factory.application.supervisor import HostJournal, RuntimeSupervisor
from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import load_fixture
from novel_factory.infrastructure import model_gateway
from novel_factory.infrastructure.model_gateway import OpenAICompatibleAdapter
from novel_factory.infrastructure.runtime_providers import RuntimeModelAdapter

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def http_adapter(kernel, monkeypatch, route):
    endpoint = "https://127.0.0.1:1/fixture"
    if route == "generic":
        return OpenAICompatibleAdapter(endpoint=endpoint, model="fixture", api_key="synthetic")
    monkeypatch.setenv("NF_RUNTIME_LIVE_APPROVED", "1")
    monkeypatch.setenv("NF_RUNTIME_ALLOWED_MODELS", '{"openai-compatible":["fixture"]}')
    monkeypatch.setenv("NF_TEST_TLS_UNSENT", "synthetic")
    profile = providers.Profile("openai-compatible", "fixture", "NF_TEST_TLS_UNSENT", 1024, endpoint=endpoint)
    return RuntimeModelAdapter(profile, HostJournal(kernel))


def forbid_io(monkeypatch):
    forbidden = lambda *args, **kwargs: pytest.fail("TLS race reached the HTTP/model sending boundary")
    monkeypatch.setattr(model_gateway, "build_opener", forbidden)
    monkeypatch.setattr(providers, "post", forbidden)


def next_generation(state):
    kernel, principal = state["kernel"], state["principal"]
    task = kernel.claim_task(principal, state["request"]["worker_id"], task_id=state["task"]["task_id"], lease_seconds=600)
    spec = state["spec"]
    compiled = WorldQueryService(kernel).context_compile(principal, {
        "schema_version": "2.1", "task_id": task["task_id"], "generation": task["generation"],
        "recipe_id": "WRITER", "input_manifest_ref": spec["input_artifacts"][0],
        "read_contract": {"revision": {"mode": "FIXED", "world_ref": spec["world_ref"]},
                          "process_policy": "ALLOW_PARTIAL", "index_policy": "AUTHORITATIVE", "max_wait_ms": 0},
        "knowledge_cut": {"plane": "WORLD"}, "subject_time": {"kind": "POINT", "at": spec["story_time"]},
        "token_budget": 20000, "idempotency_key": "tls-recompile:" + str(task["generation"])})
    assert compiled["status"] == "READY"
    return {**state["request"], "generation": task["generation"]}


@pytest.mark.parametrize("route", ["generic", "private_worker"])
def test_ca_disappears_after_availability_check_refunds_once_and_bounds_new_generations(kernel_env, tmp_path, monkeypatch, route):
    kernel = kernel_env["kernel"]
    adapter = http_adapter(kernel, monkeypatch, route)
    state = setup_gateway(kernel_env, adapter)
    original = adapter.check_available
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    def available_then_removed():
        original()
        monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "missing-ca.pem"))
    monkeypatch.setattr(adapter, "check_available", available_then_removed)
    forbid_io(monkeypatch)
    request = state["request"]
    attempts = []
    with kernel.db.uow(write=False) as repo:
        baseline = repo.one("budgets", {"budget_id": request["budget_id"]})
    for index in range(3):
        monkeypatch.delenv("SSL_CERT_FILE", raising=False)
        result = state["gateway"].execute(state["principal"], request)
        assert result["status"] == "BLOCKED" and result["reason"] == "MODEL_TLS_CONFIGURATION_INVALID"
        assert result["cost_status"] == "NOT_SENT" and result["retry"]["refunded_now"]
        attempts.append(result["attempt_id"])
        with kernel.db.uow(write=False) as repo:
            call = repo.one("call_attempts", {"attempt_id": result["attempt_id"]})
            audit = kernel.artifact(repo, result["evidence_ref"], state["world"]["world_ref"]["work_id"])["payload"]
            assert call["state"] == "FAILED" and call["actual_amount"] == Decimal(0) and call["actual_tokens"] == 0
            assert audit["usage"]["cost_status"] == "NOT_SENT" and audit["response_ref"] is audit["output_ref"] is None
            budget = repo.one("budgets", {"budget_id": request["budget_id"]})
            assert budget == baseline
            assert repo.conn.scalar(text("SELECT EXISTS(SELECT FROM nf_runtime_unsent WHERE attempt_id=:id)"), {"id": call["attempt_id"]})
            assert not repo.one("host_turns", {"attempt_id": call["attempt_id"]})
        with pytest.raises(DomainError):
            state["gateway"].execute(state["principal"], request)
        if index < 2:
            assert result["retry"]["state"] == "READY"
            request = next_generation(state)
        else:
            assert result["retry"]["state"] == "PAUSED" and result["retry"]["reason"] == "UNSENT_RETRY_EXHAUSTED"
    assert len(set(attempts)) == 3


@pytest.mark.parametrize("mode", ["received", "sent", "already_unknown", "untrusted_adapter"])
def test_claimed_unsent_cannot_refund_returned_sent_unknown_or_untrusted_calls(kernel_env, monkeypatch, mode):
    kernel = kernel_env["kernel"]
    adapter = FixtureAdapter() if mode == "untrusted_adapter" else http_adapter(kernel, monkeypatch, "generic")
    state = setup_gateway(kernel_env, adapter)
    def failure(request):
        received = None
        if mode == "already_unknown":
            kernel.tasks.settle_call(request.attempt_id, state="UNKNOWN")
        if mode == "received":
            received = ModelResponse(text="Synthetic rejected bytes", provider_receipt="synthetic-received", input_tokens=1,
                output_tokens=1, request_sha256=request.request_sha256, raw_response={"synthetic": True})
        raise ModelCallFailure("MODEL_TLS_CONFIGURATION_INVALID", sent=mode == "sent", received=received)
    monkeypatch.setattr(adapter, "generate", failure)
    forbid_io(monkeypatch)
    request = state["request"]
    result = state["gateway"].execute(state["principal"], request)
    assert result["cost_status"] == "UNKNOWN_COST"
    with kernel.db.uow(write=False) as repo:
        calls = repo.many("call_attempts", {"task_id": state["task"]["task_id"]})
        assert len(calls) == 1 and calls[0]["state"] == "UNKNOWN" and calls[0]["actual_amount"] is None
        assert not repo.conn.scalar(text("SELECT EXISTS(SELECT FROM nf_runtime_unsent WHERE attempt_id=:id)"), {"id": calls[0]["attempt_id"]})
        budget = repo.one("budgets", {"budget_id": request["budget_id"]})
        assert budget["reserved_amount"] == Decimal(request["reserve_amount"])
        original = dict(calls[0])
    try:
        state["gateway"].execute(state["principal"], request)
    except DomainError:
        pass
    with kernel.db.uow(write=False) as repo:
        assert repo.one("call_attempts", {"attempt_id": original["attempt_id"]}) == original


def test_supervisor_preserves_new_ready_generation_then_resumes_with_restored_ca(kernel_env, tmp_path, monkeypatch):
    kernel = kernel_env["kernel"]
    world = load_fixture(kernel)
    wr, owner = world["world_ref"], world["principal"]
    with backend() as (_, requests, server):
        certificate = _https(server, tmp_path)
        monkeypatch.setenv("SSL_CERT_FILE", str(certificate))
        monkeypatch.setenv("NO_PROXY", "127.0.0.1")
        adapter = OpenAICompatibleAdapter(endpoint=f"https://127.0.0.1:{server.server_port}/fixture", model="fixture", api_key="synthetic")
        gateway = AgentGateway(kernel, {"test": adapter})
        supervisor = RuntimeSupervisor(kernel, gateway)
        budget = kernel.tasks.create_budget(wr["work_id"], "2")
        job = _job(kernel, gateway, supervisor, world, budget, "READY")
        available = adapter.check_available
        def checked_then_changed():
            available()
            monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "missing.pem"))
        monkeypatch.setattr(adapter, "check_available", checked_then_changed)
        result = supervisor.run_once(owner, wr["work_id"], wr["branch_id"])
        assert result["cost_status"] == "NOT_SENT" and result["retry"]["state"] == "READY" and requests == []
        with kernel.db.uow(write=False) as repo:
            ready = repo.one("tasks", {"task_id": job.task["task_id"]})
            assert ready["state"] == "READY" and ready["generation"] > result["generation"]
        monkeypatch.setattr(adapter, "check_available", available)
        monkeypatch.setenv("SSL_CERT_FILE", str(certificate))
        completed = RuntimeSupervisor(kernel, gateway).run_once(owner, wr["work_id"], wr["branch_id"])
        assert completed["status"] == "RESULT_STORED" and completed["generation"] > result["generation"], completed
        assert completed["task_id"] == result["task_id"] == job.task["task_id"] and len(requests) == 1
        with kernel.db.uow(write=False) as repo:
            calls = repo.many("call_attempts", {"task_id": job.task["task_id"]})
            assert len(calls) == 2 and sorted(row["state"] for row in calls) == ["FAILED", "UNKNOWN"]
            assert repo.one("tasks", {"task_id": job.task["task_id"]})["state"] == "RESULT_STORED"
            assert kernel.head(repo, wr["work_id"], wr["branch_id"]) == wr
            assert not repo.many("events", {"work_id": wr["work_id"]})
