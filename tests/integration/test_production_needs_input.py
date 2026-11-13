"""Returned NEEDS_INPUT stops the production handoff without replaying the paid call."""
import json
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal

import pytest
from test_p5_supervisor import backend
from test_p6_rolling_production import answer, connect, request
from test_release import setup_batch

from novel_factory.application import production_pipeline as boards
from novel_factory.application import rolling_production as rolling
from novel_factory.application.supervisor import RuntimeSupervisor

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


@pytest.mark.parametrize("corrupt_audit", [False, True])
def test_returned_input_requirement_is_bound_and_does_not_redispatch(kernel_env, monkeypatch, corrupt_audit):
    kernel = kernel_env["kernel"]
    fixture = setup_batch(kernel, count=1)
    wr, owner = fixture["world_ref"], fixture["principal"]
    with backend() as (adapter, calls, server):
        server.packet_candidate = answer
        generate = adapter.generate

        def needs_input(packet):
            response = generate(packet)
            envelope = json.loads(response.text)
            if envelope["role_id"] == "evidence_maintainer":
                envelope.update(status="NEEDS_INPUT", unresolved_questions=["Supply the authorized entity identifiers."])
            return replace(response, text=json.dumps(envelope), actual_amount=Decimal(0), currency="USD")

        adapter.generate = needs_input
        gateway = connect(kernel, adapter)
        supervisor = RuntimeSupervisor(kernel, gateway)
        rolling.register_rolling(kernel, gateway, owner, **request(kernel, fixture, 1))
        for _ in range(30):
            dispatched = supervisor.run_once(owner, wr["work_id"], wr["branch_id"])
            with kernel.db.uow(write=False) as repo:
                board = boards._load(kernel, repo, wr["work_id"], wr["branch_id"])
                nodes = [n for n in board["nodes"].values() if n["stage"] == "facts" and n.get("task_id")]
                task = nodes and repo.one("tasks", {"task_id": nodes[0]["task_id"]})
            if task and task["state"] == "BLOCKED" and task["current_result_id"]:
                break
        assert task["state"] == "BLOCKED" and task["current_result_id"]
        assert dispatched["status"] == "RESULT_STORED" and dispatched["result_status"] == "NEEDS_INPUT"
        with kernel.db.uow(write=False) as repo:
            old_calls = repo.many("call_attempts", {"task_id": task["task_id"]})
            old_budgets = {c["budget_id"]: repo.one("budgets", {"budget_id": c["budget_id"]}) for c in old_calls}
        if corrupt_audit:
            read_audit = gateway._read_audit

            def different_generation(repo, attempt):
                row = deepcopy(read_audit(repo, attempt))
                row["payload"]["generation"] += 1
                return row

            monkeypatch.setattr(gateway, "_read_audit", different_generation)
        sent = len(calls)
        result = supervisor.run_once(owner, wr["work_id"], wr["branch_id"])
        assert result["status"] == "PRODUCTION_HANDOFF" and result["state"] == "STOPPED"
        assert result["reason"] == ("INVALID_EVIDENCE" if corrupt_audit else "MODEL_INPUT_REQUIRED")
        assert len(calls) == sent
        with kernel.db.uow(write=False) as repo:
            board = boards._load(kernel, repo, wr["work_id"], wr["branch_id"])
            node = board["nodes"][result["node_id"]]
            if corrupt_audit:
                assert "input_requirement" not in node
            else:
                requirement = node["input_requirement"]
                assert requirement["task_id"] == task["task_id"] and requirement["generation"] == task["generation"]
                assert requirement["result_id"] == task["current_result_id"]
                assert requirement["attempt_id"] == old_calls[0]["attempt_id"]
                assert requirement["unresolved_question_count"] == 1
                assert kernel.artifact(repo, requirement["response_ref"], wr["work_id"], kind="AGENT_RESPONSE")
            assert repo.one("tasks", {"task_id": task["task_id"]}) == task
            assert repo.many("call_attempts", {"task_id": task["task_id"]}) == old_calls
            assert all(repo.one("budgets", {"budget_id": key}) == value for key, value in old_budgets.items())
        for _ in range(3):
            supervisor.run_once(owner, wr["work_id"], wr["branch_id"])
        # Independent handoffs can continue; this returned task must not replay.
        with kernel.db.uow(write=False) as repo:
            assert repo.one("tasks", {"task_id": task["task_id"]}) == task
            assert repo.many("call_attempts", {"task_id": task["task_id"]}) == old_calls
        identities = [json.loads(call["messages"][-1]["content"].split("\nIdentity: ", 1)[1].splitlines()[0])
                      for call in calls]
        assert sum(identity["task_id"] == task["task_id"] for identity in identities) == 1
