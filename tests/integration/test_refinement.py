"""P3-02 PostgreSQL proof issuance, reviewed replacement, recovery and isolation."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from uuid import uuid4

import pytest

from novel_factory.application.refinement import WorldRepresentationService
from novel_factory.domain.canonical import fingerprint
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


def accepted_transfer(kernel):
    fixture = load_fixture(kernel, "white_bridge")
    result = run_command(kernel, fixture["principal"], fixture["world_ref"], "RESOURCE_TRANSFER", {
        "from_account": fixture["ids"]["warehouse"], "to_account": fixture["ids"]["relief"],
        "resource_id": fixture["ids"]["grain"], "quantity": "30", "unit": "bag", "at": point(30)})
    return {**fixture, "world_ref": result["world_ref"], "original": result}


def revision(kernel, wr):
    with kernel.db.uow(write=False) as repo:
        return kernel.revision(repo, wr)


def request_for(kernel, fixture, *, split=True):
    wr = fixture["world_ref"]
    with kernel.db.uow(write=False) as repo:
        current = kernel.revision(repo, wr)
        version = next(iter(current["selection"].values()))
        macro = repo.one("events", {"event_version_id": version})["payload"]
    steps = []
    for index, quantity in enumerate(["10", "20"] if split else ["30"]):
        effects = deepcopy(macro["effects"])
        if split:
            for effect in effects:
                assert effect["kind"] == "RESOURCE_DELTA"
                effect["effect_key"] = f"portion:{index}:" + effect["effect_key"]
                effect["quantity"] = ("-" if effect["quantity"].startswith("-") else "") + quantity
        steps.append({"step_id": str(uuid4()), "at": macro["occurrence_time"]["at"],
                      "participant_ids": macro["participants"], "depends_on": [], "effects": effects})
    return {"schema_version": "2.3", "source_world_ref": wr,
            "macro_event_ref": {k: macro[k] for k in ("occurrence_id", "event_version_id")},
            "steps": steps, "idempotency_key": "finite-refinement"}, macro


def reviewed_candidate(kernel, fixture, staged, *, key="replace"):
    principal, wr = fixture["principal"], fixture["world_ref"]
    branch = kernel.fork(principal, wr, "REPAIR", "repair:" + key)
    reviewer = kernel.issue_grant(principal, wr["work_id"], role_id="control_operator",
                                 capabilities=["READ", "APPROVE", "REPAIR"],
                                 branch_ids=[branch["branch_id"]],
                                 planes=["WORLD"], max_story_time=point(100))["principal"]
    task = create_bound_task(kernel, principal, branch, point(100))
    proposal = WorldRepresentationService(kernel).propose_refinement(
        principal, task["task_id"], task["generation"], staged["proof_ref"], "proposal:" + key)
    prepared = prepare_command(kernel, principal, branch, "REPAIR_EVENT", staged["repair_command"]["args"],
                               reviewer=reviewer, key=key, task=task)
    assert prepared["proposal"] == proposal
    return branch, reviewer, prepared


def test_resource_refinement_replays_once_preserves_history_and_needs_independent_review(kernel_env):
    kernel = kernel_env["kernel"]
    fixture = accepted_transfer(kernel)
    wr, principal = fixture["world_ref"], fixture["principal"]
    service = WorldRepresentationService(kernel)
    request, macro = request_for(kernel, fixture)
    before = revision(kernel, wr)
    staged = service.prepare_refinement(principal, request)
    assert service.prepare_refinement(principal, request) == staged
    assert revision(kernel, wr) == before
    branch, reviewer, prepared = reviewed_candidate(kernel, fixture, staged)
    assert prepared["proposal"]["approval_id"] is None
    with pytest.raises(DomainError) as forbidden:
        kernel.approve(principal, prepared["proposal"]["proposal_ref"], wr["work_id"], idempotency_key="self-review")
    assert forbidden.value.code == "VIEW_DENIED"

    def fail_before(stage):
        if stage == "before_commit":
            raise RuntimeError("isolated pre-commit interruption")
    with pytest.raises(RuntimeError):
        kernel.accept(principal, prepared["request"], fault=fail_before)
    assert revision(kernel, branch)["selection"] == before["selection"]

    def lose_response(stage):
        if stage == "after_commit":
            raise RuntimeError("commit receipt lost")
    with pytest.raises(RuntimeError):
        kernel.accept(principal, prepared["request"], fault=lose_response)
    receipt = kernel.accept(principal, prepared["request"])
    assert kernel.accept(principal, prepared["request"]) == receipt
    after = revision(kernel, receipt["world_ref"])
    assert len(after["selection"]) == len(before["selection"]) == 1
    assert after["selection"][macro["occurrence_id"]] == staged["replacement_event_ref"]["event_version_id"]
    assert after["frontiers"] == before["frontiers"]
    assert after["provenance"]["history_epoch"] == before["provenance"].get("history_epoch", 0) + 1
    assert after["provenance"]["equivalence_proof_ref"] == staged["proof_ref"]
    assert resource_at(after["snapshot"], fixture["ids"]["warehouse"], fixture["ids"]["grain"], "bag", point(31))["quantity"] == "70"
    assert after["snapshot"]["resources"] == before["snapshot"]["resources"]
    with kernel.db.uow(write=False) as repo:
        assert kernel.head(repo, wr["work_id"]) == wr
        assert repo.one("events", {"event_version_id": macro["event_version_id"]})["payload"] == macro
        proof = kernel.artifact(repo, staged["proof_ref"], wr["work_id"], kind="REFINEMENT_PROOF")["payload"]
        assert proof["proof"]["status"] == "PASS"
        assert proof["impact"]["published_text_replaced"] is False
        assert proof["impact"]["dependencies"]
        assert len(repo.many("revision_events", {"world_revision_id": after["world_revision_id"]})) == 1
    assert kernel.promote(principal, wr, receipt["world_ref"], "promote-refinement") == receipt["world_ref"]
    assert revision(kernel, wr) == before
    assert reviewer.principal_id != principal.principal_id


def test_false_early_delivery_cannot_issue_proof_or_change_formal_world(kernel_env):
    kernel = kernel_env["kernel"]
    fixture = accepted_transfer(kernel)
    service = WorldRepresentationService(kernel)
    request, _macro = request_for(kernel, fixture)
    request["steps"][0]["at"] = point(25)
    before = revision(kernel, fixture["world_ref"])
    with pytest.raises(DomainError) as error:
        service.prepare_refinement(fixture["principal"], request)
    assert error.value.code == "REFINEMENT_NOT_EQUIVALENT"
    with kernel.db.uow(write=False) as repo:
        assert not repo.many("artifacts", {"work_id": fixture["world_ref"]["work_id"], "kind": "REFINEMENT_PROOF"})
        assert kernel.head(repo, fixture["world_ref"]["work_id"]) == fixture["world_ref"]
    assert revision(kernel, fixture["world_ref"]) == before


def test_proof_issuance_is_idempotent_under_real_competing_transactions(kernel_env):
    kernel = kernel_env["kernel"]
    fixture = accepted_transfer(kernel)
    service = WorldRepresentationService(kernel)
    request, _macro = request_for(kernel, fixture)
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(lambda _: service.prepare_refinement(fixture["principal"], request), range(2)))
    assert results[0] == results[1]
    changed = deepcopy(request)
    changed["steps"] = list(reversed(changed["steps"]))
    with pytest.raises(DomainError) as error:
        service.prepare_refinement(fixture["principal"], changed)
    assert error.value.code == "IDEMPOTENCY_MISMATCH"
    with kernel.db.uow(write=False) as repo:
        assert len(repo.many("artifacts", {"work_id": fixture["world_ref"]["work_id"], "kind": "REFINEMENT_PROOF"})) == 1


@pytest.mark.parametrize("attack", ["forged_kind", "changed_effect", "cross_work", "wrong_fork"])
def test_untrusted_or_rebound_proofs_are_rejected(kernel_env, attack):
    kernel = kernel_env["kernel"]
    fixture = accepted_transfer(kernel)
    principal, wr = fixture["principal"], fixture["world_ref"]
    service = WorldRepresentationService(kernel)
    request, _macro = request_for(kernel, fixture)
    staged = service.prepare_refinement(principal, request)
    args = deepcopy(staged["repair_command"]["args"])
    if attack == "forged_kind":
        with kernel.db.uow(write=False) as repo:
            payload = kernel.artifact(repo, staged["proof_ref"], wr["work_id"])["payload"]
        args["equivalence_proof_ref"] = kernel.put_artifact(principal, wr["work_id"], payload, "EVIDENCE")
    elif attack == "changed_effect":
        args["replacement_event"]["effects"][0]["quantity"] = "-11"
        args["replacement_event"]["effects"][1]["quantity"] = "11"
    elif attack == "cross_work":
        other = accepted_transfer(kernel)
        principal, wr = other["principal"], other["world_ref"]
    elif attack == "wrong_fork":
        newer = run_command(kernel, principal, wr, "ADVANCE_SCOPE", {
            "scope_id": fixture["ids"]["warehouse"], "target": point(31)})
        wr = newer["world_ref"]
    branch = kernel.fork(principal, wr, "REPAIR", "untrusted")
    task = create_bound_task(kernel, principal, branch, point(100))
    with pytest.raises(DomainError) as error:
        kernel.propose(principal, task["task_id"], task["generation"], "REPAIR_EVENT", args, "untrusted")
    assert error.value.code in {"INVALID_ARGUMENT", "INVALID_EVIDENCE", "VIEW_DENIED", "HEAD_CONFLICT"}
    with kernel.db.uow(write=False) as repo:
        assert kernel.head(repo, wr["work_id"]) == wr


def test_actor_plane_story_horizon_and_cancelled_generation_cannot_use_proof(kernel_env):
    kernel = kernel_env["kernel"]
    fixture = accepted_transfer(kernel)
    principal, wr = fixture["principal"], fixture["world_ref"]
    service = WorldRepresentationService(kernel)
    request, _macro = request_for(kernel, fixture)
    actor = kernel.issue_grant(principal, wr["work_id"], role_id="character_actor", actor_id=fixture["ids"]["friend"],
                               planes=["BELIEF"], capabilities=["READ"], max_story_time=point(100))["principal"]
    short = kernel.issue_grant(principal, wr["work_id"], role_id="control_operator", planes=["WORLD"],
                               capabilities=["READ", "REPAIR"], max_story_time=point(15))["principal"]
    for identity in (actor, short):
        with pytest.raises(DomainError) as error:
            service.prepare_refinement(identity, request)
        assert error.value.code == "VIEW_DENIED"
    staged = service.prepare_refinement(principal, request)
    branch = kernel.fork(principal, wr, "REPAIR", "cancel-proof")
    task = create_bound_task(kernel, principal, branch, point(100))
    kernel.cancel_task(principal, task["task_id"], task["generation"], "cancel")
    with pytest.raises(DomainError):
        service.propose_refinement(principal, task["task_id"], task["generation"], staged["proof_ref"], "late")


def test_registered_message_schedule_and_three_actor_decision_are_preserved(kernel_env):
    from test_simulation import group, process_definition, register, rules

    from novel_factory.application.simulation import outcome_command

    kernel = kernel_env["kernel"]
    fixture = load_fixture(kernel, "white_bridge")
    ids = fixture["ids"]
    process = process_definition(kernel, fixture, channels=[{
        "channel_id": "news-next-phase", "from_scope_id": ids["warehouse"], "to_scope_id": ids["north"],
        "min_delay_ticks": 1, "earliest_possible_impact": point(31), "completeness": "COMPLETE",
        "evidence_refs": rules(kernel, fixture["world_ref"])}])
    fixture, _registered = register(kernel, fixture, process)
    scheduler, interaction, _grants, _tasks, _actions = group(kernel, fixture, process)
    commands = [{"template_id": "RESOURCE_TRANSFER", "args": {
        "from_account": ids["warehouse"], "to_account": ids[target], "resource_id": ids["grain"],
        "quantity": quantity, "unit": "bag", "at": point(30)}}
        for target, quantity in [("relief", "10"), ("recipient", "10"), ("reserve", "10")]]
    outcome_ref = scheduler.choose_outcome(fixture["principal"], interaction["interaction_id"], commands=commands,
        process_outcomes=[{"process_id": process["process_id"], "outcome": "FIRED", "world_stage": "COMPLETED", "next_due": None}],
        reasoning_summary="Three sealed participants share a fixed 30-bag allocation.",
        rule_evidence_refs=rules(kernel, fixture["world_ref"]), source_kind="FIXTURE_SELECTED")
    with kernel.db.uow(write=False) as repo:
        outcome = kernel.artifact(repo, outcome_ref, fixture["world_ref"]["work_id"])["payload"]
        bundle = repo.one("simulation_interactions", {"interaction_id": interaction["interaction_id"]})
    accepted = run_command(kernel, fixture["principal"], fixture["world_ref"], "JOINT_INTERACTION",
                           outcome_command(bundle["artifact_ref"], bundle["payload"], outcome_ref, outcome))
    fixture = {**fixture, "world_ref": accepted["world_ref"]}
    request, macro = request_for(kernel, fixture, split=False)
    service = WorldRepresentationService(kernel)
    staged = service.prepare_refinement(fixture["principal"], request)
    before = revision(kernel, fixture["world_ref"])
    branch, _reviewer, prepared = reviewed_candidate(kernel, fixture, staged, key="joint")
    receipt = kernel.accept(fixture["principal"], prepared["request"])
    after = revision(kernel, receipt["world_ref"])
    assert after["snapshot"]["resources"] == before["snapshot"]["resources"]
    assert state_from_revision(after) == state_from_revision(before)
    assert state_from_revision(after)["processes"][process["process_id"]]["world_stage"] == "COMPLETED"
    assert len(state_from_revision(after)["closed_interactions"]) == 1
    assert after["provenance"]["refinement_source_event_ref"] == request["macro_event_ref"]
    assert len(after["selection"]) == 1
    with kernel.db.uow(write=False) as repo:
        proof = kernel.artifact(repo, staged["proof_ref"], branch["work_id"])["payload"]
        assert any(cut["at"] == point(31) for cut in proof["proof"]["boundaries"])
        assert proof["proof"]["schedule_sha256"] == fingerprint(state_from_revision(before))
        assert repo.one("events", {"event_version_id": macro["event_version_id"]})["payload"] == macro
