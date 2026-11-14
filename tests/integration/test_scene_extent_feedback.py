"""Current length repair must not inherit stale assertions from another draft."""
from uuid import uuid4

import pytest
from test_production_strategy import drive, synthetic_production
from test_release import setup_batch, write_scene

from novel_factory.application.scene_revision import repair_scene_extent, resume_extent_revision

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


def test_length_plateau_diagnosis_receives_only_current_measurement_not_stale_semantic_feedback(kernel_env, monkeypatch):
    kernel = kernel_env["kernel"]
    gateway, owner, wr, _, _ = synthetic_production(kernel, monkeypatch, lengths=[1, 5, 3, 2])
    board, _ = drive(kernel, gateway, owner, wr,
        until=lambda b: any(node.get("reason") == "STRATEGY_REASSESSMENT_REQUIRED" for node in b["nodes"].values()))
    target = next(key for key, node in board["nodes"].items() if node["stage"] == "draft")
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
