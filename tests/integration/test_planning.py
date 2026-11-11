"""P2-01 and R1-A planning over real immutable PostgreSQL world revisions."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from novel_factory.application.planning import PlanningService, validate_task_plan_binding
from novel_factory.application.queries import WorldQueryService
from novel_factory.contracts.registry import validate_contract
from novel_factory.domain.canonical import fingerprint
from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import create_bound_task, load_fixture, point, run_command

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def uid():
    return str(uuid4())


def volume_payload(fixture):
    return {"schema_version": "2.0", "volume_id": uid(), "contract_revision_id": uid(),
            "input_world_ref": fixture["world_ref"], "creative_brief": "保留损失后寻找合作出路",
            "focus_subject_ids": [fixture["ids"]["protagonist"]], "narrative_intents": ["尊重双方独立动机"],
            "protected_commitments": [], "open_choices": ["救济是否按时成功尚未确定"],
            "completion_or_stop_conditions": ["整卷编辑完成或授权预算耗尽"],
            "budget": {"max_semantic_calls": 24, "max_candidate_branches": 2},
            "upstream_literary_refs": [], "dependency_fingerprint": fingerprint(fixture["world_ref"])}


def make_node(subject, *, condition=None, goal="携带完整青霜剑抵达山门"):
    return {"node_id": uid(), "goal": goal, "status": "VIABLE", "target_time": {"kind": "EXACT", "at": point(50)},
            "subject_ids": [subject], "hard_preconditions": [condition] if condition else [],
            "risk_notes": ["未来结果仍需世界裁决"], "failure_exit": "承担失去旧路的代价", "fulfillment_event_refs": []}


def make_plan(service, fixture):
    volume = volume_payload(fixture)
    contract = service.save_volume_contract(fixture["principal"], volume, "volume:" + volume["volume_id"])
    first = make_node(fixture["ids"]["sword"], condition={"kind": "ENTITY_ACTIVE", "subject_id": fixture["ids"]["sword"]})
    successor = make_node(fixture["ids"]["protagonist"], goal="凭剑求见旧友")
    independent = make_node(fixture["ids"]["friend"], goal="友人继续独立清点仓库")
    plan = {"schema_version": "2.1", "plan_id": uid(), "plan_revision_id": uid(), "volume_id": volume["volume_id"],
            "input_world_ref": fixture["world_ref"], "volume_contract_ref": contract, "prior_plan_ref": None,
            "nodes": [first, successor, independent], "published_prefix_node_ids": [],
            "dependencies": [{"source_node_id": first["node_id"], "target_node_id": successor["node_id"],
                              "kind": "HARD_PREREQUISITE", "reason": "求见路线依赖完整剑作为凭证"}],
            "decision": {"reason": "选择保留不确定性的路径", "considered_options": [
                {"option_id": uid(), "summary": "持剑求见", "disposition": "SELECTED", "reason": "当前前提可行"},
                {"option_id": uid(), "summary": "强行闯入", "disposition": "REJECTED", "reason": "无正当动机"}]}}
    ref = service.save_plan(fixture["principal"], plan, "plan:" + plan["plan_revision_id"])
    return plan, ref


def task_with_plan(kernel, fixture, plan_ref):
    principal, wr = fixture["principal"], fixture["world_ref"]
    manifest = kernel.put_artifact(principal, wr["work_id"], {"subject_ids": [fixture["ids"]["sword"]], "predicates": ["lifecycle"]}, "INPUT_MANIFEST")
    task_id = uid()
    spec = {"schema_version": "2.0", "task_id": task_id, "generation": 0, "role_id": "control_operator", "task_kind": "scene_plan",
            "state": "READY", "world_ref": wr, "story_time": point(1), "context_bundle_id": None,
            "policy_version": "nf-policy-1", "dependency_fingerprint": None, "runtime_contract_version": fingerprint("planning-test"),
            "capability_grant_id": principal.grant_id, "input_artifacts": [manifest, plan_ref],
            "budget": {"currency": "USD", "max_amount": "1", "token_budget": 50000},
            "wall_deadline": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}
    kernel.create_task(principal, spec, "plan-task:" + task_id)
    task = kernel.claim_task(principal, "writer:" + task_id, lease_seconds=3600, task_id=task_id)
    WorldQueryService(kernel).context_compile(principal, {"schema_version": "2.1", "task_id": task_id, "generation": task["generation"],
        "recipe_id": "ADJUDICATOR", "input_manifest_ref": manifest, "read_contract": {"revision": {"mode": "FIXED", "world_ref": wr},
        "process_policy": "ALLOW_PARTIAL", "index_policy": "AUTHORITATIVE", "max_wait_ms": 0},
        "knowledge_cut": {"plane": "WORLD"}, "subject_time": {"kind": "POINT", "at": point(1)},
        "token_budget": 50000, "idempotency_key": "compile:" + task_id})
    return task


def test_world_dossier_preserves_offstage_entities_and_requires_review(kernel_env):
    k = kernel_env["kernel"]; fixture = load_fixture(k, "white_bridge"); s = PlanningService(k)
    wr, ids = fixture["world_ref"], fixture["ids"]
    designer = k.issue_grant(fixture["principal"], wr["work_id"], role_id="world_designer",
        capabilities=["READ", "ARTIFACT_WRITE"], planes=["WORLD"], max_story_time=point(100))["principal"]
    with k.db.uow(write=False) as r:
        work = r.one("works", {"work_id": wr["work_id"]})
        setup = r.one("artifacts", {"work_id": wr["work_id"], "kind": "INITIAL_SETUP"})
        setup_ref = {field: setup[field] for field in ("artifact_id", "artifact_revision_id", "sha256")}
    dossier = {"schema_version": "2.1", "dossier_id": uid(), "input_world_ref": wr, "ruleset_ref": work["rules_ref"],
        "initial_setup_ref": setup_ref, "background_entity_ids": [ids["smith2"], ids["sword2"]],
        "factions": [{"entity_id": ids["sect"], "decision_maker_id": ids["master"],
                      "independent_goal": "保住冬季粮路", "without_protagonist_action": "照常检查桥梁并安排仓储", "constraints": ["资源守恒"]}],
        "sections": {name: "既有底稿的解释性摘要" for name in ("geography", "history", "institutions", "resources", "powers")},
        "open_questions": [], "source_event_refs": [], "proposed_world_changes": []}
    ref = s.save_world_dossier(designer, dossier, "dossier")
    review = s.review_world_dossier(fixture["principal"], wr, ref, "APPROVED", [], "review")
    assert s.review_world_dossier(fixture["principal"], wr, ref, "APPROVED", [], "review") == review
    with k.db.uow(write=False) as r:
        assert k.head(r, wr["work_id"]) == wr
        assert r.one("artifacts", {"artifact_revision_id": review["artifact_revision_id"]})["payload"]["world_effects_applied"] == 0
    rejected = deepcopy(dossier); rejected["proposed_world_changes"] = ["主角突然获得额外一百袋粮食"]
    ref2 = s.save_world_dossier(designer, rejected, "unaccepted-setting")
    with pytest.raises(DomainError) as error:
        s.review_world_dossier(fixture["principal"], wr, ref2, "APPROVED", [], "invalid-review")
    assert error.value.code == "WORLD_SETUP_INCOMPLETE"
    fake = deepcopy(dossier); fake["ruleset_ref"] = setup_ref
    with pytest.raises(DomainError):
        s.save_world_dossier(designer, fake, "forged-rules")


def test_plan_adoption_exposes_future_cut_without_effects_or_actor_leak(kernel_env):
    k = kernel_env["kernel"]; f = load_fixture(k); s = PlanningService(k)
    plan, ref = make_plan(s, f)
    result = s.adopt_plan(f["principal"], f["world_ref"], ref, None, "adopt")
    assert s.adopt_plan(f["principal"], f["world_ref"], ref, None, "adopt") == result
    with k.db.uow(write=False) as r:
        assert k.head(r, f["world_ref"]["work_id"]) == f["world_ref"]
        assert not r.many("events", {"work_id": f["world_ref"]["work_id"]})
        manifest = k.artifact(r, result["plan_manifest_ref"], f["world_ref"]["work_id"])["payload"]
        validate_contract("PlanManifest", manifest, "kernel")
    task = create_bound_task(k, f["principal"], f["world_ref"], point(60))
    request = {"schema_version": "2.1", "task_id": task["task_id"], "generation": task["generation"],
        "read_contract": {"revision": {"mode": "FIXED", "world_ref": f["world_ref"]}, "process_policy": "ALLOW_PARTIAL", "index_policy": "AUTHORITATIVE", "max_wait_ms": 0},
        "knowledge_cut": result["knowledge_cut"], "subject_time": {"kind": "POINT", "at": point(50)},
        "query": {"kind": "TIMELINE", "subject_ids": [f["ids"]["sword"]], "entry_kinds": ["PLAN"], "time_axis": "TARGET", "include_inactive": True},
        "page_size": 50, "cursor": None}
    answer = WorldQueryService(k).query(f["principal"], request)
    assert answer["payload"]["entries"][0]["entry_kind"] == "PLAN"
    actor = k.issue_grant(f["principal"], f["world_ref"]["work_id"], role_id="character_actor", actor_id=f["ids"]["protagonist"],
        capabilities=["READ", "ARTIFACT_WRITE"], planes=["BELIEF"], max_story_time=point(100))["principal"]
    with pytest.raises(DomainError) as denied:
        s.get_plan(actor, f["world_ref"]["work_id"], ref)
    assert denied.value.code == "VIEW_DENIED"
    changed = deepcopy(plan); changed["nodes"][0]["goal"] = "不同内容"
    with pytest.raises(DomainError) as idem:
        s.save_plan(f["principal"], changed, "plan:" + plan["plan_revision_id"])
    assert idem.value.code == "IDEMPOTENCY_MISMATCH"


def test_destroyed_future_prerequisite_fences_immediately_then_replans_locally(kernel_env):
    k = kernel_env["kernel"]; f = load_fixture(k); s = PlanningService(k)
    plan, ref = make_plan(s, f)
    s.adopt_plan(f["principal"], f["world_ref"], ref, None, "initial-adopt")
    affected = task_with_plan(k, f, ref); unaffected = task_with_plan(k, f, ref)
    s.bind_task(f["principal"], affected["task_id"], affected["generation"], ref, [plan["nodes"][1]["node_id"]], "bind-affected")
    s.bind_task(f["principal"], unaffected["task_id"], unaffected["generation"], ref, [plan["nodes"][2]["node_id"]], "bind-unaffected")
    accepted = run_command(k, f["principal"], f["world_ref"], "DESTROY_ITEM", {"item_id": f["ids"]["sword"], "at": point(40)})
    with k.db.uow(write=False) as r:
        current_task = r.one("tasks", {"task_id": affected["task_id"]})
        with pytest.raises(DomainError) as stale:
            validate_task_plan_binding(r, k, current_task)
        assert stale.value.code == "STALE_PLAN"
        validate_task_plan_binding(r, k, r.one("tasks", {"task_id": unaffected["task_id"]}))
    replacements = deepcopy(plan["nodes"][:2])
    for node in replacements:
        node.update(status="PROPOSED", hard_preconditions=[], goal="承认失剑，花费时间向旧友说明来意")
    request = {"schema_version": "2.1", "prior_plan_ref": ref, "input_world_ref": accepted["world_ref"], "replacements": replacements,
        "additional_nodes": [], "additional_dependencies": [], "decision": {"reason": "保留失剑后果与时间代价", "considered_options": [
            {"option_id": uid(), "summary": "绕行求证", "disposition": "SELECTED", "reason": "仍尊重历史损失"},
            {"option_id": uid(), "summary": "复活原剑", "disposition": "REJECTED", "reason": "违反已接受世界后果"}]}}
    result = s.replan(f["principal"], request, "replan")
    assert result == s.replan(f["principal"], request, "replan")
    revised = s.get_plan(f["principal"], f["world_ref"]["work_id"], result["plan_ref"])
    assert revised["nodes"][2] == plan["nodes"][2]
    assert revised["volume_contract_ref"] == plan["volume_contract_ref"]
    adoption = s.adopt_plan(f["principal"], accepted["world_ref"], result["plan_ref"], ref, "re-adopt")
    assert adoption["fenced_task_ids"] == [affected["task_id"]]
    assert k.get_task(f["principal"], affected["task_id"])["state"] == "STALE"
    assert k.get_task(f["principal"], unaffected["task_id"])["state"] == "RUNNING"
    with k.db.uow(write=False) as r:
        assert k.head(r, f["world_ref"]["work_id"]) == accepted["world_ref"]
        validate_task_plan_binding(r, k, r.one("tasks", {"task_id": unaffected["task_id"]}))


def test_plan_cannot_claim_unaccepted_outcomes_or_stale_adoption(kernel_env):
    k = kernel_env["kernel"]; f = load_fixture(k); s = PlanningService(k)
    plan, ref = make_plan(s, f)
    fake = deepcopy(plan); fake["plan_revision_id"] = uid()
    fake["nodes"][0].update(status="REALIZED", fulfillment_event_refs=[{"occurrence_id": uid(), "event_version_id": uid()}])
    with pytest.raises(DomainError) as invalid:
        s.save_plan(f["principal"], fake, "false-realized")
    assert invalid.value.code == "INVALID_EVIDENCE"
    run_command(k, f["principal"], f["world_ref"], "DESTROY_ITEM", {"item_id": f["ids"]["sword"], "at": point(40)})
    with pytest.raises(DomainError) as stale:
        s.adopt_plan(f["principal"], f["world_ref"], ref, None, "stale-adopt")
    assert stale.value.code == "HEAD_CONFLICT"


@pytest.mark.concurrency
def test_two_concurrent_first_plan_selections_have_one_winner(kernel_env):
    k = kernel_env["kernel"]; f = load_fixture(k); s = PlanningService(k)
    plan, ref = make_plan(s, f)
    second = deepcopy(plan); second["plan_revision_id"] = uid(); second["nodes"][0]["goal"] = "另一种候选表达"
    other = s.save_plan(f["principal"], second, "second")

    def select(ref, key):
        try:
            return s.adopt_plan(f["principal"], f["world_ref"], ref, None, key)
        except DomainError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(select, candidate, key) for candidate, key in [(ref, "one"), (other, "two")]]
        results = [future.result() for future in futures]
    assert sum(isinstance(result, dict) for result in results) == 1
    assert "PLAN_SELECTION_CONFLICT" in results


def test_existing_policy_required_for_protected_commitments(kernel_env):
    k = kernel_env["kernel"]; f = load_fixture(k); s = PlanningService(k)
    volume = volume_payload(f)
    volume["protected_commitments"] = [{"commitment_id": uid(), "description": "任何剧情都必须成功",
                                       "policy_ref": "invented-grant", "evidence_refs": []}]
    with pytest.raises(DomainError) as denied:
        s.save_volume_contract(f["principal"], volume, "invented-policy")
    assert denied.value.code == "VIEW_DENIED"
