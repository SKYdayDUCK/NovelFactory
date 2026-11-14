"""A completed writer proves its own stored child without reauthorizing a send."""
import pytest
from test_production_strategy import drive, synthetic_production
from test_release import accept_draft, write_scene

from novel_factory.application.candidate_delivery import CandidateDelivery
from novel_factory.application.creative_context_store import load_snapshot
from novel_factory.domain.errors import DomainError

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def test_completed_writer_own_child_and_acceptance_preserve_snapshot_but_foreign_child_does_not(kernel_env, monkeypatch):
    kernel = kernel_env["kernel"]
    gateway, owner, wr, calls, _diagnoses = synthetic_production(
        kernel, monkeypatch, lengths=[1, 7], story_failures=True, harness=True)
    board, _ = drive(kernel, gateway, owner, wr, until=lambda board: any(
        node["stage"] == "draft" and node["state"] == "DRAFT_STORED" and node.get("prior_draft_ref")
        for node in board["nodes"].values()))
    writer = next(node for node in board["nodes"].values() if node["stage"] == "draft")
    with kernel.db.uow(write=False) as repo:
        task = repo.one("tasks", {"task_id": writer["task_id"]})
        audit, candidate = CandidateDelivery(kernel, gateway).audited_candidate(repo, task, production_accounting=True)
        assert task["state"] == "ACCEPTED" and candidate["text"] == "字" * 7
        assert audit["creative_context_ref"]
        # Live eligibility remains strict even though historical output proof
        # now accepts this writer's exact, genuinely stored direct child.
        with pytest.raises(DomainError) as live:
            load_snapshot(kernel, repo, task, audit["input_ref"], required=True)
        assert live.value.code == "TASK_REBIND_REQUIRED"
        scene = kernel.artifact(repo, writer["scene_ref"], wr["work_id"])["payload"]
        plan = kernel.artifact(repo, board["plan_ref"], wr["work_id"])["payload"]
    # Use the actual narrative reader/delivery/extraction/acceptance services.
    fixture = {"principal": owner, "world_ref": wr, "ids": {"protagonist": scene["scene_contract"]["pov_actor_id"]},
               "plan_ref": board["plan_ref"], "plan": plan, "volume_id": board["volume_id"],
               "batch_ref": scene["batch_ref"], "scene_refs": [writer["scene_ref"]], "scenes": [scene]}
    # runtime_grant needs the fixture's full registered identity set.
    from novel_factory.fixtures import fixture_definition
    fixture["ids"] = fixture_definition("r1_sword")["ids"]
    accepted = accept_draft(kernel, fixture, {"draft_ref": writer["output_ref"], "text": candidate["text"]})
    with kernel.db.uow(write=False) as repo:
        task = repo.one("tasks", {"task_id": writer["task_id"]})
        assert CandidateDelivery(kernel, gateway).audited_candidate(repo, task, production_accounting=True)[1] == candidate
        assert accepted["acceptance_ref"]
    foreign = write_scene(kernel, fixture, 0, "独立任务的新稿。", prior_draft_ref=writer["output_ref"])
    assert foreign["draft_ref"] != writer["output_ref"]
    with kernel.db.uow(write=False) as repo, pytest.raises(DomainError) as stale:
        task = repo.one("tasks", {"task_id": writer["task_id"]})
        CandidateDelivery(kernel, gateway).audited_candidate(repo, task, production_accounting=True)
    assert stale.value.code == "TASK_REBIND_REQUIRED"
    assert len([context for context, _, _ in calls if context["role_id"] == "writer"]) == 2
