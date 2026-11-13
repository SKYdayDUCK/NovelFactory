"""Native protocol simulation; these fixtures never call a real model."""

import json
from dataclasses import replace

import pytest
from test_agent_gateway import FixtureAdapter, setup_gateway

from novel_factory.application.candidate_delivery import CandidateDelivery
from novel_factory.application.supervisor import HostJournal
from novel_factory.domain.errors import DomainError

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


class NativeProtocolFixture(FixtureAdapter):
    def __init__(self, kernel, mode):
        super().__init__()
        self.journal, self.mode = HostJournal(kernel), mode

    @property
    def configuration(self):
        return {**super().configuration, "adapter_kind": "codex-cli", "auth_mode": "host-login",
                "provider": "codex-cli", "model": "native-protocol-fixture", "endpoint": None,
                "execution_kind": "REAL_HOST"}

    def generate(self, request):
        self.journal.authorize_send(request.attempt_id)
        thread, turn = "thread-" + request.attempt_id, "turn-" + request.attempt_id
        if self.mode != "missing_journal":
            self.journal.record(request.attempt_id, thread, None, "THREAD_STARTED")
            self.journal.record(request.attempt_id, thread, turn, "TURN_STARTED")
            if self.mode != "incomplete":
                self.journal.record(request.attempt_id, thread, turn, "COMPLETED")
        response = super().generate(request)
        return replace(response, actual_amount=None, currency=None, provider_receipt=turn,
                       input_tokens=None if self.mode == "missing_usage" else response.input_tokens,
                       raw_response={"codex_thread_id": thread,
                                     "codex_turn_id": "different" if self.mode == "wrong_turn" else turn,
                                     "model": "different" if self.mode == "wrong_model" else self.configuration["model"],
                                     "auth_mode": "host-login", "billing": "SUBSCRIPTION_AMOUNT_UNKNOWN"})


@pytest.mark.parametrize("mode", ["valid", "missing_journal", "incomplete", "wrong_turn", "wrong_model", "missing_usage"])
def test_native_production_delivery_needs_completed_bound_host_result(kernel_env, mode):
    kernel = kernel_env["kernel"]
    item = setup_gateway(kernel_env, NativeProtocolFixture(kernel, mode))
    item["request"]["call_key"] = "supervised-candidate"
    result = item["gateway"].execute(item["principal"], item["request"])
    assert result["status"] == "RESULT_STORED"
    with kernel.db.uow(write=False) as repo:
        task = repo.one("tasks", {"task_id": item["task"]["task_id"]})
        call_before = repo.one("call_attempts", {"attempt_id": result["attempt_id"]})
        budget_before = repo.one("budgets", {"budget_id": call_before["budget_id"]})
        service = CandidateDelivery(kernel, item["gateway"])
        if mode == "valid":
            evidence, payload = service.audited_candidate(repo, task, production_accounting=True)
            assert evidence["accounting_state"] == "UNKNOWN" and payload["text"]
        else:
            with pytest.raises(DomainError) as error:
                service.audited_candidate(repo, task, production_accounting=True)
            assert error.value.code == "CALL_RECONCILIATION_REQUIRED"
        assert repo.one("call_attempts", {"attempt_id": result["attempt_id"]}) == call_before
        assert repo.one("budgets", {"budget_id": call_before["budget_id"]}) == budget_before
        assert len(item["adapter"].calls) == 1


def test_missing_input_native_receipt_is_only_a_recovery_proof(kernel_env):
    from novel_factory.application.agent_gateway import AgentGateway
    from novel_factory.application.native_delivery import require_native_delivery
    from novel_factory.application.runtime_tasks import RuntimeTaskService
    from novel_factory.fixtures import load_fixture, point

    kernel = kernel_env["kernel"]
    world = load_fixture(kernel)

    class MissingInput(NativeProtocolFixture):
        def generate(self, request):
            response = super().generate(request)
            envelope = json.loads(response.text)
            envelope.update(status="NEEDS_INPUT", unresolved_questions=["Synthetic missing entity mapping"])
            return replace(response, text=json.dumps(envelope))

    adapter = MissingInput(kernel, "valid")
    gateway = AgentGateway(kernel, {"fixture": adapter})
    prepared = RuntimeTaskService(kernel, gateway).prepare(world["principal"], world["world_ref"],
        role_id="evidence_maintainer", task_kind="assertion_extract", story_time=point(1),
        subject_ids=[world["ids"]["sword"]], predicates=["owner"], assignment="Synthetic fact reading",
        adapter_id="fixture")
    result = gateway.execute(prepared.principal, {"schema_version": "2.1", "task_id": prepared.task["task_id"],
        "generation": prepared.task["generation"], "worker_id": prepared.worker_id,
        "input_ref": prepared.agent_input["input_ref"], "budget_id": kernel.tasks.create_budget(world["world_ref"]["work_id"], "1"),
        "call_key": "supervised-candidate", "reserve_amount": "0.1", "token_reservation": 100000})
    with kernel.db.uow(write=False) as repo:
        task = repo.one("tasks", {"task_id": prepared.task["task_id"]})
        call = repo.one("call_attempts", {"attempt_id": result["attempt_id"]})
        audit = gateway._read_audit(repo, call["attempt_id"])
        evidence = audit["payload"]
        args = kernel, repo, task, call, audit, kernel.artifact(repo, evidence["response_ref"], task["work_id"]), kernel.artifact(repo, evidence["output_ref"], task["work_id"])
        require_native_delivery(*args, needs_input_read=True)
        with pytest.raises(DomainError):
            require_native_delivery(*args)
        with pytest.raises(DomainError):
            CandidateDelivery(kernel, gateway).audited_candidate(repo, task, production_accounting=True)
        assert repo.one("call_attempts", {"attempt_id": call["attempt_id"]}) == call


@pytest.mark.parametrize("role,kind,permitted", [("reviewer", "literary", True), ("reviewer", "epistemic", True),
                                                ("writer", "scene_draft", False)])
def test_unresolved_review_proof_does_not_relax_default_delivery(kernel_env, role, kind, permitted):
    from novel_factory.application.agent_gateway import AgentGateway
    from novel_factory.application.native_delivery import require_native_delivery
    from novel_factory.application.runtime_tasks import RuntimeTaskService
    from novel_factory.fixtures import load_fixture, point

    kernel = kernel_env["kernel"]
    world = load_fixture(kernel)

    class UnresolvedReturn(NativeProtocolFixture):
        def generate(self, request):
            response = super().generate(request)
            envelope = json.loads(response.text)
            if envelope["role_id"] == "reviewer":
                envelope["payload"] = {"summary": "Synthetic completed reading", "issues": [],
                                       "counterinterpretations": [], "evidence_refs": []}
            envelope["unresolved_questions"] = ["Synthetic interpretation remains open in this chapter."]
            return replace(response, text=json.dumps(envelope))

    adapter = UnresolvedReturn(kernel, "valid")
    gateway = AgentGateway(kernel, {"fixture": adapter})
    prepared = RuntimeTaskService(kernel, gateway).prepare(world["principal"], world["world_ref"],
        role_id=role, task_kind=kind, story_time=point(1), subject_ids=[world["ids"]["sword"]],
        predicates=["owner"], assignment="Synthetic complete reading with interpretation question", adapter_id="fixture")
    result = gateway.execute(prepared.principal, {"schema_version": "2.1", "task_id": prepared.task["task_id"],
        "generation": prepared.task["generation"], "worker_id": prepared.worker_id,
        "input_ref": prepared.agent_input["input_ref"], "budget_id": kernel.tasks.create_budget(world["world_ref"]["work_id"], "1"),
        "call_key": "supervised-candidate", "reserve_amount": "0.1", "token_reservation": 100000})
    with kernel.db.uow(write=False) as repo:
        task = repo.one("tasks", {"task_id": prepared.task["task_id"]})
        assert task["state"] == "RESULT_STORED"
        call = repo.one("call_attempts", {"attempt_id": result["attempt_id"]})
        budget = repo.one("budgets", {"budget_id": call["budget_id"]})
        audit = gateway._read_audit(repo, call["attempt_id"])
        evidence = audit["payload"]
        args = kernel, repo, task, call, audit, kernel.artifact(repo, evidence["response_ref"], task["work_id"]), kernel.artifact(repo, evidence["output_ref"], task["work_id"])
        if permitted:
            require_native_delivery(*args, allow_unresolved_review=True)
        else:
            with pytest.raises(DomainError):
                require_native_delivery(*args, allow_unresolved_review=True)
        with pytest.raises(DomainError):
            require_native_delivery(*args)
        with pytest.raises(DomainError) as default_delivery:
            CandidateDelivery(kernel, gateway).audited_candidate(repo, task, production_accounting=True)
        assert default_delivery.value.code == "REVIEW_REQUIRED"
        assert repo.one("call_attempts", {"attempt_id": call["attempt_id"]}) == call
        assert repo.one("budgets", {"budget_id": call["budget_id"]}) == budget
