"""The ordinary Harness must propagate dry_run without looking up a live secret."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_runtime import providers
from agent_runtime.protocol import DryRunSpec, ModelCallFailure, ModelRequest
from novel_factory.application.prompt_layout import role_messages
from novel_factory.contracts.registry import validate_contract
from novel_factory.infrastructure.runtime_providers import RuntimeModelAdapter


def request(rate=0.7, outcome="normal"):
    identity = {"schema_version": "2.1", "task_id": "11111111-1111-4111-8111-111111111111", "generation": 1,
                "context_bundle_id": "22222222-2222-4222-8222-222222222222", "role_id": "reviewer", "actor_id": None,
                "world_ref": {"work_id": "33333333-3333-4333-8333-333333333333", "branch_id": "44444444-4444-4444-8444-444444444444",
                              "world_revision_id": "55555555-5555-4555-8555-555555555555"}}
    subject = {"artifact_id": identity["task_id"], "artifact_revision_id": identity["context_bundle_id"], "sha256": "a" * 64}
    messages = role_messages("Return the assigned JSON review", {"context": identity, "dry_run_subject_ref": subject}, identity)
    return ModelRequest(messages, "a" * 64, 8192, "attempt-simulation", dry_run=True,
        simulation=DryRunSpec(42, rate, outcome, {"summary": "DRY_RUN", "issues": [], "counterinterpretations": [], "evidence_refs": []}))


@pytest.mark.parametrize("kind", ["deepseek", "codex-cli"])
@pytest.mark.parametrize("rate", [0, 1])
def test_normal_adapter_invocation_returns_typed_simulation_without_live_preflight(monkeypatch, kind, rate):
    def forbidden(*args, **kwargs):
        pytest.fail("Simulation attempted live credentials or upstream I/O")
    monkeypatch.setattr(providers.Profile, "preflight", forbidden)
    monkeypatch.setattr(providers, "post", forbidden)
    profile = providers.Profile(kind, "deepseek-flash" if kind == "deepseek" else "gpt-6.1-sol", "NO_REAL_CREDENTIAL", 8192,
        **({"auth_mode": "host-login", "model_output_ceiling": 8192} if kind == "codex-cli" else {}))
    phases, authorizations = [], []
    journal = SimpleNamespace(dry_run=True, authorize_send=lambda attempt: authorizations.append(attempt) or True,
                              record=lambda *values: phases.append(values))
    adapter = RuntimeModelAdapter(profile, journal)
    adapter.check_available(dry_run=True)
    response = adapter.generate(request(rate))
    assert response.dry_run and response.raw_response["execution_kind"] == "TEST_FIXTURE"
    assert response.raw_response["review_approved"] == bool(rate)
    envelope = json.loads(response.text)
    validate_contract("AgentModelResponse", envelope, "agent")
    validate_contract("AgentReviewOutput", envelope["payload"], "agent")
    assert bool(envelope["payload"]["issues"]) != bool(rate)
    assert authorizations == ["attempt-simulation"]
    assert [row[-1] for row in phases] == (["THREAD_STARTED", "TURN_STARTED", "COMPLETED"] if kind == "codex-cli" else [])
    assert adapter.generate(request(rate)) == response


def test_live_journal_cannot_accept_a_simulation_request():
    profile = providers.Profile("deepseek", "deepseek-flash", "NO_REAL_CREDENTIAL", 8192)
    with pytest.raises(ModelCallFailure, match="DRY_RUN_MODE_MISMATCH") as error:
        RuntimeModelAdapter(profile, SimpleNamespace()).generate(request())
    assert not error.value.sent


def test_dry_run_cannot_publish_a_live_wire_receipt():
    with pytest.raises(ModelCallFailure, match="DRY_RUN_LIVE_RECEIPT_FORBIDDEN"):
        providers.invoke(providers.Profile("deepseek", "deepseek-flash", "NO_REAL_CREDENTIAL", 8192), request(),
                         authorize=lambda _: True, record=lambda *args: None, receipt_target={"path": "must-never-exist"})


def test_live_request_cannot_carry_unapplied_simulation_options():
    with pytest.raises(ModelCallFailure, match="INVALID_MODEL_REQUEST"):
        providers.invoke(providers.Profile("deepseek", "deepseek-flash", "NO_REAL_CREDENTIAL", 8192),
                         replace(request(), dry_run=False), authorize=lambda _: True, record=lambda *args: None)


@pytest.mark.parametrize("outcome,sent", [("rate_limit", False), ("crash_before_send", False), ("timeout", True), ("crash_after_send", True)])
def test_harness_faults_distinguish_unsent_from_unknown(outcome, sent):
    with pytest.raises(ModelCallFailure) as error:
        providers.invoke(providers.Profile("deepseek", "deepseek-flash", "NO_REAL_CREDENTIAL", 8192), request(outcome=outcome),
                         authorize=lambda _: True, record=lambda *args: None)
    assert error.value.sent == sent
