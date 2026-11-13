"""Independent review recovery preserves unresolved returns and their native ledgers."""
import json

import pytest
from test_p6_rolling_production import answer, request
from test_release import setup_batch

from agent_runtime import native_codex, providers
from agent_runtime.protocol import ModelResponse
from novel_factory.application import production_pipeline as boards
from novel_factory.application import rolling_production as rolling
from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.authorization import Principal
from novel_factory.application.candidate_delivery import CandidateDelivery
from novel_factory.application.reading_policy_recovery import (
    ReadingPolicyRecovery,
    replace_stopped_reading,
)
from novel_factory.application.supervisor import HostJournal, RuntimeSupervisor
from novel_factory.domain.errors import DomainError
from novel_factory.infrastructure import runtime_providers
from novel_factory.infrastructure.runtime_providers import RuntimeModelAdapter

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def stopped_reviews(kernel, monkeypatch, *, kinds=("literary",), mode="valid"):
    """Simulate the full native protocol locally, without a CLI or provider request."""
    fixture = setup_batch(kernel, count=1)
    owner, wr = fixture["principal"], fixture["world_ref"]
    calls = []

    def invoke(profile, packet, *, authorize, record, receipt_target=None):
        assert profile.kind == "codex-cli"
        calls.append(packet.attempt_id)
        identity = json.loads(packet.messages[-1]["content"].split("\nIdentity: ", 1)[1].splitlines()[0])
        user = json.loads(packet.messages[1]["content"])
        user["context"] = identity
        with kernel.db.uow(write=False) as repo:
            task = repo.one("tasks", {"task_id": identity["task_id"]})
        target = task["role_id"] == "reviewer" and task["task_kind"] in kinds
        assert authorize(packet.attempt_id) is not False
        thread, turn = "synthetic-thread-" + packet.attempt_id, "synthetic-turn-" + packet.attempt_id
        record(packet.attempt_id, thread, None, "THREAD_STARTED")
        record(packet.attempt_id, thread, turn, "TURN_STARTED")
        if not target or mode != "incomplete":
            record(packet.attempt_id, thread, turn, "COMPLETED")
        envelope = {**identity, "status": "SUCCEEDED", "payload": answer(user),
                    "unresolved_questions": ["Synthetic: this chapter leaves the cause uncertain."] if target else []}
        return ModelResponse(text=json.dumps(envelope), request_sha256=packet.request_sha256,
            provider_receipt=turn, input_tokens=25, output_tokens=40, cached_input_tokens=0,
            actual_amount=None, currency=None,
            raw_response={"codex_thread_id": thread, "codex_turn_id": "different" if target and mode == "wrong_turn" else turn,
                          "model": profile.model, "auth_mode": "host-login", "billing": "SUBSCRIPTION_AMOUNT_UNKNOWN"})

    monkeypatch.setattr(runtime_providers, "invoke", invoke)
    monkeypatch.setattr(RuntimeModelAdapter, "generate", lambda adapter, packet: invoke(adapter.profile, packet,
        authorize=adapter.journal.authorize_send, record=adapter.journal.record))
    forbidden = lambda *args, **kwargs: pytest.fail("Synthetic test reached a live transport boundary")
    monkeypatch.setattr(native_codex, "generate", forbidden)
    monkeypatch.setattr(native_codex, "start_host", forbidden)
    monkeypatch.setattr(providers, "post", forbidden)
    monkeypatch.setattr(providers.Profile, "preflight", lambda _: None)
    profile = providers.Profile("codex-cli", "gpt-6.1-sol", "SYNTHETIC_NO_CREDENTIAL", 1024,
                                auth_mode="host-login", model_output_ceiling=1024)
    native = RuntimeModelAdapter(profile, HostJournal(kernel))
    creative = RuntimeModelAdapter(providers.Profile("deepseek", "deepseek-flash", "SYNTHETIC_NO_CREDENTIAL", 8192),
                                   HostJournal(kernel))
    gateway = AgentGateway(kernel, {"fixture": native, "review": native, "creative": creative},
                           material_reader=boards.production_material_reader(kernel))
    rolling.register_rolling(kernel, gateway, owner, **request(kernel, fixture, 1))
    supervisor = RuntimeSupervisor(kernel, gateway)
    for _ in range(45):
        supervisor.run_once(owner, wr["work_id"], wr["branch_id"])
        with kernel.db.uow(write=False) as repo:
            board = boards._load(kernel, repo, wr["work_id"], wr["branch_id"])
        selected = {("literary" if node["stage"] == "review" else "epistemic"): node
                    for node in board["nodes"].values()
                    if node["stage"] in {"review", "epistemic"} and node.get("reason") == "REVIEW_REQUIRED"}
        if set(selected) == set(kinds):
            break
    assert set(selected) == set(kinds)
    return gateway, owner, wr, selected, calls


def recovery_request(kernel, wr, node):
    with kernel.db.uow(write=False) as repo:
        task = repo.one("tasks", {"task_id": node["task_id"]})
        pointer = repo.one("selections", {"work_id": wr["work_id"], "selection_kind": boards._key(wr["branch_id"])})
    return ReadingPolicyRecovery(wr["work_id"], wr["branch_id"], task["task_id"], task["generation"],
                                 pointer["artifact_ref"]["sha256"], "creative", "review", 100000)


@pytest.mark.parametrize("kind,mode", [("literary", "valid"), ("epistemic", "valid"),
                                      ("literary", "incomplete"), ("literary", "wrong_turn")])
def test_new_independent_review_requires_complete_native_proof_and_keeps_old_result(kernel_env, monkeypatch, kind, mode):
    kernel = kernel_env["kernel"]
    gateway, owner, wr, nodes, calls = stopped_reviews(kernel, monkeypatch, kinds=(kind,), mode=mode)
    recovery = recovery_request(kernel, wr, nodes[kind])
    with kernel.db.uow(write=False) as repo:
        task = repo.one("tasks", {"task_id": recovery.task_id})
        old_calls = repo.many("call_attempts", {"task_id": recovery.task_id})
        old_budget = repo.one("budgets", {"budget_id": old_calls[0]["budget_id"]})
        results = repo.many("task_results", {"task_id": recovery.task_id})
        job = repo.one("supervisor_jobs", {"task_id": recovery.task_id})
        original_input = kernel.artifact(repo, job["config"]["dispatch"]["input_ref"], wr["work_id"])["payload"]
        with pytest.raises(DomainError) as default:
            CandidateDelivery(kernel, gateway).audited_candidate(repo, task, production_accounting=True)
        assert default.value.code == "REVIEW_REQUIRED"
    sent = len(calls)
    if mode != "valid":
        with pytest.raises(DomainError) as incomplete:
            replace_stopped_reading(kernel, gateway, owner, recovery, apply=True)
        assert incomplete.value.code == "CALL_RECONCILIATION_REQUIRED"
        with kernel.db.uow(write=False) as repo:
            assert repo.one("tasks", {"task_id": recovery.task_id}) == task
    else:
        assert replace_stopped_reading(kernel, gateway, owner, recovery)["provider_calls_sent"] == 0
        result = replace_stopped_reading(kernel, gateway, owner, recovery, apply=True)
        assert replace_stopped_reading(kernel, gateway, owner, recovery, apply=True) == result
        with kernel.db.uow(write=False) as repo:
            assert repo.one("tasks", {"task_id": recovery.task_id})["state"] == "CANCELLED"
            new_task = repo.one("tasks", {"task_id": result["new_task_id"]})
            assert new_task["state"] == "READY" and new_task["generation"] == 0
            assert new_task["principal_id"] != task["principal_id"] and new_task["grant_id"] != task["grant_id"]
            new_job = repo.one("supervisor_jobs", {"task_id": new_task["task_id"]})
            fresh_input = kernel.artifact(repo, new_job["config"]["dispatch"]["input_ref"], wr["work_id"])["payload"]
            assert all(fresh_input[key] == original_input[key] for key in
                       ("role_id", "task_kind", "material_refs", "output_contract", "runtime_sha256"))
            assert "协议层提醒" in fresh_input["assignment"]
            # The opt-in proof did not authorize adoption of the original review.
            with pytest.raises(DomainError) as default:
                CandidateDelivery(kernel, gateway).audited_candidate(repo, task, production_accounting=True)
            assert default.value.code == "REVIEW_REQUIRED"
    with kernel.db.uow(write=False) as repo:
        assert repo.many("call_attempts", {"task_id": recovery.task_id}) == old_calls
        assert repo.many("task_results", {"task_id": recovery.task_id}) == results
        assert repo.one("budgets", {"budget_id": old_budget["budget_id"]}) == old_budget
    assert len(calls) == sent


@pytest.mark.parametrize("claim_first", [False, True])
def test_two_stopped_readers_can_only_be_recovered_as_an_unsent_registered_batch(kernel_env, monkeypatch, claim_first):
    kernel = kernel_env["kernel"]
    gateway, owner, wr, nodes, calls = stopped_reviews(kernel, monkeypatch, kinds=("literary", "epistemic"))
    first_request = recovery_request(kernel, wr, nodes["literary"])
    first = replace_stopped_reading(kernel, gateway, owner, first_request, apply=True)
    with kernel.db.uow(write=False) as repo:
        first_task = repo.one("tasks", {"task_id": first["new_task_id"]})
        first_job = repo.one("supervisor_jobs", {"task_id": first["new_task_id"]})
        first_budget = repo.one("budgets", {"budget_id": first["new_budget_id"]})
    second_request = recovery_request(kernel, wr, nodes["epistemic"])
    sent = len(calls)
    if claim_first:
        actor = Principal(first_task["grant_id"], first_task["principal_id"])
        assert kernel.claim_task(actor, "synthetic-batch-claimer", task_id=first_task["task_id"], lease_seconds=600)
        with pytest.raises(DomainError) as active:
            replace_stopped_reading(kernel, gateway, owner, second_request, apply=True)
        assert active.value.code == "TASK_REBIND_REQUIRED"
    else:
        second = replace_stopped_reading(kernel, gateway, owner, second_request, apply=True)
        assert second["old_budget_id"] == first["old_budget_id"]
        assert second["prior_phase_budget_id"] == first["new_budget_id"]
        assert second["new_budget_id"] != first["new_budget_id"]
        with kernel.db.uow(write=False) as repo:
            assert repo.one("tasks", {"task_id": first["new_task_id"]}) == first_task
            assert repo.one("supervisor_jobs", {"task_id": first["new_task_id"]}) == first_job
            assert repo.one("budgets", {"budget_id": first["new_budget_id"]}) == first_budget
            assert not repo.many("call_attempts", {"task_id": second["new_task_id"]})
        assert replace_stopped_reading(kernel, gateway, owner, first_request, apply=True) == first
    assert len(calls) == sent
