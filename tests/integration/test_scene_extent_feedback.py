"""Current length repair must not inherit stale assertions from another draft."""
from copy import deepcopy
from uuid import uuid4

import pytest
from test_production_strategy import drive, synthetic_production
from test_release import runtime_grant, setup_batch, task_for, write_scene

from novel_factory.application.quality_memory import PAUSED
from novel_factory.application.scene_revision import (
    repair_scene_extent,
    restore_unsent_extent_source,
    resume_extent_revision,
)
from novel_factory.application.world_coverage_recovery import prove_pure_coverage
from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import point, run_command

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def legacy_feedback(kernel, owner, work):
    reference = kernel.put_artifact(owner, work, {"diagnostic": "historical report"}, "EVIDENCE")
    findings = [{"reason": "Historical conflict or uncertain important text requires revision or review",
                 "passage": f"Stale synthetic finding {index}"} for index in range(35)]
    return {"report_ref": reference, "findings": findings}


def test_new_measured_length_repair_archives_35_old_findings_without_reusing_them(kernel_env):
    kernel = kernel_env["kernel"]
    f = setup_batch(kernel, count=1)
    owner, wr = f["principal"], f["world_ref"]
    draft = write_scene(kernel, f, 0, "字")
    legacy = legacy_feedback(kernel, owner, wr["work_id"])
    writer = {"stage": "draft", "state": "ENROLLED", "task_id": draft["task"]["task_id"],
              "generation": draft["task"]["generation"], "revision_count": 0,
              "han_characters": {"min": 6, "max": 10}, "revision_feedback": legacy}
    board = {"world_ref": wr, "production_mode": "quality"}
    with kernel.objects.lock(), kernel.db.uow() as repo:
        task = repo.one("tasks", {"task_id": draft["task"]["task_id"]})
        assert repair_scene_extent(kernel.in_transaction(repo), repo, owner, board, writer, task,
            {"attempt_id": str(uuid4())}, draft["draft_ref"], draft["text"])
    fresh = writer["revision_feedback"]
    assert fresh["findings"] == [{"actual_han": 1, "target_han": {"min": 6, "max": 10}}]
    assert fresh["source_draft_ref"] == draft["draft_ref"] and fresh["report_ref"] != legacy["report_ref"]
    assert fresh["historical_report_refs"] == [legacy["report_ref"]]
    assert writer["history"][-1]["revision_feedback"] == legacy and len(legacy["findings"]) == 35


@pytest.mark.parametrize("split_cut", [False, True])
def test_length_plateau_diagnosis_receives_only_current_measurement_not_stale_semantic_feedback(kernel_env, monkeypatch, split_cut):
    kernel = kernel_env["kernel"]
    gateway, owner, wr, _, _ = synthetic_production(kernel, monkeypatch, lengths=[1, 5, 3, 2])
    board, _ = drive(kernel, gateway, owner, wr,
        until=lambda b: any(node.get("reason") == "STRATEGY_REASSESSMENT_REQUIRED" for node in b["nodes"].values()))
    target = next(key for key, node in board["nodes"].items() if node["stage"] == "draft")
    original_writer = deepcopy(board["nodes"][target])
    if split_cut:
        head = run_command(kernel, owner, wr, "SCHEDULE_ADVANCE", {
            "scope_ids": board["subject_ids"], "target": point(2, 1)})["world_ref"]
        with kernel.db.uow(write=False) as repo:
            board.update(world_ref=head, writer_source_world_ref=wr,
                         coverage_registration_proof=prove_pure_coverage(kernel, repo, wr, head))
        board["nodes"][target]["world_ref"] = wr
        proof = board.pop("coverage_registration_proof")
        with kernel.db.uow(write=False) as repo, pytest.raises(DomainError) as error:
            resume_extent_revision(kernel, repo, owner, board, target)
        assert error.value.code == "INVALID_EVIDENCE"
        board["coverage_registration_proof"] = proof
    old = legacy_feedback(kernel, owner, wr["work_id"])
    board["nodes"][target]["revision_feedback"] = old
    with kernel.objects.lock(), kernel.db.uow() as repo:
        assert resume_extent_revision(kernel.in_transaction(repo), repo, owner, board, target)
    diagnosis = next(node for node in board["nodes"].values() if node["stage"] == "reassessment")
    request = diagnosis["reassessment_request"]
    assert request["review_stage"] == "extent"
    assert request["findings"] == [{"actual_han": 2, "target_han": {"min": 6, "max": 10}}]
    assert request["report_ref"] != old["report_ref"]
    assert len(board["nodes"][target]["revision_feedback"]["findings"]) == 35
    assert request["source_draft_ref"] == board["nodes"][target]["output_ref"]
    # Inject an actual unsent paused task, matching the legacy Host interruption
    # that restore_unsent_extent_source handles, then recover the latest stored body.
    with kernel.db.uow(write=False) as repo:
        origin = kernel.artifact(repo, original_writer["output_ref"], wr["work_id"])["payload"]
    fixture = {"principal": owner, "world_ref": wr, "ids": {"sword": board["subject_ids"][-1]}}
    principal = runtime_grant(kernel, fixture, "writer")
    unsent = task_for(kernel, fixture, principal, [original_writer["scene_ref"]], story_time=point(2, 1))
    current = {key: original_writer[key] for key in ("task_id", "generation", "output_ref")}
    current.update(reason=PAUSED, state="STOPPED")
    restored_writer = deepcopy(original_writer)
    restored_writer.update(state="ENROLLED", task_id=unsent["task_id"], generation=unsent["generation"],
        prior_draft_ref=restored_writer["history"][0]["output_ref"], history=[*restored_writer["history"], current])
    if split_cut:
        restored_writer["world_ref"] = wr
    restored_board = {**board, "nodes": {target: restored_writer}}
    with kernel.db.uow() as repo:
        repo.change("tasks", {"task_id": unsent["task_id"]}, {"state": "PAUSED", "lease_owner": None, "lease_expiry": None})
        task = repo.one("tasks", {"task_id": unsent["task_id"]})
        assert restore_unsent_extent_source(kernel, repo, restored_board, target, task)
        assert restored_writer["output_ref"] == original_writer["output_ref"]
        assert restored_writer["task_id"] == original_writer["task_id"] and restored_writer["state"] == "STOPPED"
        assert not repo.many("call_attempts", {"task_id": unsent["task_id"]})
        assert origin["input_world_ref"] == wr
