"""S9 uses PostgreSQL for repair decisions; Redis only delivers SQL-backed hints."""
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import text

from novel_factory.application.conflict_resolution import ConflictResolutionService
from novel_factory.application.planning import PlanningService
from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import create_bound_task, load_fixture, point, run_command
from test_planning import make_plan, task_with_plan

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def changed_reader(kernel, *, reserve=False):
    f = load_fixture(kernel)
    wr, user = f["world_ref"], f["principal"]
    task = create_bound_task(kernel, user, wr, point(1), [f["ids"]["sword"]], ["lifecycle"])
    call = None
    if reserve:
        budget = kernel.tasks.create_budget(wr["work_id"], "1")
        call = kernel.tasks.reserve_call(task["task_id"], task["generation"], task["worker_id"],
                    budget_id=budget, amount="0.25", token_amount=100, call_key="once", request_hash="a" * 64)
        assert kernel.tasks.mark_call_sent(call["attempt_id"], worker_id=task["worker_id"])
    change = run_command(kernel, user, wr, "DESTROY_ITEM", {"item_id": f["ids"]["sword"], "at": point(40)})
    return f, task, change, call


def case_for(k, f, task):
    svc = ConflictResolutionService(k)
    wr, controller = f["world_ref"], f["principal"]
    args = (controller, wr["work_id"], wr["branch_id"])
    while svc.scan_one(*args) is not None:
        pass
    with k.db.uow(write=False) as repo:
        case = str(repo.conn.execute(text("SELECT case_id FROM nf_conflict_cases WHERE task_id=:task"),
                                     {"task": task["task_id"]}).scalar_one())
    return svc, args, case


def test_recompute_preserves_frozen_input_and_concurrent_replay(kernel_env):
    k = kernel_env["kernel"]
    f, task, change, _ = changed_reader(k)
    svc, args, case = case_for(k, f, task)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: svc.resolve(*args, case), range(2)))
    assert results[0] == results[1]
    result = results[0]
    assert result["outcome"] == "RECOMPUTE_QUEUED" and result["additional_model_attempts"] == 0
    with k.db.uow() as repo:
        assert k.head(repo, args[1], args[2]) == change["world_ref"]
        assert repo.one("task_inputs", {"task_id": task["task_id"], "generation": task["generation"]})["spec"]["world_ref"] == f["world_ref"]
    reissued = k.claim_task(args[0], "new-execution", task_id=task["task_id"])
    assert reissued["world_ref"] == change["world_ref"]
    assert reissued["generation"] > task["generation"] and reissued["context_bundle_id"] is None
    assert svc.resolve_one(*args) is None


def test_unknown_call_paused_and_original_reservation_survives(kernel_env):
    k = kernel_env["kernel"]
    f, task, _, call = changed_reader(k, reserve=True)
    svc, args, case = case_for(k, f, task)
    assert svc.resolve(*args, case)["outcome"] == "CALL_RECONCILIATION_REQUIRED"
    with k.db.uow() as repo:
        row = repo.one("call_attempts", {"attempt_id": call["attempt_id"]})
        assert row["state"] == "UNKNOWN"
        assert str(repo.one("budgets", {"budget_id": row["budget_id"]})["reserved_amount"]) == "0.250000"
        assert len(repo.many("call_attempts", {"task_id": task["task_id"]})) == 1
    with pytest.raises(DomainError) as blocked:
        k.tasks.resume(task["task_id"], task["generation"])
    assert blocked.value.code == "CALL_RECONCILIATION_REQUIRED"
    rogue = k.issue_grant(args[0], args[1], role_id="character_actor", actor_id=f["ids"]["friend"],
                          capabilities=["READ"], planes=["BELIEF"])["principal"]
    with pytest.raises(DomainError) as denied:
        svc.resolve(rogue, *args[1:], case)
    assert denied.value.code == "VIEW_DENIED"


def test_automatically_prepares_bounded_future_candidate(kernel_env):
    k = kernel_env["kernel"]
    f = load_fixture(k)
    wr, user = f["world_ref"], f["principal"]
    plans = PlanningService(k)
    plan, plan_ref = make_plan(plans, f)
    plans.adopt_plan(user, wr, plan_ref, None, "first")
    task = task_with_plan(k, f, plan_ref)
    plans.bind_task(user, task["task_id"], task["generation"], plan_ref, [plan["nodes"][1]["node_id"]], "bind")
    change = run_command(k, user, wr, "DESTROY_ITEM", {"item_id": f["ids"]["sword"], "at": point(40)})
    svc, args, case = case_for(k, f, task)
    result = svc.resolve(*args, case)
    assert result["outcome"] == "REPLAN_PROPOSED"
    revised = plans.get_plan(user, wr["work_id"], result["plan_ref"])
    assert revised["input_world_ref"] == change["world_ref"]
    assert revised["nodes"][2] == plan["nodes"][2]
    assert all(n["status"] == "BLOCKED" for n in revised["nodes"][:2])
    with k.db.uow() as repo:
        selection = repo.one("selections", {"work_id": wr["work_id"], "selection_kind": "PLAN:" + plan["volume_id"]})
        assert selection["artifact_ref"] == plan_ref and k.head(repo, args[1], args[2]) == change["world_ref"]
        review = repo.one("task_inputs", {"task_id": result["review_task_id"], "generation": 0})["spec"]
        assert review["budget"]["token_budget"] == 0
