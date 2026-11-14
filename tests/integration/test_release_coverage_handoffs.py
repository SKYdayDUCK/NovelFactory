"""Exact predecessor manuscripts remain usable after accepted pure coverage."""
from copy import deepcopy

import pytest
from test_multiscene_coverage import close_accept, two_scenes, uid, write, writer_task

from novel_factory.application.narrative import NarrativeService
from novel_factory.application.production_pipeline import _artifact
from novel_factory.application.release import ReleaseService, _load_acceptance
from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import point, run_command

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def test_covered_predecessor_keeps_fixed_source_writer_and_accepts_successor(kernel_env):
    kernel = kernel_env["kernel"]
    f = two_scenes(kernel, dependent=True)
    first = close_accept(kernel, f, write(kernel, f, 0), f["world_ref"])
    release = ReleaseService(kernel)
    ready = release.ready_scenes(f["principal"], f["batch_ref"], [first["acceptance_ref"]])
    assert ready["world_ref"] == f["world_ref"]
    assert ready["ready_scene_ids"] == [f["scenes"][1]["scene_contract"]["scene_id"]]
    with kernel.db.uow(write=False) as repo:
        original = kernel.artifact(repo, first["acceptance_ref"], f["world_ref"]["work_id"])["payload"]
        # The exception does not extend to normal release/adoption callers.
        with pytest.raises(DomainError, match="Narrative acceptance uses a different adopted world cut"):
            _load_acceptance(repo, kernel, f["principal"], first["acceptance_ref"], f["world_ref"])
    principal, task = writer_task(kernel, f, 1, [first["acceptance_ref"]])
    binding_ref = release.bind_writer_task(principal, task["task_id"], task["generation"], f["scene_refs"][1],
                                         [first["acceptance_ref"]], uid())
    text = "工程第二场的窗纸又响了一声。"
    draft_ref = NarrativeService(kernel).store_draft(principal, task["task_id"], task["generation"],
        f["scene_refs"][1], text, uid(), handoff_refs=[first["acceptance_ref"]])
    second = close_accept(kernel, f, {"draft_ref": draft_ref, "text": text, "scene_ref": f["scene_refs"][1],
        "scene": f["scenes"][1], "task": task}, first["world_ref"])
    assert second["status"] == "CLEAR" and second["world_ref"] != first["world_ref"]
    with kernel.db.uow(write=False) as repo:
        binding = kernel.artifact(repo, binding_ref, f["world_ref"]["work_id"])["payload"]
        draft = kernel.artifact(repo, draft_ref, f["world_ref"]["work_id"])["payload"]
        assert binding["world_ref"] == draft["input_world_ref"] == f["world_ref"]
        assert binding["predecessor_acceptance_refs"] == draft["handoff_refs"] == [first["acceptance_ref"]]
        assert kernel.artifact(repo, first["acceptance_ref"], f["world_ref"]["work_id"])["payload"] == original
        assert repo.one("tasks", {"task_id": task["task_id"]})["state"] == "ACCEPTED"


@pytest.mark.parametrize("change", ["non_schedule", "canon"])
def test_predecessor_alignment_cannot_authorize_a_non_coverage_world_change(kernel_env, change):
    kernel = kernel_env["kernel"]
    f = two_scenes(kernel, dependent=True)
    first = write(kernel, f, 0)
    if change == "canon":
        changed = run_command(kernel, f["principal"], f["world_ref"], "TRANSFER_ITEM", {
            "item_id": f["ids"]["sword"], "from_id": f["ids"]["smith"], "to_id": f["ids"]["protagonist"],
            "transfer_kind": "GIFT", "at": point(1, 2)})["world_ref"]
    else:
        changed = run_command(kernel, f["principal"], f["world_ref"], "ADVANCE_SCOPE", {
            "scope_id": f["ids"]["sword"], "target": point(2)})["world_ref"]
    accepted = close_accept(kernel, f, first, changed)
    assert accepted["status"] == "CLEAR"  # Real text alignment alone is insufficient.
    with pytest.raises(DomainError) as incompatible:
        ReleaseService(kernel).ready_scenes(f["principal"], f["batch_ref"], [accepted["acceptance_ref"]])
    assert incompatible.value.code == "NARRATIVE_ALIGNMENT_REQUIRED"


def test_coverage_handoff_requires_formal_acceptance_and_fixed_task_material(kernel_env):
    kernel = kernel_env["kernel"]
    f = two_scenes(kernel, dependent=True)
    first = close_accept(kernel, f, write(kernel, f, 0), f["world_ref"])
    with kernel.db.uow() as repo:
        fake = deepcopy(kernel.artifact(repo, first["acceptance_ref"], f["world_ref"]["work_id"])["payload"])
        fake["acceptance_id"] = uid()
        unaccepted = _artifact(kernel, repo, f["world_ref"]["work_id"], f["principal"], "NARRATIVE_ACCEPTANCE", fake)
    with pytest.raises(DomainError) as no_receipt:
        ReleaseService(kernel).ready_scenes(f["principal"], f["batch_ref"], [unaccepted])
    assert no_receipt.value.code == "NARRATIVE_ALIGNMENT_REQUIRED"
    principal, task = writer_task(kernel, f, 1)
    with pytest.raises(DomainError) as no_material:
        ReleaseService(kernel).bind_writer_task(principal, task["task_id"], task["generation"], f["scene_refs"][1],
                                               [first["acceptance_ref"]], uid())
    assert no_material.value.code == "TASK_REBIND_REQUIRED"
