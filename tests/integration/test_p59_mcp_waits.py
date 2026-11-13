"""Bound SDK observations during a real provider wait and an independently decided review."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
from threading import Event, Timer
from time import perf_counter

import pytest
from sqlalchemy import text

from benchmarks.p59_mcp_workflow import ProjectControl
from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.planning import PlanningService
from novel_factory.application.supervisor import RuntimeSupervisor
from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import load_fixture, point, run_command
from novel_factory.infrastructure.database import tables
from novel_factory.infrastructure.repository import KernelRepository
from test_p5_supervisor import _job, backend
from test_planning import make_plan, task_with_plan
from test_s9_resolution import case_for

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


def save_trace(name, trace):
    folder = os.environ.get("NF_MCP_WAITS_EVIDENCE")
    if folder:
        source = Path(__file__).resolve()
        trace.update(git=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source.parent,
                                                 text=True).strip(),
                     test_sha256=sha256(source.read_bytes()).hexdigest(), commercial_calls=0)
        (Path(folder) / (name + ".json")).write_text(json.dumps(trace, indent=2) + "\n")


def test_mcp_reads_and_live_lease_while_provider_waits(kernel_env, tmp_path):
    kernel = kernel_env["kernel"]
    world = load_fixture(kernel)
    ref = world["world_ref"]
    work, branch = ref["work_id"], ref["branch_id"]
    budget_id = kernel.tasks.create_budget(work, "2")
    with backend() as (adapter, requests, server), ExitStack() as stack, ThreadPoolExecutor(1) as pool:
        gateway = AgentGateway(kernel, {"test": adapter})
        supervisor = RuntimeSupervisor(kernel, gateway)
        job = _job(kernel, gateway, supervisor, world, budget_id, "READY", accept=True)
        task_id = job.task["task_id"]
        control = ProjectControl(kernel, tmp_path, stack)
        control.connect(0, world)
        for _ in range(6):
            control.call(0, "before", "nf_project_status", world)
        server.started, server.release = Event(), Event()
        launched = perf_counter()
        executing = pool.submit(supervisor.run_once, world["principal"], work, branch)
        try:
            assert server.started.wait(8)
            still_waiting = perf_counter()
            dwell = Event()
            minimum_model_wait = Timer(3, dwell.set)
            minimum_model_wait.start()
            stack.callback(minimum_model_wait.cancel)
            checkouts = kernel.db.engine.pool.checkedout()
            assert checkouts == 0
            with kernel.db.uow(write=False) as repo:
                attempts = repo.many("call_attempts", {"task_id": task_id})
                row = repo.one("tasks", {"task_id": task_id})
                budget = repo.one("budgets", {"budget_id": budget_id})
                slots = repo.many("dispatch_slots", {"task_id": task_id})
            assert row["state"] == "RUNNING" and row["lease_owner"]
            assert len(attempts) == 1 and attempts[0]["state"] == "SENT"
            assert budget["reserved_amount"] > 0 and budget["spent_amount"] == 0
            assert len(slots) == 1
            for _ in range(6):
                status = control.call(0, "provider_waiting", "nf_project_status", world)
                assert status["counts"]["RUNNING"] == 1
            tick = perf_counter()
            expiry = kernel.tasks.renew(task_id, row["generation"], row["lease_owner"], lease_seconds=1800)
            renew_ms = (perf_counter() - tick) * 1000
            with kernel.db.uow(write=False) as repo:
                new = repo.one("tasks", {"task_id": task_id})
                slot = repo.one("dispatch_slots", {"task_id": task_id})
                assert new["lease_expiry"] == expiry == slot["expires_at"]
                assert new["generation"] == row["generation"] and new["lease_owner"] == row["lease_owner"]
            assert len(requests) == 1 and not executing.done()
            assert dwell.wait(6) and not executing.done()
        finally:
            released = perf_counter()
            server.release.set()
        receipt = executing.result(timeout=15)
        assert receipt["status"] == "RESULT_STORED"
        for _ in range(6):
            control.call(0, "after", "nf_project_status", world)
        with kernel.db.uow(write=False) as repo:
            final_attempts = repo.many("call_attempts", {"task_id": task_id})
            final_budget = repo.one("budgets", {"budget_id": budget_id})
            final_slots = repo.many("dispatch_slots", {"task_id": task_id})
            assert kernel.head(repo, work, branch) == ref
            pg = repo.conn.scalar(text("SHOW server_version"))
        # This HTTP fixture supplies token usage but omits price. The kernel must retain
        # the unknown-cost reservation after receiving valid creative output.
        assert len(requests) == len(final_attempts) == 1 and final_attempts[0]["state"] == "UNKNOWN"
        assert final_budget["reserved_amount"] == budget["reserved_amount"] and not final_slots
        kernel.tasks.settle_call(final_attempts[0]["attempt_id"], state="SUCCEEDED", actual_amount="0.01",
                                 actual_tokens=18, provider_receipt="synthetic-independent-cost-reconciliation")
        with kernel.db.uow(write=False) as repo:
            reconciled = repo.one("budgets", {"budget_id": budget_id})
            assert reconciled["reserved_amount"] == 0 and str(reconciled["spent_amount"]) == "0.010000"
            assert len(repo.many("call_attempts", {"task_id": task_id})) == 1
        save_trace("provider", {"postgres": pg, "protocols": control.protocols, "rows": control.rows,
                   "provider": "loopback synthetic HTTP; Event blocked after gateway send",
                   "sent_to_observed_s": still_waiting - launched, "held_after_observed_s": released - still_waiting,
                   "worker_checked_out_before_observer": checkouts, "slot_during": len(slots),
                   "slot_after": len(final_slots), "attempt_during": attempts[0]["state"],
                   "attempt_after": final_attempts[0]["state"], "attempt_count": len(final_attempts),
                   "reserved_during": str(budget["reserved_amount"]), "reserved_after": str(final_budget["reserved_amount"]),
                   "spent_before_reconciliation": str(final_budget["spent_amount"]),
                   "reserved_after_test_receipt": str(reconciled["reserved_amount"]),
                   "spent_after_test_receipt": str(reconciled["spent_amount"]), "lease_renewal_ms": renew_ms,
                   "renewal_before_provider_release": True, "head_unchanged": True})


def test_scoped_mcp_waits_for_explicit_independent_reviewer(kernel_env, tmp_path, monkeypatch):
    # One-node automatic-repair ceiling in this isolated scenario requires independent review.
    monkeypatch.setenv("NF_S9_MAX_WINDOW", "1")
    kernel = kernel_env["kernel"]
    world = load_fixture(kernel)
    base, owner = world["world_ref"], world["principal"]
    work, branch = base["work_id"], base["branch_id"]
    planning = PlanningService(kernel)
    plan, initial = make_plan(planning, world)
    planning.adopt_plan(owner, base, initial, None, "original")
    actor = task_with_plan(kernel, world, initial)
    planning.bind_task(owner, actor["task_id"], actor["generation"], initial,
                       [plan["nodes"][0]["node_id"]], "guard")
    change = run_command(kernel, owner, base, "DESTROY_ITEM", {"item_id": world["ids"]["sword"], "at": point(40)})
    current = change["world_ref"]
    svc, args, case = case_for(kernel, world, actor)
    review = svc.resolve(*args, case)
    assert review["outcome"] == "REVIEW_REQUIRED"
    ticket = review["review_task_id"]
    with backend() as (adapter, requests, _), ExitStack() as stack:
        gateway = AgentGateway(kernel, {"test": adapter})
        supervisor = RuntimeSupervisor(kernel, gateway)
        budget_id = kernel.tasks.create_budget(work, "2")
        follower = _job(kernel, gateway, supervisor, dict(world, world_ref=current), budget_id, "BLOCKED")
        target = follower.task["task_id"]

        def authorize(conn):
            kernel.authorize(KernelRepository(conn, tables), owner, work, "ADMIN", branch)

        supervisor.workflow.wait(target, 0, [ticket], mode="AND", work_id=work, branch_id=branch,
                                 authorize_in_tx=authorize)
        control = ProjectControl(kernel, tmp_path, stack)
        control.connect(0, world)
        status = control.call(0, "awaiting_review", "nf_project_status", world)
        assert status["pending_review_count"] >= 1
        assert control.call(0, "before_decision", "nf_resume_project", world)["awakened"] == 0
        with kernel.db.uow(write=False) as repo:
            pre_ticket = repo.one("tasks", {"task_id": ticket})
            pre_target = repo.one("tasks", {"task_id": target})
            assert pre_ticket["state"] == "WAITING_REVIEW" and pre_target["state"] == "BLOCKED"
            assert pre_target["lease_owner"] is None and not repo.many("dispatch_slots", {"task_id": target})
            assert not repo.many("call_attempts", {"task_id": target})
        evidence = {"case_id": case, "decision": "REPAIR_HISTORY", "world_ref": current,
                    "reason": "独立审核员复核历史依赖与当前版本"}
        own_ref = kernel.put_artifact(owner, work, evidence, "EVIDENCE")
        with pytest.raises(DomainError) as denied:
            svc.decide_review(owner, current, case, decision="REPAIR_HISTORY", evidence_ref=own_ref)
        assert denied.value.code == "VIEW_DENIED"
        reviewer = kernel.issue_grant(owner, work, role_id="control_operator", planes=["WORLD"],
                                     capabilities=["ADMIN", "APPROVE", "REPAIR", "ARTIFACT_WRITE"],
                                     max_story_time=point(60))["principal"]
        evidence_ref = kernel.put_artifact(reviewer, work, evidence, "EVIDENCE")
        accepted = svc.decide_review(reviewer, current, case, decision="REPAIR_HISTORY", evidence_ref=evidence_ref)
        assert accepted["outcome"] == "REVIEW_DECIDED"
        assert svc.decide_review(reviewer, current, case, decision="REPAIR_HISTORY", evidence_ref=evidence_ref) == accepted
        control.connect(0, world, reconnect=True)
        after = control.call(0, "first_resume", "nf_resume_project", world)
        assert after["awakened"] == 1
        assert control.call(0, "second_resume", "nf_resume_project", world)["awakened"] == 0
        with kernel.db.uow(write=False) as repo:
            wait = repo.one("workflow_waits", {"task_id": target, "generation": 0})
            assert repo.one("tasks", {"task_id": ticket})["state"] == "ACCEPTED"
            assert repo.one("tasks", {"task_id": target})["state"] == "READY"
            assert wait["woken_at"] is not None and not repo.many("call_attempts", {"task_id": target})
            assert accepted["candidate_world_ref"]["branch_id"] != branch
            assert kernel.head(repo, work, branch) == current
            pg = repo.conn.scalar(text("SHOW server_version"))
        assert requests == []
        save_trace("review", {"postgres": pg, "protocols": control.protocols, "rows": control.rows,
                   "review_contract": "semantic conflict; independent operator REPAIR_HISTORY",
                   "reviewer": "script exercised a separately granted operator; no person clicked",
                   "self_review": denied.value.code, "ticket": ["WAITING_REVIEW", "ACCEPTED"],
                   "follower": ["BLOCKED", "READY"], "wake_counts": [0, after["awakened"], 0],
                   "identical_decision_replay": True, "ready_after_sdk_reconnect": True,
                   "provider_calls": len(requests), "follower_lease_while_waiting": None,
                   "pending_review_count": status["pending_review_count"],
                   "pending_author_approval_count": status["pending_author_approval_count"],
                   "canonical_head_unchanged": True})
