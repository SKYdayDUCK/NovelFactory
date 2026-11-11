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

    assert svc.reconcile_one(*args) is None
    k.tasks.settle_call(call["attempt_id"], state="SUCCEEDED", actual_amount="0.04",
                        actual_tokens=12, provider_receipt="synthetic-settled-once")
    result = svc.reconcile_one(*args)
    assert result["outcome"] == "REVIEW_REQUIRED" and result["reason"] == "ORIGINAL_ATTEMPT_RECONCILED"
    assert svc.reconcile_one(*args) is None
    from novel_factory.application.audit_log import LogService
    recent = next(c for c in LogService.recent_conflicts(k, *args) if str(c["case_id"]) == case)
    assert str(recent["review_task_id"]) == result["review_task_id"] and recent["review_task_state"] == "WAITING_REVIEW"
    with k.db.uow() as repo:
        assert len(repo.many("call_attempts", {"task_id": task["task_id"]})) == 1
        assert repo.one("tasks", {"task_id": result["review_task_id"]})["state"] == "WAITING_REVIEW"


@pytest.mark.parametrize("window_limit", [1, 2, 8])
def test_automatically_prepares_bounded_future_candidate(kernel_env, monkeypatch, window_limit):
    monkeypatch.setenv("NF_S9_MAX_WINDOW", str(window_limit))
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
    if window_limit == 1:
        assert (result["outcome"], result["reason"]) == ("REVIEW_REQUIRED", "WINDOW_CONFLICT")
        with k.db.uow() as repo:
            ticket = repo.one("task_inputs", {"task_id": result["review_task_id"], "generation": 0})["spec"]
            scope = k.artifact(repo, ticket["input_artifacts"][0], wr["work_id"])["payload"]
            assert scope["maximum_future_nodes"] == 1
            assert repo.one("selections", {"work_id": wr["work_id"], "selection_kind": "PLAN:" + plan["volume_id"]})["artifact_ref"] == plan_ref
        return
    assert result["outcome"] == "REPLAN_PROPOSED"
    revised = plans.get_plan(user, wr["work_id"], result["plan_ref"])
    assert revised["input_world_ref"] == change["world_ref"]
    assert revised["nodes"][2] == plan["nodes"][2]
    assert all(n["status"] == "BLOCKED" for n in revised["nodes"][:2])
    with k.db.uow() as repo:
        selection = repo.one("selections", {"work_id": wr["work_id"], "selection_kind": "PLAN:" + plan["volume_id"]})
        assert selection["artifact_ref"] == plan_ref and k.head(repo, args[1], args[2]) == change["world_ref"]
        review = repo.one("task_inputs", {"task_id": result["review_task_id"], "generation": 0})["spec"]
        assert review["budget"]["max_amount"] == "0"
        assert repo.one("tasks", {"task_id": result["review_task_id"]})["state"] == "WAITING_REVIEW"

    from novel_factory.application.statistics import StatisticsService
    stats = StatisticsService(k)
    assert "conflict.replans_applied" not in stats.rebuild(*args)["metrics"]
    adopted = plans.adopt_plan(user, change["world_ref"], result["plan_ref"], plan_ref, "adopt-window")
    assert plans.adopt_plan(user, change["world_ref"], result["plan_ref"], plan_ref, "adopt-window") == adopted
    metrics = stats.rebuild(*args)["metrics"]
    assert metrics["conflict.replan_proposals"] == metrics["conflict.replans_applied"] == 1


def test_manual_branch_or_repair_requires_independent_immutable_decision(kernel_env):
    k = kernel_env["kernel"]
    f = load_fixture(k)
    wr, user = f["world_ref"], f["principal"]
    plans = PlanningService(k)
    plan, initial = make_plan(plans, f)
    plans.adopt_plan(user, wr, initial, None, "original")
    task = task_with_plan(k, f, initial)
    plans.bind_task(user, task["task_id"], task["generation"], initial, [plan["nodes"][0]["node_id"]], "guard")
    change = run_command(k, user, wr, "DESTROY_ITEM", {"item_id": f["ids"]["sword"], "at": point(40)})
    svc, args, case = case_for(k, f, task)
    receipt = svc.resolve(*args, case)
    current = change["world_ref"]
    evidence = {"case_id": case, "decision": "REPAIR_HISTORY", "world_ref": current, "reason": "独立复核历史引用"}
    ref = k.put_artifact(user, wr["work_id"], evidence, "EVIDENCE")
    with pytest.raises(DomainError) as own:
        svc.decide_review(user, current, case, decision="REPAIR_HISTORY", evidence_ref=ref)
    assert own.value.code == "VIEW_DENIED"
    reviewer = k.issue_grant(user, wr["work_id"], role_id="control_operator",
        capabilities=["ADMIN", "APPROVE", "REPAIR", "ARTIFACT_WRITE"], planes=["WORLD"],
        max_story_time=point(60))["principal"]
    evidence_ref = k.put_artifact(reviewer, wr["work_id"], evidence, "EVIDENCE")
    result = svc.decide_review(reviewer, current, case, decision="REPAIR_HISTORY", evidence_ref=evidence_ref)
    assert svc.decide_review(reviewer, current, case, decision="REPAIR_HISTORY", evidence_ref=evidence_ref) == result
    assert result["candidate_world_ref"]["branch_id"] != current["branch_id"]
    with k.db.uow() as repo:
        branch = repo.one("branches", {"branch_id": result["candidate_world_ref"]["branch_id"]})
        assert branch["kind"] == "REPAIR" and branch["fork_ref"] == current
        assert k.head(repo, wr["work_id"]) == current
        assert repo.one("tasks", {"task_id": receipt["review_task_id"]})["state"] == "ACCEPTED"


def test_disjoint_future_patch_survives_cas_and_overlapping_patch_is_fenced(kernel_env):
    from copy import deepcopy
    from uuid import uuid4
    k = kernel_env["kernel"]
    f = load_fixture(k)
    wr, user = f["world_ref"], f["principal"]
    svc = PlanningService(k)
    plan, original = make_plan(svc, f)
    svc.adopt_plan(user, wr, original, None, "base")
    def candidate(node, goal):
        draft = deepcopy(plan)
        draft.update(prior_plan_ref=original, plan_revision_id=str(uuid4()))
        draft["nodes"][node]["goal"] = goal
        return svc.save_plan(user, draft, goal)
    left, right, overlapping = [candidate(n, label) for n, label in [(0, "left"), (2, "right"), (1, "dependent")]]
    svc.adopt_plan(user, wr, left, original, "left")
    with pytest.raises(DomainError) as conflict:
        svc.adopt_plan(user, wr, overlapping, original, "overlap", preserve_independent=True)
    assert conflict.value.code == "PLAN_SELECTION_CONFLICT"
    with pytest.raises(DomainError) as missing:
        svc.adopt_plan(user, wr, right, original, "right", preserve_independent=True)
    assert missing.value.code == "REVIEW_REQUIRED"
    with pytest.raises(DomainError) as self_review:
        svc.review_independent_windows(user, wr, right, left, "两条独立场景线", "self")
    assert self_review.value.code == "VIEW_DENIED"
    reviewer = k.issue_grant(user, wr["work_id"], role_id="chief_editor",
        capabilities=["READ","PLAN_READ","ARTIFACT_WRITE","APPROVE"], planes=["PLAN"],
        max_story_time=point(60))["principal"]
    review = svc.review_independent_windows(reviewer, wr, right, left, "两场互不引用，且人物与资源分离。", "independent")
    result = svc.adopt_plan(user, wr, right, original, "right", preserve_independent=True, independence_review_ref=review)
    assert result["composed_from"] == {"candidate_ref": right, "base_plan_ref": original, "selected_parent_ref": left}
    composed = svc.get_plan(user, wr["work_id"], result["plan_ref"])
    assert composed["nodes"][0]["goal"] == "left" and composed["nodes"][2]["goal"] == "right"
    assert svc.get_plan(user, wr["work_id"], right)["nodes"][0] == plan["nodes"][0]
    assert svc.adopt_plan(user, wr, right, original, "right", preserve_independent=True, independence_review_ref=review) == result


def test_reproven_read_issues_new_kernel_certificate_for_original_candidate(kernel_env):
    from novel_factory.fixtures import prepare_command
    k = kernel_env["kernel"]
    f = load_fixture(k)
    wr, user, ids = f["world_ref"], f["principal"], f["ids"]
    wr = run_command(k, user, wr, "ADVANCE_SCOPE", {"scope_id": ids["guard1"], "target": point(26)})["world_ref"]
    t = create_bound_task(k, user, wr, point(25), [ids["guard1"]], ["on_duty"])
    # Same Boolean receives a new relation version in a separate workflow.
    candidate = prepare_command(k, user, wr, "TRANSFER_ITEM", {"item_id": ids["sword2"], "from_id": ids["smith2"],
        "to_id": ids["friend"], "transfer_kind": "GIFT", "at": point(25)}, mode="DEPENDENCY_GUARDED", task=t)
    change = run_command(k, user, wr, "SET_RELATION", {"subject_id": ids["guard1"], "predicate": "on_duty",
        "object_value": {"kind": "BOOLEAN", "value": True}, "at": point(30)})
    svc, args, case = case_for(k, f, t)
    receipt = svc.resolve(*args, case)
    assert receipt["outcome"] == "CERTIFICATE_REISSUED"
    assert receipt["retained_artifact_refs"] == [candidate["proposal"]["proposal_ref"]]
    assert receipt["certificate_ref"] != candidate["request"]["acceptance_certificate_ref"]
    with k.db.uow() as repo:
        proof = k.artifact(repo, receipt["certificate_ref"], wr["work_id"])["payload"]
        assert proof["base_world_ref"] == wr and proof["validated_world_ref"] == change["world_ref"]
    accepted = k.accept(user, {**candidate["request"], "acceptance_certificate_ref": receipt["certificate_ref"]})
    assert accepted["world_ref"] != wr and len(accepted["accepted_event_refs"]) == 1


def test_branch_comparison_preserves_both_existing_selections(kernel_env):
    from test_s9_dependencies import query
    k = kernel_env["kernel"]
    f = load_fixture(k)
    wr, user, ids = f["world_ref"], f["principal"], f["ids"]
    branch = k.fork(user, wr, "CANDIDATE", "candidate")
    task = create_bound_task(k, user, wr, point(1), [ids["sword"]], ["lifecycle"])
    query(k, f, branch, task, ids["sword"], "lifecycle", 1)
    svc, args, case = case_for(k, f, task)
    result = svc.resolve(*args, case)
    assert result["outcome"] == "CANDIDATE_PRESERVED" and result["compared_world_refs"] == [branch, wr]
    with k.db.uow() as repo:
        assert len(repo.many("branches", {"work_id": wr["work_id"]})) == 2
        assert k.head(repo, wr["work_id"]) == wr


def test_unique_item_competition_refers_one_sealed_group_without_granting_a_winner(kernel_env, monkeypatch):
    from novel_factory.domain.canonical import fingerprint
    from novel_factory.fixtures import prepare_command
    from test_simulation import process_definition, register
    k = kernel_env["kernel"]
    f = load_fixture(k)
    ids, user = f["ids"], f["principal"]
    process = process_definition(k, f, scopes=[ids["sword"]])
    f, _ = register(k, f, process)
    wr = f["world_ref"]
    gift = {"item_id": ids["sword"], "from_id": ids["smith"], "to_id": ids["protagonist"],
            "transfer_kind": "GIFT", "at": point(12)}
    earlier = prepare_command(k, user, wr, "TRANSFER_ITEM", gift, mode="DEPENDENCY_GUARDED")
    change = run_command(k, user, wr, "TRANSFER_ITEM", {**gift, "at": point(9), "to_id": ids["friend"]})
    svc, args, case = case_for(k, f, earlier["task"])
    ticket = svc.resolve(*args, case)
    assert ticket["outcome"] == "JOINT_DECISION_REQUIRED"
    current = change["world_ref"]
    grants = {actor: k.issue_grant(user, wr["work_id"], role_id="character_actor", actor_id=actor,
         capabilities=["READ"], planes=["BELIEF"])["principal"].grant_id for actor in process["required_actor_ids"]}
    interaction = {"at": process["due_at"], "scope_ids": process["scope_ids"], "participant_grants": grants}
    reviewer = k.issue_grant(user, wr["work_id"], role_id="control_operator",
        capabilities=["ADMIN", "READ", "APPROVE", "PROPOSE", "ARTIFACT_WRITE"], planes=["WORLD"],
        max_story_time=point(80))["principal"]
    evidence = k.put_artifact(reviewer, wr["work_id"], {"case_id": case, "world_ref": current,
        "decision": "JOINT_ADJUDICATION", "reason": "后续处置由参与人共同裁决",
        "interaction_sha256": fingerprint(interaction)}, "EVIDENCE")
    with monkeypatch.context() as stricter:
        stricter.setenv("NF_S9_MAX_IMPACT", str(len(grants) - 1))
        with pytest.raises(DomainError) as above_limit:
            ConflictResolutionService(k).decide_review(reviewer, current, case, decision="JOINT_ADJUDICATION",
                                                       evidence_ref=evidence, interaction=interaction)
        assert above_limit.value.code == "INVALID_ARGUMENT"
    decision = svc.decide_review(reviewer, current, case, decision="JOINT_ADJUDICATION",
                                 evidence_ref=evidence, interaction=interaction)
    bundle = decision["joint"]
    assert bundle["base_world_ref"] == current and set(bundle["required_actor_ids"]) == set(grants)
    with k.db.uow() as repo:
        assert repo.one("simulation_outcomes", {"interaction_id": bundle["interaction_id"]}) is None
        assert k.head(repo, wr["work_id"]) == current


def test_inflight_model_output_is_audited_but_never_accepted_or_resent(kernel_env):
    from threading import Event
    from test_p5_supervisor import backend
    from test_s9_dependencies import world, set_fact
    from novel_factory.application.runtime_tasks import RuntimeTaskService
    from novel_factory.application.agent_gateway import AgentGateway
    from novel_factory.application.supervisor import RuntimeSupervisor

    k = kernel_env["kernel"]
    f = world(k)
    user, letter = f["principal"], f["letter"]
    wr = set_fact(k, f, f["world_ref"], letter, "message_authentic", {"kind": "BOOLEAN", "value": True}, 2)
    args = (user, wr["work_id"], wr["branch_id"])
    with backend(concurrent=True) as (adapter, requests, server), ThreadPoolExecutor(1) as pool:
        server.started, server.release = Event(), Event()
        gateway = AgentGateway(k, {"test": adapter})
        supervisor = RuntimeSupervisor(k, gateway)
        job = RuntimeTaskService(k, gateway).prepare(user, wr, role_id="evidence_maintainer", task_kind="summary",
            story_time=point(2), subject_ids=[letter], predicates=["message_authentic"], queue_state="READY",
            assignment="根据这封密信当前的真实性整理待执行决定。", adapter_id="test")
        supervisor.enroll(user, job.task["task_id"], manifest_ref=job.manifest_ref,
            knowledge_cut={"plane": "WORLD"}, context_tokens=20000, input_ref=job.agent_input["input_ref"],
            budget_id=k.tasks.create_budget(wr["work_id"], "1"), reserve_amount="0.1", token_reservation=100000,
            accept_summary=True)
        attempt = pool.submit(supervisor.run_once, *args)
        try:
            assert server.started.wait(10)
            change = set_fact(k, f, wr, letter, "message_authentic", {"kind": "BOOLEAN", "value": False}, 6)
        finally:
            server.release.set()
        returned = attempt.result(timeout=10)
        with k.db.uow() as repo:
            observed = repo.conn.execute(text("SELECT keys FROM nf_semantic_reads WHERE task_id=:id"),
                                         {"id": job.task["task_id"]}).scalars().all()
        assert returned.get("reason") == "DEPENDENCY_CONFLICT", (returned, observed)
        for _ in range(4):
            supervisor.run_once(*args)
        assert len(requests) == 1
        with k.db.uow() as repo:
            call = repo.one("call_attempts", {"task_id": job.task["task_id"]})
            audit = gateway._read_audit(repo, call["attempt_id"])
            assert audit["payload"]["output_ref"] and repo.one("tasks", {"task_id": job.task["task_id"]})["state"] in {"STALE", "PAUSED"}
            assert repo.one("task_results", {"task_id": job.task["task_id"]}) is None
            assert k.head(repo, wr["work_id"]) == change


@pytest.mark.parametrize("configured,limit", [(None, 2), ("1", 1), ("0", 0)])
def test_supervisor_bounds_repeated_refreshes_and_preserves_original_receipt(kernel_env, monkeypatch, configured, limit):
    from novel_factory.application.queries import WorldQueryService
    from novel_factory.application.supervisor import RuntimeSupervisor
    from test_s9_dependencies import set_fact, world

    if configured is None:
        monkeypatch.delenv("NF_S9_MAX_REFRESHES", raising=False)
    else:
        monkeypatch.setenv("NF_S9_MAX_REFRESHES", configured)
    k = kernel_env["kernel"]
    f = world(k)
    user, wr, guard = f["principal"], f["world_ref"], f["ids"]["guard1"]
    truth = {"kind": "BOOLEAN", "value": True}
    wr = set_fact(k, f, wr, guard, "on_duty", truth, 2)
    reader = create_bound_task(k, user, wr, point(2), [guard], ["on_duty"])
    tid = reader["task_id"]
    args = (user, wr["work_id"], wr["branch_id"])
    supervisor = RuntimeSupervisor(k, None)
    for step in range(limit + 1):
        wr = set_fact(k, f, wr, guard, "on_duty", {"kind": "BOOLEAN", "value": bool(step % 2)}, 3 + step)
        for _ in range(32):
            result = supervisor.advance_world(*args)
            if result and result.get("task_id") == tid:
                break
        assert result and result["task_id"] == tid
        if step == limit:
            break
        assert result["outcome"] == "RECOMPUTE_QUEUED"
        assert result["repair_limits"]["max_refreshes"] == limit
        ready = k.claim_task(user, "refresh", task_id=tid)
        compiled = WorldQueryService(k).context_compile(user, {
            "schema_version": "2.1", "task_id": tid, "generation": ready["generation"],
            "recipe_id": "ADJUDICATOR", "input_manifest_ref": reader["manifest_ref"],
            "read_contract": {"revision": {"mode": "FIXED", "world_ref": wr}, "process_policy": "ALLOW_PARTIAL",
                              "index_policy": "AUTHORITATIVE", "max_wait_ms": 0},
            "knowledge_cut": {"plane": "WORLD"}, "subject_time": {"kind": "POINT", "at": point(2)},
            "token_budget": 50000, "idempotency_key": f"refresh-context-{step}",
        })
        assert compiled["status"] == "READY"

    assert (result["outcome"], result["reason"]) == ("REVIEW_REQUIRED", "REPAIR_BUDGET_EXHAUSTED")
    with k.db.uow() as repo:
        assert repo.one("tasks", {"task_id": result["review_task_id"]})["state"] == "WAITING_REVIEW"
        assert repo.many("call_attempts", {"task_id": tid}) == []
        count = repo.conn.scalar(text("""SELECT count(*) FROM nf_conflict_resolutions
            WHERE task_id=:task AND outcome='RECOMPUTE_QUEUED'"""), {"task": tid})
        assert count == limit
    monkeypatch.setenv("NF_S9_MAX_REFRESHES", "5")
    replay = ConflictResolutionService(k).resolve(*args, result["case_id"])
    assert replay["receipt_id"] == result["receipt_id"] and replay["repair_limits"]["max_refreshes"] == limit


def test_invalid_service_limit_does_not_consume_case_or_dispatch(kernel_env, monkeypatch):
    from novel_factory.application.supervisor import RuntimeSupervisor

    k = kernel_env["kernel"]
    fixture, task, _, _ = changed_reader(k)
    _, args, case = case_for(k, fixture, task)
    supervisor = RuntimeSupervisor(k, None)
    monkeypatch.setenv("NF_S9_MAX_IMPACT", "-1")
    with pytest.raises(ValueError, match="NF_S9_MAX_IMPACT"):
        supervisor.advance_world(*args)
    with k.db.uow() as repo:
        assert repo.conn.scalar(text("SELECT count(*) FROM nf_conflict_resolutions WHERE case_id=:id"),
                                {"id": case}) == 0
    monkeypatch.setenv("NF_S9_MAX_IMPACT", "0")
    receipt = ConflictResolutionService(k).resolve(*args, case)
    assert (receipt["outcome"], receipt["reason"]) == ("REVIEW_REQUIRED", "IMPACT_LIMIT")
    with k.db.uow() as repo:
        assert repo.one("tasks", {"task_id": receipt["review_task_id"]})["state"] == "WAITING_REVIEW"
        assert repo.many("call_attempts", {"task_id": task["task_id"]}) == []
