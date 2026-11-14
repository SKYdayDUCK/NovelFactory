"""Formal empty-prefix closure and separate reading cuts on real PostgreSQL 17.

Pending-query proof is synthetic at its private verification port; these tests
do not claim to exercise the independently tested native MCP/host broker.
"""
from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

import pytest
from test_narrative import arrange, assertion, draft_for, extract
from test_release import runtime_grant, task_for
from test_simulation import process_definition, register

from novel_factory.application import world_coverage_recovery as coverage
from novel_factory.application.narrative import NarrativeService
from novel_factory.application.planning import PlanningService
from novel_factory.application.production_pipeline import _save
from novel_factory.application.semantic_dependencies import check_task_dependencies
from novel_factory.domain.canonical import fingerprint
from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import point, run_command

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


@pytest.mark.parametrize("mutation", [None, "field", "missing", "member", "intra_order", "selection", "interpretation"])
def test_canon_equivalence_only_allows_inter_entity_history_order(mutation):
    source = {"snapshot": {"entities": {}, "lifecycle_history": [
        {"entity_id": "a", "lifecycle": "ACTIVE", "at": point(0)},
        {"entity_id": "b", "lifecycle": "ACTIVE", "at": point(0)},
        {"entity_id": "a", "lifecycle": "DEAD", "at": point(1)}]},
        "selection": {}, "interpretation_ref": "same"}
    target = deepcopy(source)
    history = target["snapshot"]["lifecycle_history"]
    target["snapshot"]["lifecycle_history"] = [history[1], history[0], history[2]]
    if mutation == "field":
        target["snapshot"]["entities"]["new"] = {}
    elif mutation == "missing":
        target["snapshot"].pop("lifecycle_history")
    elif mutation == "member":
        target["snapshot"]["lifecycle_history"][1]["at"] = point(8)
    elif mutation == "intra_order":
        target["snapshot"]["lifecycle_history"] = [history[2], history[1], history[0]]
    elif mutation == "selection":
        target["selection"]["new"] = "event"
    elif mutation == "interpretation":
        target["interpretation_ref"] = "other"
    assert coverage._same_canon(source, target) is (mutation is None)
    assert source["snapshot"]["lifecycle_history"][0]["at"] == point(0)


def setup(kernel, monkeypatch, *, blocked=False):
    f = arrange(kernel)
    if blocked:
        process = process_definition(kernel, f, scopes=[f["ids"]["sword"]],
                                     actors=[f["ids"]["protagonist"]], start=2, due=2)
        f, _ = register(kernel, f, process)
        # Plan and scene must be prepared on the registered process world.
        f = arrange(kernel, f)
    item = draft_for(kernel, f, "测试原稿中的物件。", interval={"start": point(2), "end": point(2, 1), "bounds": "[)"})
    old = extract(kernel, f, item, [assertion(item["text"])])
    with kernel.db.uow(write=False) as repo:
        grant = repo.one("grants", {"grant_id": f["principal"].grant_id})
        deadline = (repo.now() + timedelta(hours=1)).isoformat()
    target = "draft:" + item["scene"]["scene_contract"]["scene_id"]
    board = {"world_ref": f["world_ref"], "volume_id": f["plan"]["volume_id"], "series_id": str(uuid4()),
             "plan_ref": f["plan_ref"], "subject_ids": [f["ids"]["sword"]], "predicates": ["owner"],
             "authorization": {"grant_id": f["principal"].grant_id, "principal_id": f["principal"].principal_id,
                               "epoch": grant["epoch"], "policy_version": "p1-policy-1", "deadline": deadline},
             "nodes": {target: {"stage": "draft", "output_ref": item["draft_ref"], "scene_ref": item["scene_ref"],
                                "task_id": item["task"]["task_id"], "generation": item["task"]["generation"],
                                "revision_count": 7, "repair_allowance": {"revision_baseline": 3}}}}
    with kernel.db.uow() as repo:
        board["authorization"]["policy_version"] = repo.one("works", {"work_id": f["world_ref"]["work_id"]})["policy_version"]
        _save(kernel, repo, f["principal"], board)
    case = {"world_ref": f["world_ref"], "case_hash": "c" * 64}
    pending = [{"selector": {"subject_id": f["ids"]["sword"], "predicate": "owner", "plane": "WORLD", "at": point(2)}}]
    # Trusted pending evidence is supplied by the verification port in this
    # isolated engineering fixture. No real model or native receipt is forged.
    monkeypatch.setattr(coverage, "_pending_queries", lambda *args: (case, {}, pending, item["draft_ref"]))
    return f, board, target, item, old, pending


def planned(kernel, f, board, target):
    with kernel.db.uow(write=False) as repo:
        return coverage.plan_coverage(kernel, repo, f["principal"], board, target,
                                      parent_case_hash="c" * 64, query_receipt_refs=[])


def test_empty_coverage_accepts_normally_and_replays_without_source_mutation(kernel_env, monkeypatch):
    kernel = kernel_env["kernel"]
    f, board, target, item, old, _ = setup(kernel, monkeypatch)
    before = deepcopy(board)
    request = planned(kernel, f, board, target)
    assert request["status"] == "READY_FOR_CLOSURE" and request["target_frontier"] == point(2, 1)
    with kernel.db.uow(write=False) as repo:
        source = deepcopy(kernel.revision(repo, f["world_ref"]))
    result = coverage.recover_coverage(kernel, f["principal"], request, "coverage")
    assert result["provider_calls_sent"] == result["world_effects_applied"] == 0
    assert result["allowance_changed"] is False and board == before
    assert coverage.recover_coverage(kernel, f["principal"], request, "coverage") == result
    with kernel.db.uow(write=False) as repo:
        revision = kernel.revision(repo, result["reading_world_ref"])
        assert coverage._same_canon(source, revision)
        assert revision["selection"] == source["selection"]
        assert revision["frontiers"][f["ids"]["sword"]] == point(2, 1)
        assert coverage.validate_reading_world(kernel, repo, board, {
            "parents": [target], "reading_world_ref": result["reading_world_ref"],
            "coverage_transition_ref": result["coverage_transition_ref"]}) == result["reading_world_ref"]
        check_task_dependencies(repo, item["task"]["task_id"], item["task"]["generation"])
    # Same immutable body, new task/reading scope and explicit authoritative alignment.
    service = NarrativeService(kernel)
    refreshed = {**f, "world_ref": result["reading_world_ref"]}
    reader = runtime_grant(kernel, refreshed, "reviewer")
    task = task_for(kernel, refreshed, reader, [item["draft_ref"]], "extract", story_time=point(2, 1))
    service.bind_reader_task(f["principal"], task["task_id"], task["generation"], [item["draft_ref"]], "new-reading")
    assert service.read_text(reader, task["task_id"], task["generation"], item["draft_ref"])["text"] == item["text"]
    aligned = service.build_alignment(f["principal"], item["draft_ref"], old["extraction_ref"], result["reading_world_ref"], "alignment")
    assert aligned["status"] == "CLEAR"


@pytest.mark.parametrize("immutable_lineage", [True, False])
def test_reading_renewal_requires_actual_immutable_draft_lineage(kernel_env, monkeypatch, immutable_lineage):
    kernel = kernel_env["kernel"]
    f, board, target, item, _, _ = setup(kernel, monkeypatch)
    result = coverage.recover_coverage(kernel, f["principal"], planned(kernel, f, board, target), "coverage")
    service = NarrativeService(kernel)
    writer = runtime_grant(kernel, f, "writer")
    task = task_for(kernel, f, writer, [item["scene_ref"], f["plan_ref"], item["draft_ref"]],
                    story_time=point(2, 1))
    PlanningService(kernel).bind_task_for_controller(f["principal"], task["task_id"], task["generation"],
        f["plan_ref"], item["scene"]["plan_node_ids"], "bind-revision:" + task["task_id"])
    revised = service.store_draft(writer, task["task_id"], task["generation"], item["scene_ref"],
        "仅用于测试的新稿。", "revision", prior_draft_ref=item["draft_ref"] if immutable_lineage else None)
    node = board["nodes"][target]
    node.update(output_ref=revised, task_id=task["task_id"], generation=task["generation"],
                revision_count=node["revision_count"] + 1, prior_draft_ref=item["draft_ref"])
    with kernel.db.uow() as repo:
        prior = kernel.artifact(repo, result["coverage_transition_ref"], f["world_ref"]["work_id"])["payload"]
        if not immutable_lineage:
            with pytest.raises(DomainError, match="immutable prior-draft lineage"):
                coverage.renew_reading_transition(kernel, repo, f["principal"], board, target=target,
                    prior_transition_ref=result["coverage_transition_ref"], idempotency_key="renew")
            return
        renewed = coverage.renew_reading_transition(kernel, repo, f["principal"], board, target=target,
            prior_transition_ref=result["coverage_transition_ref"], idempotency_key="renew")
        assert renewed == coverage.renew_reading_transition(kernel, repo, f["principal"], board, target=target,
            prior_transition_ref=result["coverage_transition_ref"], idempotency_key="renew")
        assert renewed["coverage_transition_ref"] != result["coverage_transition_ref"]
        assert renewed["reading_world_ref"] == result["reading_world_ref"]
        assert renewed["world_revision_changed"] is False and renewed["allowance_changed"] is False
        assert kernel.artifact(repo, result["coverage_transition_ref"], f["world_ref"]["work_id"])["payload"] == prior
        assert coverage.validate_reading_world(kernel, repo, board, {"parents": [target], **renewed}) == result["reading_world_ref"]
        with pytest.raises(DomainError, match="source identity"):
            coverage.validate_reading_world(kernel, repo, board, {"parents": [target], **result})


def test_semantic_blocker_is_returned_without_skipping_actor_decision(kernel_env, monkeypatch):
    kernel = kernel_env["kernel"]
    f, board, target, _, _, _ = setup(kernel, monkeypatch, blocked=True)
    request = planned(kernel, f, board, target)
    assert request["status"] == "WAITING_WORLD_PROCESS"
    result = coverage.recover_coverage(kernel, f["principal"], request, "blocked")
    assert result["status"] == "WAITING_WORLD_PROCESS" and result["blockers"]
    with kernel.db.uow(write=False) as repo:
        assert kernel.head(repo, f["world_ref"]["work_id"], f["world_ref"]["branch_id"]) == f["world_ref"]


def test_old_query_cut_can_adopt_existing_pure_coverage_acceptance(kernel_env, monkeypatch):
    kernel = kernel_env["kernel"]
    f, board, target, _, _, _ = setup(kernel, monkeypatch)
    result = coverage.recover_coverage(kernel, f["principal"], planned(kernel, f, board, target), "first")
    request = planned(kernel, f, board, target)
    assert request["parent_query_world_ref"] == board["world_ref"]
    assert request["source_world_ref"] == result["reading_world_ref"]
    assert request["status"] == "ALREADY_COVERED" and request["scope_ids"] == []
    adopted = coverage.recover_coverage(kernel, f["principal"], request, "adopt")
    assert adopted["world_revision_changed"] is False
    assert adopted["reading_world_ref"] == result["reading_world_ref"]
    with kernel.db.uow(write=False) as repo:
        assert coverage.validate_reading_world(kernel, repo, board, {"parents": [target], **adopted}) == result["reading_world_ref"]


def test_old_query_cut_does_not_silently_adopt_canonical_changes(kernel_env, monkeypatch):
    kernel = kernel_env["kernel"]
    f, board, target, _, _, _ = setup(kernel, monkeypatch)
    changed = run_command(kernel, f["principal"], f["world_ref"], "TRANSFER_ITEM", {
        "item_id": f["ids"]["sword"], "from_id": f["ids"]["smith"], "to_id": f["ids"]["protagonist"], "transfer_kind": "GIFT", "at": point(1, 2)})
    request = planned(kernel, f, board, target)
    assert request["status"] == "WAITING_WORLD_REVALIDATION"
    result = coverage.recover_coverage(kernel, f["principal"], request, "wait-real-facts")
    assert result["status"] == "WAITING_WORLD_REVALIDATION" and result["world_effects_applied"] == 0
    with kernel.db.uow(write=False) as repo:
        assert kernel.head(repo, f["world_ref"]["work_id"], f["world_ref"]["branch_id"]) == changed["world_ref"]


def test_registry_change_is_not_mistaken_for_empty_coverage(kernel_env, monkeypatch):
    kernel = kernel_env["kernel"]
    f, board, target, _, _, _ = setup(kernel, monkeypatch)
    process = process_definition(kernel, f, scopes=[f["ids"]["sword"]],
                                 actors=[f["ids"]["protagonist"]], start=2, due=2)
    updated, _ = register(kernel, f, process)
    request = planned(kernel, f, board, target)
    assert request["status"] == "WAITING_WORLD_REVALIDATION"
    assert request["source_world_ref"] == updated["world_ref"]


@pytest.mark.parametrize("tamper", ["acceptance", "closures", "request_ref", "raw_hash"])
def test_reading_cut_rechecks_actual_acceptance_and_immutable_proof(kernel_env, monkeypatch, tamper):
    kernel = kernel_env["kernel"]
    f, board, target, _, _, _ = setup(kernel, monkeypatch)
    result = coverage.recover_coverage(kernel, f["principal"], planned(kernel, f, board, target), "coverage")
    with kernel.db.uow() as repo:
        payload = deepcopy(kernel.artifact(repo, result["coverage_transition_ref"], f["world_ref"]["work_id"])["payload"])
        if tamper == "acceptance":
            payload["acceptance_key"] = "unaccepted"
        elif tamper == "closures":
            payload["closure_refs"] = []
        elif tamper == "request_ref":
            payload["request_ref"] = result["coverage_transition_ref"]
        else:
            payload["source_snapshot_sha256"] = "0" * 64
        altered = coverage._artifact(kernel, repo, f["principal"], f["world_ref"]["work_id"], payload, coverage.TRANSITION_KIND)
        with pytest.raises(DomainError):
            coverage.validate_reading_world(kernel, repo, board, {"parents": [target], "reading_world_ref": result["reading_world_ref"],
                "coverage_transition_ref": altered})
        with pytest.raises(DomainError):
            coverage.renew_reading_transition(kernel, repo, f["principal"], board, target=target,
                prior_transition_ref=altered, idempotency_key="cannot-refresh-invalid-proof")


@pytest.mark.parametrize("at", [point(1), point(2, 1), point(8)])
def test_scene_interval_restricts_coverage_not_board_horizon(kernel_env, monkeypatch, at):
    kernel = kernel_env["kernel"]
    f, board, target, _, _, pending = setup(kernel, monkeypatch)
    pending[0]["selector"]["at"] = at
    with pytest.raises(DomainError, match="scene interval"):
        planned(kernel, f, board, target)


@pytest.mark.parametrize("mutation", ["source_hash", "branch", "target", "controller"])
def test_tampered_request_and_cross_branch_controller_are_rejected(kernel_env, monkeypatch, mutation):
    kernel = kernel_env["kernel"]
    f, board, target, _, _, _ = setup(kernel, monkeypatch)
    request = planned(kernel, f, board, target)
    if mutation == "source_hash":
        request["source_identity_sha256"] = "0" * 64
    elif mutation == "branch":
        request["source_world_ref"]["branch_id"] = str(uuid4())
    elif mutation == "target":
        request["target_frontier"] = point(8)
    else:
        wrong = runtime_grant(kernel, f, "reviewer")
        with pytest.raises(DomainError):
            coverage.recover_coverage(kernel, wrong, request, "bad")
        return
    request["request_sha256"] = fingerprint({key: value for key, value in request.items() if key != "request_sha256"})
    with pytest.raises(DomainError):
        coverage.recover_coverage(kernel, f["principal"], request, "bad")
