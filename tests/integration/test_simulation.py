"""P2-02 true PostgreSQL process/intent/effect acceptance, S01/02/03/05/08."""
from copy import deepcopy
from itertools import permutations
from uuid import UUID, uuid4, uuid5

import pytest

from novel_factory.application.simulation import WorldSchedulerService
from novel_factory.domain.errors import DomainError
from novel_factory.domain.scheduling import state_from_revision
from novel_factory.domain.world import resource_at
from novel_factory.fixtures import (
    create_bound_task,
    load_fixture,
    point,
    prepare_command,
    run_command,
)

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def rules(kernel, wr):
    with kernel.db.uow(write=False) as r:
        return [{"kind": "RULE", "artifact_ref": r.one("works", {"work_id": wr["work_id"]})["rules_ref"]}]


def process_definition(kernel, fixture, *, name="crossing", scopes=None, actors=None, start=2, due=30, channels=None):
    wr, ids = fixture["world_ref"], fixture["ids"]
    pid = str(uuid5(UUID(wr["work_id"]), "process:" + name))
    actors = actors or [ids["protagonist"], ids["friend"], ids["rival"]]
    return {"schema_version": "2.2", "process_id": pid, "process_kind": "INTERACTION",
            "scope_ids": scopes or [ids["warehouse"]], "participant_ids": actors, "required_actor_ids": actors,
            "start_at": point(start), "due_at": point(due) if due is not None else None,
            "world_stage": "ACTIVE", "high_impact": True, "channels": channels or [],
            "goal": "各方有独立目标；到期仅求值，不承诺成功。", "evaluator_kind": "SEMANTIC",
            "rule_evidence_refs": rules(kernel, wr),
            "trigger_id": str(uuid5(UUID(pid), "trigger")), "trigger_version_id": str(uuid5(UUID(pid), "trigger:v1"))}


def register(kernel, fixture, process):
    service = WorldSchedulerService(kernel)
    ref = service.stage_process(fixture["principal"], fixture["world_ref"], process, "stage:" + process["process_id"])
    result = run_command(kernel, fixture["principal"], fixture["world_ref"], "REGISTER_PROCESS",
                         {"at": process["start_at"], "process": process, "record_ref": ref})
    return {**fixture, "world_ref": result["world_ref"]}, result


def group(kernel, fixture, process, *, order=None):
    service = WorldSchedulerService(kernel)
    grants = {actor: kernel.issue_grant(fixture["principal"], fixture["world_ref"]["work_id"], role_id="character_actor",
        actor_id=actor, capabilities=["READ", "COMPILE", "TASK_CREATE", "TASK_CLAIM", "ARTIFACT_WRITE"],
        planes=["BELIEF"], max_story_time=point(100)) for actor in process["required_actor_ids"]}
    interaction = service.prepare_interaction(fixture["principal"], fixture["world_ref"], process["due_at"], process["scope_ids"],
                                              {actor: grant["principal"].grant_id for actor, grant in grants.items()}, "group")
    tasks, actions = {}, {}
    for actor in order or process["required_actor_ids"]:
        principal = grants[actor]["principal"]
        task = create_bound_task(kernel, principal, fixture["world_ref"], process["due_at"],
                                 [actor], ["location"], plane="BELIEF", actor_id=actor)
        with kernel.db.uow(write=False) as r:
            context = r.one("contexts", {"task_id": task["task_id"], "generation": task["generation"]})
        action = {"schema_version": "2.0", "proposal_id": str(uuid4()), "task_id": task["task_id"],
                  "generation": task["generation"], "context_bundle_id": context["context_bundle_id"],
                  "world_ref": fixture["world_ref"], "actor_id": actor, "intent": "争取粮食，但不知道其他人的密封意图。",
                  "targets": [], "story_interval": {"start": process["due_at"], "end": point(process["due_at"]["tick"] + 1), "bounds": "[)"},
                  "resource_requests": [], "preconditions": [], "evidence_refs": [],
                  "dependency_fingerprint": context["dependency_fingerprint"], "idempotency_key": "intent:" + actor}
        service.submit_action(principal, interaction["interaction_id"], action)
        tasks[actor], actions[actor] = task, action
    return service, interaction, grants, tasks, actions


def selected(kernel, fixture, process, service, interaction, *, quantity="30"):
    command = {"template_id": "RESOURCE_TRANSFER", "args": {"from_account": fixture["ids"]["warehouse"],
        "to_account": fixture["ids"]["relief"], "resource_id": fixture["ids"]["grain"], "quantity": quantity,
        "unit": "bag", "at": process["due_at"]}}
    updates = [{"process_id": process["process_id"], "outcome": "FIRED", "world_stage": "COMPLETED", "next_due": None}]
    ref = service.choose_outcome(fixture["principal"], interaction["interaction_id"], commands=[command],
                                process_outcomes=updates, reasoning_summary="固定工程裁决：共同前态下分配30袋，剩余70袋。",
                                rule_evidence_refs=rules(kernel, fixture["world_ref"]), source_kind="FIXTURE_SELECTED")
    with kernel.db.uow(write=False) as r:
        outcome = kernel.artifact(r, ref, fixture["world_ref"]["work_id"])["payload"]
        bundle = r.one("simulation_interactions", {"interaction_id": interaction["interaction_id"]})
    from novel_factory.application.simulation import outcome_command
    return ref, outcome_command(bundle["artifact_ref"], bundle["payload"], ref, outcome)


def revision(kernel, wr):
    with kernel.db.uow(write=False) as r:
        return kernel.revision(r, wr)


def test_process_registration_is_world_versioned_and_due_is_not_success(kernel_env):
    k = kernel_env["kernel"]
    initial = load_fixture(k, "white_bridge")
    process = process_definition(k, initial)
    f, _registered = register(k, initial, process)
    assert WorldSchedulerService(k).processes(initial["principal"], initial["world_ref"]) == []
    record = WorldSchedulerService(k).processes(f["principal"], f["world_ref"])[0]
    assert record["world_stage"] == "ACTIVE" and record["evaluation_status"] == "PENDING"
    assert revision(k, f["world_ref"])["selection"] == {}
    with pytest.raises(DomainError) as error:
        run_command(k, f["principal"], f["world_ref"], "ADVANCE_SCOPE", {"scope_id": f["ids"]["warehouse"], "target": point(31)})
    assert error.value.code == "ADVANCE_BLOCKED"
    # The bound itself is excluded, so the prefix may advance exactly to D30.
    result = run_command(k, f["principal"], f["world_ref"], "ADVANCE_SCOPE", {"scope_id": f["ids"]["warehouse"], "target": point(30)})
    assert state_from_revision(revision(k, result["world_ref"]))["processes"][process["process_id"]]["evaluation_status"] == "PENDING"


def test_three_actor_joint_result_is_atomic_idempotent_and_sealed(kernel_env):
    k = kernel_env["kernel"]
    original = load_fixture(k, "white_bridge")
    process = process_definition(k, original)
    f, _ = register(k, original, process)
    service, interaction, grants, _tasks, _actions = group(k, f, process, order=list(reversed(process["required_actor_ids"])))
    actor = process["required_actor_ids"][0]
    actor_status = service.interaction_status(grants[actor]["principal"], interaction["interaction_id"])
    assert "missing_actor_ids" not in actor_status and "intent_refs" not in actor_status
    assert actor_status["own_input_stored"] is True
    # A model cannot select its own success or read the process registry.
    with pytest.raises(DomainError):
        service.processes(grants[actor]["principal"], f["world_ref"])
    _, args = selected(k, f, process, service, interaction)
    prepared = prepare_command(k, f["principal"], f["world_ref"], "JOINT_INTERACTION", args)
    def fail(stage):
        if stage == "before_commit":
            raise RuntimeError("joint atomicity failure")
    with pytest.raises(RuntimeError):
        k.accept(f["principal"], prepared["request"], fault=fail)
    assert revision(k, f["world_ref"])["selection"] == {}
    assert state_from_revision(revision(k, f["world_ref"]))["closed_interactions"] == {}
    receipt = k.accept(f["principal"], prepared["request"])
    assert k.accept(f["principal"], prepared["request"]) == receipt
    accepted = revision(k, receipt["world_ref"])
    assert resource_at(accepted["snapshot"], f["ids"]["warehouse"], f["ids"]["grain"], "bag", point(31))["quantity"] == "70"
    assert len(accepted["selection"]) == 1
    schedule = state_from_revision(accepted)
    assert schedule["processes"][process["process_id"]]["world_stage"] == "COMPLETED"
    assert interaction["interaction_id"] in schedule["closed_interactions"]
    with k.db.uow(write=False) as r:
        proposal = r.one("proposals", {"task_id": prepared["task"]["task_id"]})["payload"]
    assert proposal["trigger_evaluations"][0]["outcome"] == "FIRED"
    assert proposal["trigger_evaluations"][0]["produced_event_refs"] == receipt["accepted_event_refs"]


def test_real_wall_timeout_never_becomes_a_character_failure(kernel_env):
    k = kernel_env["kernel"]
    original = load_fixture(k, "white_bridge")
    process = process_definition(k, original)
    f, _ = register(k, original, process)
    # Create the group, but leave a required actor slot missing.
    service = WorldSchedulerService(k)
    grants = {actor: k.issue_grant(f["principal"], f["world_ref"]["work_id"], role_id="character_actor", actor_id=actor,
        planes=["BELIEF"], max_story_time=point(100)) for actor in process["required_actor_ids"]}
    interaction = service.prepare_interaction(f["principal"], f["world_ref"], point(30), process["scope_ids"],
                                              {aid: g["principal"].grant_id for aid, g in grants.items()}, "pending-group")
    with pytest.raises(DomainError) as error:
        selected(k, f, process, service, interaction)
    assert error.value.code == "INPUTS_PENDING"
    assert service.interaction_status(f["principal"], interaction["interaction_id"])["status"] == "INPUTS_PENDING"
    assert service.processes(f["principal"], f["world_ref"])[0]["world_stage"] == "ACTIVE"
    assert revision(k, f["world_ref"])["frontiers"][f["ids"]["warehouse"]] == point(1, 1)


def test_cancellation_after_sealed_intent_blocks_joint_acceptance(kernel_env):
    k = kernel_env["kernel"]
    original = load_fixture(k, "white_bridge")
    process = process_definition(k, original)
    f, _ = register(k, original, process)
    service, interaction, grants, tasks, _actions = group(k, f, process)
    _, args = selected(k, f, process, service, interaction)
    prepared = prepare_command(k, f["principal"], f["world_ref"], "JOINT_INTERACTION", args)
    actor = process["required_actor_ids"][0]
    k.cancel_task(grants[actor]["principal"], tasks[actor]["task_id"], tasks[actor]["generation"], "cancel-action")
    with pytest.raises(DomainError):
        k.accept(f["principal"], prepared["request"])
    assert revision(k, f["world_ref"])["selection"] == {}


def test_unrelated_region_can_advance_and_channel_exact_bound_is_respected(kernel_env):
    k = kernel_env["kernel"]
    original = load_fixture(k, "white_bridge")
    ids = original["ids"]
    evidence = rules(k, original["world_ref"])
    process = process_definition(k, original, name="message", scopes=[ids["south"]], start=29, due=31,
        channels=[{"channel_id": "south-to-north", "from_scope_id": ids["south"], "to_scope_id": ids["north"],
                   "min_delay_ticks": 2, "earliest_possible_impact": point(31), "completeness": "COMPLETE", "evidence_refs": evidence}])
    process["process_kind"] = "MESSAGE"
    f, _ = register(k, original, process)
    independent = run_command(k, f["principal"], f["world_ref"], "ADVANCE_SCOPE", {"scope_id": ids["reserve"], "target": point(40)})
    allowed = run_command(k, f["principal"], independent["world_ref"], "ADVANCE_SCOPE", {"scope_id": ids["north"], "target": point(31)})
    with pytest.raises(DomainError) as error:
        run_command(k, f["principal"], allowed["world_ref"], "ADVANCE_SCOPE", {"scope_id": ids["north"], "target": point(31, 1)})
    assert error.value.code == "ADVANCE_BLOCKED"
    assert WorldSchedulerService(k).processes(f["principal"], allowed["world_ref"])[0]["world_stage"] == "ACTIVE"


def test_fork_copies_only_registered_prefix_and_state_hash_is_checked(kernel_env):
    k = kernel_env["kernel"]
    original = load_fixture(k, "white_bridge")
    process = process_definition(k, original)
    f, _ = register(k, original, process)
    branch = k.fork(f["principal"], f["world_ref"], "SIMULATION", "simulation-copy")
    assert state_from_revision(revision(k, branch)) == state_from_revision(revision(k, f["world_ref"]))
    corrupted = deepcopy(revision(k, branch))
    corrupted["provenance"]["simulation_state"]["processes"][process["process_id"]]["world_stage"] = "COMPLETED"
    with pytest.raises(DomainError) as error:
        state_from_revision(corrupted)
    assert error.value.code == "PROJECTION_CORRUPT"


def test_false_trigger_evaluation_does_not_deliver_grain(kernel_env):
    k = kernel_env["kernel"]
    original = load_fixture(k, "white_bridge")
    process = process_definition(k, original)
    f, _ = register(k, original, process)
    service, interaction, _grants, _tasks, _actions = group(k, f, process)
    outcome_ref = service.choose_outcome(f["principal"], interaction["interaction_id"], commands=[],
        process_outcomes=[{"process_id": process["process_id"], "outcome": "CONDITION_FALSE", "world_stage": "SUSPENDED", "next_due": None}],
        reasoning_summary="白桥受阻，抵达条件不成立，未发生交付。", rule_evidence_refs=rules(k, f["world_ref"]), source_kind="FIXTURE_SELECTED")
    task = create_bound_task(k, f["principal"], f["world_ref"], point(30))
    service.propose_outcome(f["principal"], task["task_id"], task["generation"], outcome_ref, "false-outcome")
    with k.db.uow(write=False) as r:
        stored = r.one("proposals", {"task_id": task["task_id"]})
    prepared = prepare_command(k, f["principal"], f["world_ref"], "JOINT_INTERACTION", stored["template_args"], key="new-qualification")
    receipt = k.accept(f["principal"], prepared["request"])
    rev = revision(k, receipt["world_ref"])
    assert len(rev["selection"]) == 0
    assert resource_at(rev["snapshot"], f["ids"]["warehouse"], f["ids"]["grain"], "bag", point(31))["quantity"] == "100"
    assert service.processes(f["principal"], receipt["world_ref"])[0]["evaluation_status"] == "EVALUATED"


def test_schedule_repair_requires_rebuild_instead_of_copying_false_coverage(kernel_env):
    k = kernel_env["kernel"]
    initial = load_fixture(k, "white_bridge")
    first = run_command(k, initial["principal"], initial["world_ref"], "RESOURCE_TRANSFER",
        {"from_account": initial["ids"]["warehouse"], "to_account": initial["ids"]["relief"],
         "resource_id": initial["ids"]["grain"], "quantity": "10", "unit": "bag", "at": point(10)})
    initial = {**initial, "world_ref": first["world_ref"]}
    process = process_definition(k, initial, start=11)
    f, _ = register(k, initial, process)
    repair = k.fork(f["principal"], f["world_ref"], "REPAIR", "repair-scheduled")
    with k.db.uow(write=False) as r:
        event = deepcopy(r.one("events", {"event_version_id": first["receipt"]["accepted_event_refs"][0]["event_version_id"]})["payload"])
    event["event_version_id"], event["representation_id"] = str(uuid4()), str(uuid4())
    task = create_bound_task(k, f["principal"], repair, point(100))
    with pytest.raises(DomainError) as error:
        k.propose(f["principal"], task["task_id"], task["generation"], "REPAIR_EVENT",
                  {"occurrence_id": event["occurrence_id"], "replacement_event": event,
                   "reason": "This history edit needs registered process revalidation."}, "unsafe-schedule-copy")
    assert error.value.code == "SCHEDULE_REPAIR_REQUIRED"
    assert revision(k, repair)["selection"] == revision(k, f["world_ref"])["selection"]


def test_new_registered_influence_invalidates_existing_guarded_certificate(kernel_env):
    k = kernel_env["kernel"]
    initial = load_fixture(k, "white_bridge")
    prepared = prepare_command(k, initial["principal"], initial["world_ref"], "RESOURCE_TRANSFER",
        {"from_account": initial["ids"]["warehouse"], "to_account": initial["ids"]["relief"],
         "resource_id": initial["ids"]["grain"], "quantity": "10", "unit": "bag", "at": point(31)},
        mode="DEPENDENCY_GUARDED")
    process = process_definition(k, initial)
    f, _ = register(k, initial, process)
    with pytest.raises(DomainError) as error:
        k.accept(f["principal"], prepared["request"])
    assert error.value.code == "DEPENDENCY_CONFLICT"
    assert revision(k, f["world_ref"])["selection"] == {}


def test_actor_impersonation_and_sealed_overwrite_rejected(kernel_env):
    k = kernel_env["kernel"]
    initial = load_fixture(k, "white_bridge")
    process = process_definition(k, initial)
    f, _ = register(k, initial, process)
    service, interaction, grants, _tasks, actions = group(k, f, process)
    a, b = process["required_actor_ids"][:2]
    with pytest.raises(DomainError) as error:
        service.submit_action(grants[a]["principal"], interaction["interaction_id"], actions[b])
    assert error.value.code == "VIEW_DENIED"
    edited = deepcopy(actions[a])
    edited["intent"] = "Change sealed intent after learning other outcomes."
    with pytest.raises(DomainError) as error:
        service.submit_action(grants[a]["principal"], interaction["interaction_id"], edited)
    assert error.value.code == "IDEMPOTENCY_MISMATCH"
    _, args = selected(k, f, process, service, interaction)
    edited_args = deepcopy(args)
    edited_args["commands"][0]["args"]["quantity"] = "31"
    with pytest.raises(DomainError) as error:
        prepare_command(k, f["principal"], f["world_ref"], "JOINT_INTERACTION", edited_args)
    assert error.value.code == "INVALID_EVIDENCE"


@pytest.mark.parametrize("return_order", list(permutations(range(3))))
def test_all_six_actor_completion_orders_preserve_selected_joint_world(kernel_env, return_order):
    k = kernel_env["kernel"]
    initial = load_fixture(k, "white_bridge")
    process = process_definition(k, initial)
    f, _ = register(k, initial, process)
    actor_order = [process["required_actor_ids"][i] for i in return_order]
    service, interaction, _grants, _tasks, _actions = group(k, f, process, order=actor_order)
    _, args = selected(k, f, process, service, interaction)
    accepted = run_command(k, f["principal"], f["world_ref"], "JOINT_INTERACTION", args)
    current = revision(k, accepted["world_ref"])
    assert resource_at(current["snapshot"], f["ids"]["warehouse"], f["ids"]["grain"], "bag", point(31))["quantity"] == "70"
    assert resource_at(current["snapshot"], f["ids"]["relief"], f["ids"]["grain"], "bag", point(31))["quantity"] == "30"
    assert len(current["selection"]) == 1
    assert len(state_from_revision(current)["closed_interactions"]) == 1


def test_registered_deterministic_delivery_is_due_evaluation_not_advance_side_effect(kernel_env):
    k = kernel_env["kernel"]
    initial = load_fixture(k, "white_bridge")
    ids = initial["ids"]
    process = process_definition(k, initial, name="delivery-recipe", scopes=[ids["warehouse"], ids["relief"]])
    process.update(evaluator_kind="DETERMINISTIC", required_actor_ids=[], participant_ids=[], process_kind="TRAVEL",
        deterministic_commands=[{"template_id": "RESOURCE_TRANSFER", "args": {
            "from_account": ids["warehouse"], "to_account": ids["relief"], "resource_id": ids["grain"],
            "quantity": "25", "unit": "bag", "at": point(30)}}])
    f, _ = register(k, initial, process)
    service = WorldSchedulerService(k)
    chosen = service.evaluate_due(f["principal"], f["world_ref"], point(30), process["scope_ids"], "recipe-evaluate")
    assert revision(k, f["world_ref"])["selection"] == {}
    with k.db.uow(write=False) as r:
        bundle = r.one("simulation_interactions", {"interaction_id": chosen["interaction_id"]})
        outcome = k.artifact(r, chosen["outcome_ref"], f["world_ref"]["work_id"])["payload"]
    from novel_factory.application.simulation import outcome_command
    args = outcome_command(bundle["artifact_ref"], bundle["payload"], chosen["outcome_ref"], outcome)
    accepted = run_command(k, f["principal"], f["world_ref"], "JOINT_INTERACTION", args)
    assert resource_at(revision(k, accepted["world_ref"])["snapshot"], ids["relief"], ids["grain"], "bag", point(31))["quantity"] == "25"
    assert service.processes(f["principal"], accepted["world_ref"])[0]["world_stage"] == "COMPLETED"


def test_deterministic_blocked_route_is_false_trigger_and_cannot_be_relabelled_success(kernel_env):
    k = kernel_env["kernel"]
    initial = load_fixture(k, "white_bridge")
    ids = initial["ids"]
    block = run_command(k, initial["principal"], initial["world_ref"], "SET_RELATION",
        {"subject_id": ids["north"], "predicate": "blocked_by", "object_value": {"kind": "ENTITY", "entity_id": ids["blocker"]}, "at": point(20)})
    initial = {**initial, "world_ref": block["world_ref"]}
    process = process_definition(k, initial, name="travel-recipe", scopes=[ids["traveler"]], start=21)
    recipe = {"template_id": "PASS_CLEAR_ROUTE", "args": {"traveler_id": ids["traveler"], "region_id": ids["north"],
                                                           "destination_id": ids["south"], "at": point(30)}}
    process.update(evaluator_kind="DETERMINISTIC", required_actor_ids=[], participant_ids=[ids["traveler"]],
                   process_kind="TRAVEL", deterministic_commands=[recipe])
    f, _ = register(k, initial, process)
    service = WorldSchedulerService(k)
    chosen = service.evaluate_due(f["principal"], f["world_ref"], point(30), process["scope_ids"], "blocked-recipe")
    with k.db.uow(write=False) as r:
        bundle = r.one("simulation_interactions", {"interaction_id": chosen["interaction_id"]})
        outcome = k.artifact(r, chosen["outcome_ref"], f["world_ref"]["work_id"])["payload"]
    assert outcome["commands"] == []
    assert outcome["process_outcomes"][0]["outcome"] == "CONDITION_FALSE"
    from novel_factory.application.simulation import outcome_command
    args = outcome_command(bundle["artifact_ref"], bundle["payload"], chosen["outcome_ref"], outcome)
    accepted = run_command(k, f["principal"], f["world_ref"], "JOINT_INTERACTION", args)
    assert revision(k, accepted["world_ref"])["selection"] == revision(k, f["world_ref"])["selection"]
    assert service.processes(f["principal"], accepted["world_ref"])[0]["world_stage"] == "SUSPENDED"
    # World suspension is an accepted failed condition; a paused process has
    # no continuing unknown trigger until an explicitly registered new recipe.
    later = run_command(k, f["principal"], accepted["world_ref"], "ADVANCE_SCOPE",
                        {"scope_id": ids["traveler"], "target": point(31)})
    assert later["world_ref"] != accepted["world_ref"]
