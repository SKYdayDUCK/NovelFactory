"""A formally closed reading cut survives a returned book's complete child edition."""
import pytest
import test_native_final_book as final_fixture
from test_production_verification import coverage_fixture, drive_coverage

from novel_factory.application import rolling_production

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def test_covered_frozen_parent_can_return_then_accept_and_freeze_child(kernel_env, monkeypatch):
    kernel = kernel_env["kernel"]
    captured = {}

    def production(bound, patch, **_options):
        gateway, owner, source, calls, diagnoses, observed = coverage_fixture(bound, patch)
        _, cursor = drive_coverage(bound, gateway, owner, source)
        assert cursor["state"] == "FROZEN" and len(observed) == 2
        adopted = observed[1]["case"]["world_ref"]
        assert adopted != source
        captured.update(source=source, adopted=adopted, parent=cursor["last_checkpoint"], owner=owner)
        return gateway, owner, source, calls, diagnoses

    monkeypatch.setattr(final_fixture, "synthetic_production", production)
    final_fixture.test_native_editing_blind_reading_restart_and_final_freeze(
        kernel_env, monkeypatch, "book_rework")
    source, adopted = captured["source"], captured["adopted"]
    with kernel.db.uow(write=False) as repo:
        cursor = rolling_production._load(kernel, repo, source["work_id"], source["branch_id"])
        checkpoint = kernel.artifact(repo, cursor["last_checkpoint"], source["work_id"], kind="ROLLING_CHECKPOINT")["payload"]
        assert checkpoint["parent_checkpoint"] == captured["parent"]
        edition = kernel.artifact(repo, checkpoint["edition_ref"], source["work_id"], kind="P6_RETURNED_EDITION_CANDIDATE")["payload"]
        child = edition["candidates"][0]
        assert child["writer_source_world_ref"] == source and child["adopted_world_ref"] == adopted
        draft = kernel.artifact(repo, child["draft_ref"], source["work_id"], kind="NARRATIVE_DRAFT")["payload"]
        assert draft["input_world_ref"] == source
        parent = kernel.artifact(repo, captured["parent"], source["work_id"], kind="ROLLING_CHECKPOINT")["payload"]
        old_scene = parent["chapters"][0]["scenes"][0]
        assert draft["prior_draft_ref"] == old_scene["draft_ref"]
        for item in (old_scene, checkpoint["chapters"][0]["scenes"][0]):
            accepted = kernel.artifact(repo, item["acceptance_ref"], source["work_id"], kind="NARRATIVE_ACCEPTANCE")["payload"]
            alignment = kernel.artifact(repo, accepted["alignment_ref"], source["work_id"], kind="ALIGNMENT_REPORT")["payload"]
            assert accepted["world_ref"] == alignment["to_world_ref"] == adopted
            assert accepted["world_effects_applied"] == 0
        jobs = [row for row in repo.many("supervisor_jobs", {})
                if row["config"].get("returned_edition", {}).get("parent_session") == edition["parent_session"]]
        assert {job["config"]["returned_edition"]["phase"] for job in jobs} == {"write", "epistemic", "facts", "read"}
        for job in jobs:
            task = repo.one("tasks", {"task_id": job["task_id"]})
            spec = repo.one("task_inputs", {"task_id": task["task_id"], "generation": task["generation"]})["spec"]
            phase = job["config"]["returned_edition"]["phase"]
            assert task["state"] == "ACCEPTED" and spec["world_ref"] == (source if phase == "write" else adopted)
            if phase == "write":
                assert job["config"]["returned_writer_binding"]["parent_acceptance_ref"] == old_scene["acceptance_ref"]
            else:
                assert job["config"]["required_review_mcp"]
        assert kernel.head(repo, source["work_id"], source["branch_id"]) == adopted
