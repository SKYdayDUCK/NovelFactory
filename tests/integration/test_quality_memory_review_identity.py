"""Independent real-MCP reads of one fixed writer cannot alias local records."""
from copy import deepcopy

import pytest
from test_production_verification import coverage_fixture, drive_coverage

from novel_factory.application.production_pipeline import _artifact
from novel_factory.application.quality_memory import (
    _record,
    plan_repair,
    reflect_repair,
    verify_method,
)
from novel_factory.application.review_mcp import require_review_queries
from novel_factory.domain.errors import DomainError

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def test_two_independent_readings_preserve_legacy_record_and_same_reading_replay(kernel_env, monkeypatch):
    kernel = kernel_env["kernel"]
    gateway, owner, wr, _, _, _ = coverage_fixture(kernel, monkeypatch)
    board, cursor = drive_coverage(kernel, gateway, owner, wr)
    assert cursor["state"] == "FROZEN"
    writer = next(node for node in board["nodes"].values() if node["stage"] == "draft")
    readers = [next(node for node in board["nodes"].values() if node["stage"] == stage)
               for stage in ("epistemic", "review")]
    with kernel.objects.lock(), kernel.db.uow() as repo:
        repo.one("works", {"work_id": wr["work_id"]}, lock="update")
        source_call = repo.one("call_attempts", {"task_id": writer["task_id"], "call_key": "supervised-candidate"})
        evidence_row = gateway._read_audit(repo, source_call["attempt_id"])
        evidence = evidence_row["payload"]
        plan = reflect_repair(kernel, repo, owner, wr, writer["task_id"], evidence["attempt_id"],
                              {key: evidence_row[key] for key in ("artifact_id", "artifact_revision_id", "sha256")},
                              plan_repair("editorial", ["Synthetic prior finding"], []), "SYNTHETIC_REVIEW_IDENTITY")
        reviews = []
        for reader in readers:
            task = repo.one("tasks", {"task_id": reader["task_id"]})
            call = repo.one("call_attempts", {"task_id": task["task_id"], "call_key": "supervised-candidate"})
            audit = gateway._read_audit(repo, call["attempt_id"])["payload"]
            assert task["state"] == "ACCEPTED" and require_review_queries(kernel, repo, task, audit)
            payload = {"draft_refs": [writer["output_ref"]], "generation": task["generation"],
                       "task_id": task["task_id"], "candidate_ref": audit["output_ref"], "literary_quality": "NOT_EVALUATED"}
            advice = _artifact(kernel, repo, wr["work_id"], owner, "CHAPTER_READING_ADVICE", payload)
            reviews.append((call["attempt_id"], advice, payload))
        legacy_payload = {"world_ref": wr, "phase": "STAGE_COMPLETED", "attempt_id": evidence["attempt_id"],
                          "error_ref": plan["error_ref"], "actual_fix": writer["output_ref"],
                          "regression_result": reviews[0][1], "status": "LOCAL_VALIDATION_ONLY", "reusable": False}
        legacy = _record(kernel, repo, wr["work_id"], owner.principal_id, evidence["attempt_id"],
                         "reviewed-local-repair", legacy_payload, "P6_REFLECTION_RECORD")
        calls_before = repo.many("call_attempts", {})
        budgets_before = repo.many("budgets", {"work_id": wr["work_id"]})
        for attempt, advice, _ in reviews:
            verify_method(kernel, repo, owner, wr, plan, evidence, "targeted_revision", writer["output_ref"], attempt, advice)
        local = [row for row in repo.many("artifacts", {"work_id": wr["work_id"], "kind": "P6_REFLECTION_RECORD"})
                 if row["payload"].get("status") == "LOCAL_VALIDATION_ONLY"]
        assert len(local) == 3
        assert {row["payload"].get("review_attempt_id") for row in local if row["payload"].get("review_attempt_id")} == {r[0] for r in reviews}
        assert kernel.artifact(repo, legacy, wr["work_id"])["payload"] == legacy_payload
        before = sorted(deepcopy(local), key=lambda row: row["artifact_revision_id"])
        for attempt, advice, payload in reviews:
            alias = _artifact(kernel, repo, wr["work_id"], owner, "CHAPTER_READING_ADVICE", payload)
            assert alias != advice and alias["sha256"] == advice["sha256"]
            verify_method(kernel, repo, owner, wr, plan, evidence, "targeted_revision", writer["output_ref"], attempt, alias)
        after = [row for row in repo.many("artifacts", {"work_id": wr["work_id"], "kind": "P6_REFLECTION_RECORD"})
                 if row["payload"].get("status") == "LOCAL_VALIDATION_ONLY"]
        assert sorted(after, key=lambda row: row["artifact_revision_id"]) == before
        changed = _artifact(kernel, repo, wr["work_id"], owner, "CHAPTER_READING_ADVICE",
                            {**reviews[0][2], "literary_quality": "DIFFERENT_RESULT"})
        with pytest.raises(DomainError) as error:
            verify_method(kernel, repo, owner, wr, plan, evidence, "targeted_revision", writer["output_ref"], reviews[0][0], changed)
        assert error.value.code == "INVALID_EVIDENCE"
        assert repo.many("call_attempts", {}) == calls_before
        assert repo.many("budgets", {"work_id": wr["work_id"]}) == budgets_before
