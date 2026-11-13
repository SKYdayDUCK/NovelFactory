"""Native protocol simulation; these fixtures never call a real model."""

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
