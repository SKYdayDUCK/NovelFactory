"""A new task generation must never forget an earlier plan-node guard."""
from datetime import timedelta

import pytest

from novel_factory.application.planning import PlanningService
from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import load_fixture
from novel_factory.infrastructure.database import tables
from novel_factory.infrastructure.repository import KernelRepository
from novel_factory.orchestration.workflow import WorkflowService
from test_planning import make_plan, task_with_plan

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


def test_bound_plan_cannot_be_reclaimed_without_new_guards(kernel_env):
    kernel = kernel_env["kernel"]
    world = load_fixture(kernel)
    owner, wr = world["principal"], world["world_ref"]
    work, branch = wr["work_id"], wr["branch_id"]
    planning, wf = PlanningService(kernel), WorkflowService(kernel.tasks)
    plan, ref = make_plan(planning, world)
    planning.adopt_plan(owner, wr, ref, None, "initial")
    bound, upstream = (task_with_plan(kernel, world, ref) for _ in range(2))
    task_id, generation = bound["task_id"], bound["generation"]
    planning.bind_task(owner, task_id, generation, ref, [plan["nodes"][0]["node_id"]], "pin")
    def authorized(conn):
        kernel.authorize(KernelRepository(conn, tables), owner, work, "ADMIN", branch)
    opts = dict(work_id=work, branch_id=branch, authorize_in_tx=authorized)
    with pytest.raises(DomainError) as forced:
        wf.wait(task_id, generation, [upstream["task_id"]], mode="AND", worker_id=bound["lease_owner"], **opts)
    assert forced.value.code == "TASK_REBIND_REQUIRED"
    with kernel.db.uow() as repo:
        repo.change("tasks", {"task_id": task_id}, {"lease_expiry": repo.now() - timedelta(seconds=1)})
    assert wf.resume_project(**opts)["recovered"] == 1
    current = kernel.tasks.get(task_id)
    assert current["state"] == "STALE" and current["generation"] > generation
    # Older recovery could persist PAUSED or READY after issuing a generation
    # that lacks a PLAN_TASK selection. The original node guards still fence it.
    with kernel.db.uow() as repo:
        repo.change("tasks", {"task_id": task_id}, {"state": "PAUSED"})
    with pytest.raises(DomainError) as paused:
        kernel.tasks.resume(task_id, current["generation"])
    assert paused.value.code == "TASK_REBIND_REQUIRED"
    with kernel.db.uow() as repo:
        repo.change("tasks", {"task_id": task_id}, {"state": "READY"})
    assert kernel.claim_task(owner, "old-plan-worker", task_id=task_id) is None
    assert wf.resume_project(**opts)["counts"]["STALE"] == 1
    with kernel.db.uow() as repo:
        kernel.tasks.accept_in_transaction(repo.conn, upstream["task_id"], upstream["generation"])
        repo.change("tasks", {"task_id": task_id}, {"state": "BLOCKED"})
        epoch = repo.one("grants", {"grant_id": owner.grant_id})["epoch"]
        key = {"task_id": task_id, "generation": current["generation"]}
        repo.add("workflow_waits", {**key, "mode": "AND", "grant_epoch": epoch})
        repo.add("workflow_edges", {**key, "predecessor_id": upstream["task_id"]})
    assert wf.resume_project(**opts)["awakened"] == 0
    assert kernel.tasks.get(task_id)["state"] == "STALE"
    with kernel.db.uow(write=False) as repo:
        assert not repo.many("inbox", {"consumer": f"workflow:{task_id}:{current['generation']}"})
    replacement = task_with_plan(kernel, world, ref)
    assert planning.bind_task(owner, replacement["task_id"], replacement["generation"], ref,
                              [plan["nodes"][0]["node_id"]], "fresh")


@pytest.mark.parametrize("change_after_wake", [False, True], ids=["sleeping", "awaiting-claim"])
def test_suspended_generation_keeps_semantic_read_fence(kernel_env, change_after_wake):
    from novel_factory.fixtures import create_bound_task, point, run_command

    kernel = kernel_env["kernel"]
    world = load_fixture(kernel)
    owner, wr = world["principal"], world["world_ref"]
    task = create_bound_task(kernel, owner, wr, point(1), [world["ids"]["sword"]], ["lifecycle"])
    upstream = create_bound_task(kernel, owner, wr, point(1))
    wf = WorkflowService(kernel.tasks)

    def authorize(conn):
        kernel.authorize(KernelRepository(conn, tables), owner, wr["work_id"], "ADMIN", wr["branch_id"])

    scope = dict(work_id=wr["work_id"], branch_id=wr["branch_id"], authorize_in_tx=authorize)
    wf.wait(task["task_id"], task["generation"], [upstream["task_id"]], mode="AND",
            worker_id=task["worker_id"], **scope)
    with kernel.db.uow() as repo:
        kernel.tasks.accept_in_transaction(repo.conn, upstream["task_id"], upstream["generation"])
    if change_after_wake:
        assert wf.resume_project(**scope)["awakened"] == 1
    run_command(kernel, owner, wr, "DESTROY_ITEM", {"item_id": world["ids"]["sword"], "at": point(40)})
    assert wf.resume_project(**scope)["awakened"] == 0
    assert kernel.claim_task(owner, "late-worker", task_id=task["task_id"]) is None
    assert kernel.tasks.get(task["task_id"])["state"] == "STALE"
    with kernel.db.uow() as repo:
        assert not repo.many("call_attempts", {"task_id": task["task_id"]})
        assert len(repo.many("task_executions", {"task_id": task["task_id"]})) == 1


def test_ordinary_claim_uses_live_branch_set_before_queue_order(kernel_env):
    from uuid import uuid4

    from novel_factory.fixtures import create_bound_task, point

    kernel = kernel_env["kernel"]
    world = load_fixture(kernel)
    owner, wr = world["principal"], world["world_ref"]
    sibling = kernel.fork(owner, wr, "CANDIDATE", "branch-queue")
    exemplar = create_bound_task(kernel, owner, wr, point(1))
    with kernel.db.uow() as repo:
        template = repo.one("task_inputs", {"task_id": exemplar["task_id"], "generation": 0})["spec"]
    queued = []
    for world_ref in (sibling, wr):
        task_id = str(uuid4())
        kernel.create_task(owner, {**template, "task_id": task_id, "world_ref": world_ref}, f"queue:{task_id}")
        queued.append(task_id)
    with kernel.db.uow() as repo:
        repo.change("grants", {"grant_id": owner.grant_id}, {"branch_ids": [wr["branch_id"]]})
    selected = kernel.claim_task(owner, "scoped-worker")
    assert selected["task_id"] == queued[1] and selected["world_ref"] == wr
    assert kernel.tasks.get(queued[0])["state"] == "READY"
    assert kernel.claim_task(owner, "scoped-worker") is None
