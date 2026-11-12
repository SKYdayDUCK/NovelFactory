"""S2 production task, plan pointer, sealed actor contexts and PostgreSQL race tests."""
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import json
from threading import Barrier
from uuid import uuid4

import pytest

from novel_factory.application import creative_simulation
from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.creative_planning import CreativePlanningService
from novel_factory.application.creative_simulation import CreativeSimulationService
from novel_factory.application.budget_limits import SharedBudgetService
from novel_factory.application.planning import PlanningService
from novel_factory.application.runtime_tasks import RuntimeTaskService
from novel_factory.domain.errors import DomainError
from novel_factory.domain.propositions import encode_proposition
from novel_factory.fixtures import entity_value, fixture_definition, point, run_command
from novel_factory.infrastructure.creative_config import CreativeHarnessConfig
from test_agent_gateway import FixtureAdapter
from test_planning import make_node, volume_payload


pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def uid():
    return str(uuid4())


def scope(job):
    return {name: job.task[name] for name in ("task_id", "generation")}


@pytest.fixture
def scene(kernel_env, monkeypatch):
    kernel = kernel_env["kernel"]
    config = CreativeHarnessConfig(enabled=True)
    monkeypatch.setenv("NF_CREATIVE_HARNESS_CONFIG", json.dumps(config.as_dict()))
    world = fixture_definition("r1_sword")
    ids = world["ids"]
    for actor, believed_owner in (("protagonist", "smith"), ("friend", "rival")):
        value = entity_value(ids[believed_owner])
        world["genesis"]["cognitions"].append({
            "record_id": uid(), "actor_id": ids[actor], "record_kind": "BELIEF", "acquired_at": point(1),
            "statement": encode_proposition(ids["sword"], "legal_owner", value), "evidence_refs": [],
        })
    world.update(kernel.bootstrap_work("TEST_ONLY:hypothetical choices", world["genesis"],
                                       frontiers=world["frontiers"]))
    wr, owner = world["world_ref"], world["principal"]
    planning = PlanningService(kernel)
    volume = volume_payload(world)
    contract = planning.save_volume_contract(owner, volume, "volume")
    published = make_node(ids["sect"], goal="Keep the published promise to guard the bridge.")
    question = make_node(ids["sword"], goal="Ask the smith to identify the sword.",
                         condition={"kind": "STATE_EQUALS", "subject_id": ids["sword"],
                                    "predicate": "legal_owner", "expected_value": entity_value(ids["smith"])})
    detour = make_node(ids["sword"], goal="Hide the sword until both witnesses can meet.")
    side = make_node(ids["sword"], goal="Ask a passing witness what she saw.")
    question["subject_ids"].append(ids["protagonist"])
    detour["subject_ids"].append(ids["friend"])
    plan = {
        "schema_version": "2.1", "plan_id": uid(), "plan_revision_id": uid(), "volume_id": volume["volume_id"],
        "input_world_ref": wr, "volume_contract_ref": contract, "prior_plan_ref": None,
        "nodes": [published, question, detour, side], "published_prefix_node_ids": [published["node_id"]],
        "dependencies": [{"source_node_id": question["node_id"], "target_node_id": side["node_id"],
                          "kind": "HARD_PREREQUISITE", "reason": "Ask a witness after identifying the sword."}],
        "decision": {"reason": "Preserve prior commitments.",
            "considered_options": [{"option_id": uid(), "summary": "Pause at the bridge.",
                                    "disposition": "SELECTED", "reason": "Both witnesses are reachable."}]},
    }
    anchor = planning.save_plan(owner, plan, "anchor")
    selection = planning.adopt_plan(owner, wr, anchor, None, "initial-selection")
    adapter = FixtureAdapter()
    runtime = RuntimeTaskService(kernel, AgentGateway(kernel, {"fixture": adapter}))
    options = {"story_time": point(1, 1), "subject_ids": [ids["sword"]], "predicates": ["legal_owner"],
               "adapter_id": "fixture"}
    nodes = [question["node_id"], detour["node_id"]]

    def issue_planner():
        job = runtime.prepare(owner, wr, role_id="volume_director", task_kind="window_plan",
                              assignment="Consider the two proposed routes.",
                              knowledge_cut=selection["knowledge_cut"],
                              input_artifacts=[anchor, contract, selection["plan_manifest_ref"]], **options)
        planning.bind_task_for_controller(owner, **scope(job), plan_ref=anchor,
                                          node_ids=[*nodes, side["node_id"]], idempotency_key="assign:" + job.task["task_id"])
        return job

    planner = issue_planner()
    actors = [runtime.prepare(owner, wr, role_id="character_actor", task_kind="character_action",
                              actor_id=ids[key], assignment="Act on your own account of the sword.", **options)
              for key in ("protagonist", "friend")]
    budget = kernel.tasks.create_budget(wr["work_id"], "1")
    SharedBudgetService(kernel).set_limit(owner, wr["work_id"], wr["branch_id"], budget, 100000)
    quota = {"currency": "USD", "max_amount": "0.10", "max_tokens": 50000, "max_attempts": 4,
             "budget_id": budget, "deadline": (datetime.now(UTC) + timedelta(minutes=3)).isoformat()}
    return dict(kernel=kernel, world=world, wr=wr, owner=owner, planner=planner, issue_planner=issue_planner, actors=actors,
                plan=plan, anchor=anchor, nodes=nodes, extra_node=side["node_id"], quota=quota, adapter=adapter)


def state(scene):
    with scene["kernel"].db.uow(write=False) as repo:
        wr = scene["wr"]
        return {
            "head": scene["kernel"].head(repo, wr["work_id"], wr["branch_id"]),
            "events": [r["occurrence_id"] for r in repo.many("events", {"work_id": wr["work_id"]})],
            "branches": len(repo.many("branches", {"work_id": wr["work_id"]})),
            "plan": repo.one("selections", {"work_id": wr["work_id"],
                "selection_kind": PlanningService.selection_key(scene["plan"]["volume_id"])})["artifact_ref"],
        }


def propose(scene, *, trigger="MOTIVE_CONFLICT", key="hypotheses", quota=None, nodes=None):
    return CreativePlanningService(scene["kernel"]).propose(
        scene["planner"].principal, **scope(scene["planner"]), plan_ref=scene["anchor"],
        node_ids=scene["nodes"] if nodes is None else nodes, trigger=trigger,
        quota=scene["quota"] if quota is None else quota, idempotency_key=key, horizon_scenes=3)


def simulate(scene, candidate, *, key="compare", actors=None, branches=2, steps=4):
    return CreativeSimulationService(scene["kernel"]).run(
        scene["owner"], **scope(scene["planner"]), candidate_ref=candidate["candidate_ref"],
        actor_tasks=[scope(actor) for actor in (scene["actors"] if actors is None else actors)],
        max_branches=branches, max_steps_per_branch=steps, seed=19, idempotency_key=key)


def decide(scene, candidate, option_id, key, *, principal=None):
    return CreativePlanningService(scene["kernel"]).decide(
        scene["owner"] if principal is None else principal, **scope(scene["planner"]),
        candidate_ref=candidate["candidate_ref"], option_id=option_id, idempotency_key=key)


def test_single_path_and_reject_all_preserve_selected_plan(scene):
    before = state(scene)
    ordinary = propose(scene, trigger="ORDINARY", nodes=scene["nodes"][:1], key="simple-scene")
    assert ordinary["candidate"]["scope"] == {**scope(scene["planner"]), "world_ref": scene["wr"]}
    assert len(ordinary["candidate"]["alternatives"]) == 1
    assert ordinary["candidate"]["status"] == "HYPOTHESIS_ONLY"
    result = decide(scene, ordinary, None, "keep-the-original", principal=scene["planner"].principal)
    assert result["status"] == "DECLINED"
    assert decide(scene, ordinary, None, "keep-the-original", principal=scene["planner"].principal) == result
    assert state(scene) == before
    assert scene["adapter"].calls == []


@pytest.mark.concurrency
def test_concurrent_proposals_recover_one_candidate_and_only_one_adoption_wins(scene):
    barrier = Barrier(2)

    def race_proposal(_):
        barrier.wait(timeout=10)
        return propose(scene)

    with ThreadPoolExecutor(2) as pool:
        first, second = pool.map(race_proposal, range(2))
    assert first == second == propose(scene)
    candidate = first["candidate"]
    assert candidate["plan_anchor"] == scene["anchor"]
    assert candidate["scope"]["world_ref"] == scene["wr"]
    alternatives = candidate["alternatives"]
    assert len(alternatives) == 2 and len({a["goal"] for a in alternatives}) == 2
    assert all(a["expected_consequences"] and a["risks"] and a["selection_reason"] for a in alternatives)
    choices = [a["option_id"] for a in alternatives]
    before = state(scene)

    def race_decision(option):
        barrier.wait(timeout=10)
        try:
            return option, decide(scene, first, option, "approve-" + option)
        except DomainError as error:
            return option, error

    with ThreadPoolExecutor(2) as pool:
        outcomes = list(pool.map(race_decision, choices))
    successes = [(option, result) for option, result in outcomes if isinstance(result, dict)]
    assert len(successes) == 1
    assert {r.code for _, r in outcomes if isinstance(r, DomainError)} <= {
        "PLAN_SELECTION_CONFLICT", "STALE_PLAN", "TASK_NOT_ACCEPTABLE"}
    selected, receipt = successes[0]
    assert receipt["status"] == "ADOPTED"
    assert decide(scene, first, selected, "approve-" + selected) == receipt
    after = state(scene)
    assert after["plan"] == receipt["plan_ref"] != before["plan"]
    assert {k:v for k,v in after.items() if k != "plan"} == {k:v for k,v in before.items() if k != "plan"}
    selected_plan = PlanningService(scene["kernel"]).get_plan(scene["owner"], scene["wr"]["work_id"], after["plan"])
    assert selected_plan["prior_plan_ref"] == scene["anchor"]
    chosen_goal = next(a["goal"] for a in alternatives if a["option_id"] == selected)
    with scene["kernel"].db.uow(write=False) as repo:
        manifest = scene["kernel"].artifact(repo, receipt["adoption"]["plan_manifest_ref"],
                                            scene["wr"]["work_id"], kind="PLAN_MANIFEST")["payload"]
        open_items = [n for n in manifest["plans"] if n["plan_id"] in scene["nodes"]]
    assert {n["summary"]: n["status"] for n in open_items} == {
        option["goal"]: "READY" if option["goal"] == chosen_goal else "DRAFT" for option in alternatives}
    published = scene["plan"]["published_prefix_node_ids"]
    assert selected_plan["published_prefix_node_ids"] == published
    assert [n for n in selected_plan["nodes"] if n["node_id"] in published] == [
        n for n in scene["plan"]["nodes"] if n["node_id"] in published]


def test_world_change_invalidates_candidate_without_a_second_world_event(scene):
    candidate = propose(scene)
    result = run_command(scene["kernel"], scene["owner"], scene["wr"], "DESTROY_ITEM",
                         {"item_id": scene["world"]["ids"]["sword"], "at": point(40)})
    changed = state(scene)
    assert changed["head"] == result["world_ref"] and len(changed["events"]) == 1
    with pytest.raises(DomainError):
        simulate(scene, candidate)
    with pytest.raises(DomainError):
        decide(scene, candidate, candidate["candidate"]["alternatives"][0]["option_id"], "late-approval")
    assert state(scene) == changed


def test_actor_disagreement_changes_hypothesis_without_exposing_private_facts(scene):
    before = state(scene)
    candidate = propose(scene)
    both = simulate(scene, candidate)
    assert both == simulate(scene, candidate)
    assert both == simulate(scene, candidate, key="same-run-after-restart")
    with pytest.raises(DomainError) as changed_request:
        simulate(scene, candidate, key="changing-an-existing-run", actors=scene["actors"][:1])
    assert changed_request.value.code == "IDEMPOTENCY_MISMATCH"
    assert both["report"]["status"] == "HYPOTHESIS_ONLY" and both["report"]["accepted_world_events"] == 0
    assert both["report"]["plan_candidate_ref"] == candidate["candidate_ref"]
    assert both["report"]["scope"]["world_ref"] == before["head"]
    assert len(both["report"]["outcomes"]) == 2
    second_assignment = {**scene, "planner": scene["issue_planner"]()}
    second_candidate = propose(second_assignment)
    alone = simulate(second_assignment, second_candidate, key="one-witness", actors=scene["actors"][:1])
    assert any(s["reaction"] == "DISAGREEMENT" for s in both["stops"])
    assert not any(s["reaction"] == "DISAGREEMENT" for s in alone["stops"])
    assert [o["conditional_result"] for o in both["report"]["outcomes"]] != [
        o["conditional_result"] for o in alone["report"]["outcomes"]]
    for response in (both, alone):
        public = json.dumps(response["report"])
        assert scene["world"]["ids"]["rival"] not in public
        assert all(a.private_token not in public for a in scene["actors"])
        assert response["usage"]["model_tokens"] == response["usage"]["model_attempts"] == 0
        assert all(stop["steps"] <= 4 for stop in response["stops"])
    assert state(scene) == before and scene["adapter"].calls == []
    # A revoked actor's receipt is revalidated even on idempotent resume.
    scene["kernel"].revoke_grant(scene["owner"], scene["actors"][1].principal.grant_id)
    with pytest.raises(DomainError):
        simulate(scene, candidate)
    assert state(scene) == before


def test_branch_and_step_limits_are_measured_on_actual_outcomes(scene):
    candidate = propose(scene)
    before = state(scene)
    limited = simulate(scene, candidate, branches=1, steps=1, actors=scene["actors"][:1])
    assert {s["reason"] for s in limited["stops"]} >= {"BRANCH_LIMIT", "STEP_LIMIT"}
    assert all(s["steps"] <= 1 for s in limited["stops"])
    assert sum(s["steps"] for s in limited["stops"]) <= 1
    second = {**scene, "planner": scene["issue_planner"]()}
    extended = simulate(second, propose(second), key="two-steps", branches=1, steps=2, actors=scene["actors"][:1])
    first_path = next(s["path"] for s in extended["stops"] if s["steps"])
    assert [step["node_id"] for step in first_path] == [scene["nodes"][0], scene["extra_node"]]
    assert limited["stops"][0]["path"] == first_path[:1]
    assert state(scene) == before


def test_token_budget_exhaustion_stops_before_any_paid_attempt(scene):
    before = state(scene)
    candidate = propose(scene, quota={**scene["quota"], "max_tokens": 1})
    result = simulate(scene, candidate)
    assert any(s["reason"] == "TOKEN_BUDGET" for s in result["stops"])
    assert result["usage"]["model_tokens"] == result["usage"]["model_attempts"] == 0
    assert result["usage"]["local_input_units"] <= 1
    assert state(scene) == before and scene["adapter"].calls == []


def test_published_node_and_revoked_planner_cannot_generate_alternatives(scene):
    before = state(scene)
    with pytest.raises(DomainError) as protected:
        propose(scene, nodes=scene["plan"]["published_prefix_node_ids"], trigger="ORDINARY")
    assert protected.value.code == "VIEW_DENIED"
    candidate = propose(scene)
    with pytest.raises(DomainError) as dependent:
        decide(scene, candidate, candidate["candidate"]["alternatives"][0]["option_id"],
               "planner-cannot-self-approve", principal=scene["planner"].principal)
    assert dependent.value.code == "VIEW_DENIED"
    scene["kernel"].revoke_grant(scene["owner"], scene["planner"].principal.grant_id)
    with pytest.raises(DomainError):
        propose(scene)
    with pytest.raises(DomainError):
        simulate(scene, candidate)
    assert state(scene) == before


@pytest.mark.parametrize("cap", ["money", "budget_scope"])
def test_candidate_quota_cannot_exceed_real_nf_authority(scene, cap):
    quota = dict(scene["quota"])
    if cap == "money":
        quota["max_amount"] = "50.00"
    else:
        other = fixture_definition("white_bridge")
        work = scene["kernel"].bootstrap_work("TEST_ONLY:separate budget", other["genesis"],
                                              frontiers=other["frontiers"])
        quota["budget_id"] = scene["kernel"].tasks.create_budget(work["world_ref"]["work_id"], "1")
    before = state(scene)
    with pytest.raises(DomainError) as error:
        propose(scene, quota=quota)
    assert error.value.code == ("BUDGET_EXCEEDED" if cap == "money" else "VIEW_DENIED")
    assert state(scene) == before and scene["adapter"].calls == []


def test_monotonic_deadline_and_attempt_limit_stop_rollout(scene, monkeypatch):
    before = state(scene)
    candidate = propose(scene, quota={**scene["quota"], "max_attempts": 1})
    limited = simulate(scene, candidate)
    assert limited["usage"]["rollout_attempts"] == 1
    assert "ATTEMPT_LIMIT" in {s["reason"] for s in limited["stops"]}

    # The immutable UTC deadline stays within the planning task lease. Elapsed
    # time crosses that deadline after the service samples the real DB clock.
    readings = iter((0.0,))
    monkeypatch.setattr(creative_simulation, "monotonic", lambda: next(readings, 3600.0))
    next_assignment = {**scene, "planner": scene["issue_planner"]()}
    next_candidate = propose(next_assignment)
    expired = simulate(next_assignment, next_candidate, key="deadline")
    assert all(s["reason"] == "DEADLINE" and s["steps"] == 0 for s in expired["stops"])
    assert expired["usage"]["model_attempts"] == expired["usage"]["rollout_attempts"] == 0
    assert state(scene) == before


def test_volume_branch_ceiling_rejects_even_three_authorized_paths(scene):
    before = state(scene)
    with pytest.raises(DomainError) as over_limit:
        propose(scene, nodes=[*scene["nodes"], scene["extra_node"]])
    assert over_limit.value.code == "CAPACITY_LIMIT"
    assert state(scene) == before


@pytest.mark.concurrency
def test_competing_run_claims_consume_one_frozen_allowance(scene):
    candidate = propose(scene)
    before = state(scene)
    barrier = Barrier(2)

    def compete(branches):
        barrier.wait(timeout=10)
        try:
            return simulate(scene, candidate, key=f"run-{branches}", branches=branches)
        except DomainError as error:
            return error

    with ThreadPoolExecutor(2) as pool:
        outcomes = list(pool.map(compete, (1, 2)))
    assert len([v for v in outcomes if isinstance(v, dict)]) == 1
    assert [e.code for e in outcomes if isinstance(e, DomainError)] == ["IDEMPOTENCY_MISMATCH"]
    with scene["kernel"].db.uow(write=False) as repo:
        for kind in ("CREATIVE_SIMULATION_RUN", "CREATIVE_SIMULATION_REPORT"):
            assert len(repo.many("artifacts", {"work_id": scene["wr"]["work_id"], "kind": kind})) == 1
    assert state(scene) == before


def test_revocation_during_local_rule_evaluation_prevents_report_publication(scene, monkeypatch):
    candidate = propose(scene)
    before = state(scene)
    original = creative_simulation._rollout

    def revoke_after_rules(*args):
        outcome = original(*args)
        scene["kernel"].revoke_grant(scene["owner"], scene["actors"][0].principal.grant_id)
        return outcome

    monkeypatch.setattr(creative_simulation, "_rollout", revoke_after_rules)
    with pytest.raises(DomainError) as revoked:
        simulate(scene, candidate)
    assert revoked.value.code == "VIEW_DENIED"
    with scene["kernel"].db.uow(write=False) as repo:
        for kind in ("CREATIVE_SIMULATION_RUN", "CREATIVE_SIMULATION_REPORT"):
            assert not repo.many("artifacts", {"work_id": scene["wr"]["work_id"], "kind": kind})
    assert state(scene) == before
