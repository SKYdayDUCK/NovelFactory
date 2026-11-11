"""Durable AND/OR barriers, replay, scope, cancellation, and lease fencing on PG17."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from novel_factory.domain.errors import DomainError
from novel_factory.infrastructure.recovery import fence_recovered_tasks
from novel_factory.orchestration.workflow import WorkflowService
from .conftest import start_task, task_spec

pytestmark = [pytest.mark.postgres]


def scope_args(scope):
    # This suite exercises trusted workflow mechanics. Public grant authorization
    # is exercised independently at the MCP / kernel entry point.
    return dict(work_id=scope["work_id"], branch_id=scope["branch_id"], authorize_in_tx=lambda conn: None)


def blocked(env, scope):
    spec = dict(task_spec(scope), state="BLOCKED", policy_version="policy-test")
    env["service"].create(spec, principal_id=scope["principal_id"], idempotency_key=spec["task_id"])
    return spec["task_id"]


def accept(env, task):
    with env["db"].uow() as repo:
        env["service"].accept_in_transaction(repo.conn, task["task_id"], task["generation"])


def test_durable_barrier_replay_and_manual_bypass(rel_env, rel_scope):
    wf = WorkflowService(rel_env["service"])
    opts = scope_args(rel_scope)
    first, second = (start_task(rel_env, rel_scope) for _ in range(2))
    target = blocked(rel_env, rel_scope)
    accept(rel_env, first)  # outbox existed before the wait was installed
    pred_ids = [first["task_id"], second["task_id"]]
    wf.wait(target, 0, pred_ids, mode="AND", **opts)
    wf.wait(target, 0, list(reversed(pred_ids)), mode="AND", **opts)
    with pytest.raises(DomainError, match="Wait specification changed"):
        wf.wait(target, 0, pred_ids, mode="OR", **opts)
    with pytest.raises(DomainError) as bypass:
        rel_env["service"].resume(target, 0)
    assert bypass.value.code == "WAIT_CONDITION_REQUIRED"
    assert wf.resume_project(**opts)["counts"]["WAITING"] == 1
    assert rel_env["service"].claim("early", task_id=target) is None
    accept(rel_env, second)
    # Other consumers may have already delivered the source message.
    box = rel_env["tables"]["outbox"]
    with rel_env["db"].uow() as repo:
        repo.conn.execute(box.update().where(box.c.aggregate_id.in_(pred_ids)).values(state="DELIVERED"))
    barrier = Barrier(2)
    def recover(_):
        barrier.wait(timeout=10)
        return WorkflowService(rel_env["service"]).resume_project(**opts)["awakened"]
    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(recover, range(2))) == [0, 1]
    assert wf.resume_project(**opts)["awakened"] == 0
    assert wf.status(**opts)["counts"]["READY"] == 1
    with rel_env["db"].uow(write=False) as repo:
        assert len(repo.many("inbox", {"consumer": f"workflow:{target}:0"})) == 2
        assert len(repo.many("outbox", {"dedupe_key": f"task:{target}:0:READY"})) == 1


def test_waiting_worker_releases_lease_and_requires_new_generation(rel_env, rel_scope):
    wf = WorkflowService(rel_env["service"])
    opts = scope_args(rel_scope)
    spec = dict(task_spec(rel_scope), policy_version="policy-test")
    task = rel_env["service"]
    task.create(spec, principal_id=rel_scope["principal_id"], idempotency_key=spec["task_id"])
    old = task.claim("actor-B", task_id=spec["task_id"])
    one, two = (start_task(rel_env, rel_scope) for _ in range(2))
    args = (old["task_id"], old["generation"], [one["task_id"], two["task_id"]])
    with pytest.raises(DomainError) as denied:
        wf.wait(*args, worker_id="other", mode="OR", **opts)
    assert denied.value.code == "LEASE_OWNER_MISMATCH"
    wf.wait(*args, worker_id="actor-B", mode="OR", **opts)
    row = task.get(spec["task_id"])
    assert row["state"] == "BLOCKED" and row["attempt_id"] is None and row["lease_owner"] is None
    accept(rel_env, two)
    assert wf.resume_project(**opts)["awakened"] == 1
    new = task.claim("actor-B-restarted", task_id=spec["task_id"])
    assert new["generation"] > old["generation"] and new["attempt_id"] != old["attempt_id"]
    with pytest.raises(DomainError) as stale:
        wf.wait(*args, worker_id="actor-B", mode="OR", **opts)
    assert stale.value.code == "STALE_GENERATION"


def test_cycle_scope_cancel_and_revoked_grant(rel_env, rel_scope):
    wf = WorkflowService(rel_env["service"])
    opts = scope_args(rel_scope)
    left, right = (blocked(rel_env, rel_scope) for _ in range(2))
    outside_branch = str(uuid4())
    with rel_env["db"].uow() as repo:
        repo.add("branches", {"branch_id": outside_branch, "work_id": rel_scope["work_id"], "kind": "SIMULATION"})
    other_task = blocked(rel_env, {**rel_scope, "branch_id": outside_branch})
    with pytest.raises(DomainError) as foreign:
        wf.wait(left, 0, [other_task], mode="OR", **opts)
    assert foreign.value.code == "VIEW_DENIED"
    with pytest.raises(DomainError) as self_cycle:
        wf.wait(left, 0, [left], mode="OR", **opts)
    assert self_cycle.value.code == "WORKFLOW_CYCLE"
    barrier = Barrier(2)
    def edit(ids):
        barrier.wait(timeout=10)
        try:
            wf.wait(ids[0], 0, [ids[1]], mode="AND", **opts)
            return "BLOCKED"
        except DomainError as exc:
            return exc.code
    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(edit, [(left,right), (right,left)])) == ["BLOCKED","WORKFLOW_CYCLE"]
    upstream = start_task(rel_env, rel_scope)
    target = blocked(rel_env, rel_scope)
    cancelled = blocked(rel_env, rel_scope)
    for waiter in (target, cancelled):
        wf.wait(waiter, 0, [upstream["task_id"]], mode="AND", **opts)
    rel_env["service"].cancel(cancelled, 0, principal_id=rel_scope["principal_id"], idempotency_key="stop")
    accept(rel_env, upstream)
    with rel_env["db"].uow() as repo:
        repo.change("grants", {"grant_id": rel_scope["grant_id"]}, {"active": False})
    assert wf.resume_project(**opts)["awakened"] == 0
    with rel_env["db"].uow() as repo:
        repo.change("grants", {"grant_id": rel_scope["grant_id"]}, {"active": True})
    assert wf.resume_project(**opts)["awakened"] == 1
    assert rel_env["service"].get(cancelled)["state"] == "CANCELLED"


def test_restore_fences_wait_without_losing_preconditions(rel_env, rel_scope):
    wf = WorkflowService(rel_env["service"])
    opts = scope_args(rel_scope)
    pred = start_task(rel_env, rel_scope)
    target = blocked(rel_env, rel_scope)
    wf.wait(target, 0, [pred["task_id"]], mode="AND", **opts)
    fence_recovered_tasks(rel_env["db"].engine, rel_env["store"])
    restored = rel_env["service"].get(target)
    assert restored["generation"] == 1 and restored["state"] == "BLOCKED"
    with pytest.raises(DomainError) as exc:
        rel_env["service"].resume(target, restored["generation"])
    assert exc.value.code == "WAIT_CONDITION_REQUIRED"
    assert wf.resume_project(**opts)["awakened"] == 0
    with rel_env["db"].uow(write=False) as repo:
        assert repo.one("workflow_edges", {"task_id": target, "generation": 1})["predecessor_id"] == pred["task_id"]


def test_recovery_respects_authorizer_and_policy_version(rel_env, rel_scope):
    wf = WorkflowService(rel_env["service"])
    opts = scope_args(rel_scope)
    pred = start_task(rel_env, rel_scope)
    target = blocked(rel_env, rel_scope)
    wf.wait(target, 0, [pred["task_id"]], mode="AND", **opts)
    accept(rel_env, pred)
    def deny(conn):
        raise DomainError("VIEW_DENIED", "No longer authorized")
    with pytest.raises(DomainError):
        wf.resume_project(**{**opts, "authorize_in_tx": deny})
    with rel_env["db"].uow() as repo:
        repo.change("works", {"work_id": rel_scope["work_id"]}, {"policy_version": "policy-next"})
    assert wf.resume_project(**opts)["awakened"] == 0
    calls = rel_env["tables"]["call_attempts"]
    with rel_env["db"].uow(write=False) as repo:
        assert repo.conn.scalar(select(func.count()).select_from(calls).where(calls.c.task_id == target)) == 0


def test_wait_admission_and_wake_recheck_role_grant(rel_env, rel_scope):
    from datetime import datetime, timedelta, timezone

    wf = WorkflowService(rel_env["service"])
    opts = scope_args(rel_scope)
    pred = start_task(rel_env, rel_scope)
    target = blocked(rel_env, rel_scope)
    args = (target, 0, [pred["task_id"]])
    # 0004 works alone. 0006 and later add optional independent grant fences.
    from sqlalchemy import inspect
    grant_cols = {c["name"] for c in inspect(rel_env["db"].engine).get_columns("nf_grants")}
    def change(**values):
        with rel_env["db"].uow() as repo:
            repo.change("grants", {"grant_id": rel_scope["grant_id"]}, values)
    change(active=False)
    with pytest.raises(DomainError) as revoked:
        wf.wait(*args, mode="AND", **opts)
    assert revoked.value.code == "VIEW_DENIED"
    change(active=True)
    if "expires_at" in grant_cols:
        change(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
        with pytest.raises(DomainError) as expired:
            wf.wait(*args, mode="AND", **opts)
        assert expired.value.code == "VIEW_DENIED"
        change(expires_at=None, bound_policy_version="another-policy")
        with pytest.raises(DomainError) as wrong_policy:
            wf.wait(*args, mode="AND", **opts)
        assert wrong_policy.value.code == "VIEW_DENIED"
        change(bound_policy_version="policy-test")
    wf.wait(*args, mode="AND", **opts)
    accept(rel_env, pred)
    if "expires_at" in grant_cols:
        change(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
        assert wf.resume_project(**opts)["awakened"] == 0
        change(expires_at=None)
    change(epoch=1)
    assert wf.resume_project(**opts)["awakened"] == 0
    assert rel_env["service"].get(target)["state"] == "BLOCKED"
