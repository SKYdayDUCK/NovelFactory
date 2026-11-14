"""Story completion uses actual PG/MCP proofs; no live model or HTTP send is allowed."""

from copy import deepcopy

import pytest
import test_native_final_book as final_fixture

from novel_factory.application import frozen_editing, rolling_production, story_completion
from novel_factory.application.idea_continuation import _pending
from novel_factory.application.production_pipeline import _artifact
from novel_factory.domain.errors import DomainError

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def test_native_story_completion_then_editing_blind_review_and_freeze_ignores_estimated_chapters(kernel_env, monkeypatch):
    original_production, original_editorial = final_fixture.synthetic_production, final_fixture._editorial
    captured = {}

    def production(kernel, patch, **kwargs):
        gateway, owner, wr, calls, diagnoses = original_production(kernel, patch, **kwargs)
        with kernel.objects.lock(), kernel.db.uow() as repo:
            cursor = rolling_production._load(kernel, repo, wr["work_id"], wr["branch_id"])
            cursor["target_chapters"] = 99
            rolling_production._save(kernel, repo, owner, cursor)
            design_ref = _artifact(kernel, repo, wr["work_id"], owner, "INITIAL_STORY_DESIGN",
                                   {"storyline": "Synthetic approved complete story.", "chapter_goals": ["Synthetic outcome."]})
            kernel._receipt(repo, owner, wr, "P6_IDEA_PROVENANCE", "initial", {}, {"design_ref": design_ref})
        captured.update(owner=owner, world_ref=wr)
        return gateway, owner, wr, calls, diagnoses

    def editorial(phase, text):
        if phase == "STORY_COMPLETION":
            return {"decision": "COMPLETE", "reason": "Synthetic approved outcome is present in the exact manuscript.",
                    "unfulfilled_commitments": []}
        return original_editorial(phase, text)

    monkeypatch.setattr(final_fixture, "synthetic_production", production)
    monkeypatch.setattr(final_fixture, "_editorial", editorial)
    final_fixture.test_native_editing_blind_reading_restart_and_final_freeze(kernel_env, monkeypatch, False)
    kernel, wr = kernel_env["kernel"], captured["world_ref"]
    with kernel.db.uow(write=False) as repo:
        cursor = rolling_production._load(kernel, repo, wr["work_id"], wr["branch_id"])
        assert cursor["frozen_chapters"] == cursor["chapter_count"] == 1 and cursor["target_chapters"] == 99
        decision = story_completion.current_decision(kernel, repo, cursor)
        assert decision["advice"]["decision"] == "COMPLETE" and not _pending(kernel, repo, cursor)
        task = repo.one("tasks", {"task_id": decision["task_id"]})
        job = repo.one("supervisor_jobs", {"task_id": task["task_id"]})
        assert task["state"] == "ACCEPTED" and job["config"]["required_review_mcp"]
        assert len(repo.many("receipts", {"work_id": wr["work_id"], "command_type": "STORY_COMPLETION"})) == 1
        assert not repo.many("receipts", {"work_id": wr["work_id"], "command_type": "P6_NEXT_PLANNER"})
        # The proof is tied to the full actual chapter prefix, not a reusable
        # statement that a future draft or another run is complete.
        changed = deepcopy(cursor)
        changed["last_checkpoint"] = None
        assert story_completion.current_decision(kernel, repo, changed) is None
        state = frozen_editing._state(kernel, repo, wr["work_id"], wr["branch_id"])
        assert state["status"] == "EDITORIAL_APPROVED" and state["story_completion_ref"] == decision["decision_ref"]
        assert len(state["reports"]) == 3 and len(state["decisions"]) == 3
        with pytest.raises(DomainError) as unproven:
            story_completion.require_complete(kernel, repo, changed)
        assert unproven.value.code == "STORY_COMPLETION_REQUIRED"
