"""PG17 recovery keeps returned attempts and ledgers while enrolling a new reader."""
from copy import copy
from dataclasses import replace
from decimal import Decimal

import pytest
from sqlalchemy import text
from test_p5_supervisor import backend
from test_p6_rolling_production import answer, connect, request
from test_release import setup_batch

from novel_factory.application import production_pipeline as boards
from novel_factory.application import rolling_production as rolling
from novel_factory.application.reading_policy_recovery import (
    ReadingPolicyRecovery,
    replace_stopped_reading,
)
from novel_factory.application.supervisor import RuntimeSupervisor
from novel_factory.domain.errors import DomainError

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def test_policy_recovery_is_atomic_idempotent_and_preserves_old_call(kernel_env):
    kernel = kernel_env["kernel"]
    fixture = setup_batch(kernel, count=1)
    wr, owner = fixture["world_ref"], fixture["principal"]
    with backend() as (adapter, calls, server):
        server.packet_candidate = answer
        generate = adapter.generate
        adapter.generate = lambda packet: replace(generate(packet), actual_amount=Decimal(0), currency="USD")
        gateway = connect(kernel, adapter)
        alternate = copy(adapter)
        alternate.model = "new-independent-reader-fixture"
        gateway.adapters.update(creative=adapter, review=alternate)
        rolling.register_rolling(kernel, gateway, owner, **request(kernel, fixture, 1))
        for _ in range(30):
            RuntimeSupervisor(kernel, gateway).run_once(owner, wr["work_id"], wr["branch_id"])
            with kernel.db.uow(write=False) as repo:
                board = boards._load(kernel, repo, wr["work_id"], wr["branch_id"])
                nodes = [n for n in board["nodes"].values() if n["stage"] == "facts" and n.get("task_id")]
                task = nodes and repo.one("tasks", {"task_id": nodes[0]["task_id"]})
            if task and task["state"] == "RESULT_STORED":
                break
        assert task["state"] == "RESULT_STORED"
        # Inject a bounded strategy hold at the returned-reading handoff.
        with kernel.objects.lock(), kernel.db.uow() as repo:
            board = boards._load(kernel, repo, wr["work_id"], wr["branch_id"])
            node = next(n for n in board["nodes"].values() if n.get("task_id") == task["task_id"])
            node.update(state="STOPPED", reason="STRATEGY_REASSESSMENT_REQUIRED")
            node["fact_format_repairs"] = [{"diagnostic": {"keyword": "injected-strategy-hold"}}]
            ref = boards._save(kernel, repo, owner, board)
            old_calls = repo.many("call_attempts", {"task_id": task["task_id"]})
            budgets = {c["budget_id"]: repo.one("budgets", {"budget_id": c["budget_id"]}) for c in old_calls}
            count = repo.conn.scalar(text("SELECT count(*) FROM nf_call_attempts"))
        recovery = ReadingPolicyRecovery(wr["work_id"], wr["branch_id"], task["task_id"], task["generation"],
                                         ref["sha256"], "creative", "review", 100000)
        assert replace_stopped_reading(kernel, gateway, owner, recovery)["provider_calls_sent"] == 0
        with pytest.raises(DomainError) as stale:
            replace_stopped_reading(kernel, gateway, owner, replace(recovery, expected_board_sha256="0" * 64), apply=True)
        assert stale.value.code == "STALE_GENERATION"
        sent = len(calls)
        result = replace_stopped_reading(kernel, gateway, owner, recovery, apply=True)
        assert replace_stopped_reading(kernel, gateway, owner, recovery, apply=True) == result
        assert len(calls) == sent
        with kernel.db.uow(write=False) as repo:
            assert repo.many("call_attempts", {"task_id": task["task_id"]}) == old_calls
            assert all(repo.one("budgets", {"budget_id": k}) == v for k, v in budgets.items())
            assert repo.conn.scalar(text("SELECT count(*) FROM nf_call_attempts")) == count
            assert repo.one("tasks", {"task_id": task["task_id"]})["state"] == "CANCELLED"
            job = repo.one("supervisor_jobs", {"task_id": result["new_task_id"]})
            incoming = kernel.artifact(repo, job["config"]["dispatch"]["input_ref"], wr["work_id"], kind="AGENT_INPUT")["payload"]
            assert incoming["adapter_id"] == "review"
            current = boards._load(kernel, repo, wr["work_id"], wr["branch_id"])
            replacement = next(n for n in current["nodes"].values() if n.get("task_id") == result["new_task_id"])
            assert replacement["policy_recoveries"][-1]["prior_node"]["fact_format_repairs"]
            assert not replacement.get("fact_format_repairs")
