"""PostgreSQL receipts, superseded editions, frozen chapter text and cut boundaries."""

from uuid import uuid4

import pytest
from editorial_helpers import review_binding
from test_continuity import entry, qualify, request, reviewer, volume
from test_editorial_service import literary_issue, prepared

from novel_factory.application.continuity import ContinuityService
from novel_factory.application.literary_statistics import LiteraryStatisticsService
from novel_factory.application.statistics import StatisticsService
from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import load_fixture

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


def test_selected_epoch_and_frozen_chapter_only_in_bound_cut(kernel_env):
    k = kernel_env["kernel"]
    f, first, _ = volume(k)
    p, wr = f["principal"], f["world_ref"]
    work, branch = wr["work_id"], wr["branch_id"]
    service, continuity = LiteraryStatisticsService(k), ContinuityService(k)
    with k.db.uow(write=False) as repo:
        record = k.artifact(repo, first["release_ref"], work)["payload"]
        approval = k.artifact(repo, record["editorial_approval_ref"], work)["payload"]
        reviews = [
            (ref, k.artifact(repo, ref, work)["payload"]["submission"]["scale"])
            for ref in approval["review_refs"]
        ]
    chapter_review = next(ref for ref, scale in reviews if scale == "CHAPTER")
    chapter = {
        "chapter_id": str(uuid4()),
        "chapter_review_ref": chapter_review,
        "release_ref": first["release_ref"],
    }
    scope = (p, work, branch)
    initial = service.report(*scope, chapters=[chapter])
    assert initial["chapters"][0]["unicode_characters"] == len("北窗的雨声很轻。南岸的水声很远。")
    assert initial["chapters"][0]["draft_count"] == 2
    assert initial["plan_goals"][0]["plan_ref"] == record["plan_ref"]
    assert initial["plan_goals"][0]["nodes"] == sum(initial["plan_goals"][0]["by_status"].values())
    assert initial["foreshadow"] is None and service.report(*scope)["chapters"] is None
    assert service.index_history(*scope)["indexed"] == dict.fromkeys(
        ["PLAN_ADOPT", "RELEASE_FREEZE", "CONTINUITY_PUBLISH", "editorial_artifacts"], 0
    )
    with pytest.raises(DomainError, match="assigned more than once"):
        service.report(*scope, chapters=[chapter, {**chapter, "chapter_id": str(uuid4())}])
    with pytest.raises(DomainError, match="chapter-scale"):
        service.report(
            *scope,
            chapters=[
                {
                    **chapter,
                    "chapter_review_ref": next(ref for ref, scale in reviews if scale == "SCENE"),
                }
            ],
        )

    shadow = entry(f, first, 1, kind="FORESHADOW")
    shadow["status"] = "OPEN"
    original = request(f, [first], [shadow])
    reader = reviewer(k, f)
    publish, _, _ = qualify(continuity, p, reader, work, original)
    snapshot = continuity.publish(p, work, publish)
    opened = service.report(*scope)
    assert opened["foreshadow"][0]["by_status"] == {"OPEN": 1}
    resolved = entry(f, first, 1, prior=shadow, kind="FORESHADOW")
    resolved["status"] = "RESOLVED"
    revised = request(
        f,
        [first],
        [shadow, resolved],
        series_id=original["series_id"],
        prior=snapshot["snapshot_ref"],
        operation="REVISE",
        reason="冻结稿中的伏笔已有解读。",
    )
    publish, _, _ = qualify(continuity, p, reader, work, revised)
    newer = continuity.publish(p, work, publish)
    latest = service.report(*scope)
    assert latest["foreshadow"][0] == {
        "series_id": original["series_id"],
        "snapshot_ref": newer["snapshot_ref"],
        "selection_epoch": 2,
        "threads": 1,
        "by_status": {"RESOLVED": 1},
        "by_evidence_status": {"CURRENT_SOURCE_BOUND": 1},
    }
    assert service.report(*scope, source_cut=opened["source_cut"]) == opened
    assert service.report(*scope, chapters=[chapter], source_cut=initial["source_cut"]) == initial
    other = load_fixture(k)
    with pytest.raises(DomainError, match="Unavailable statistics cut"):
        service.report(
            other["principal"],
            other["world_ref"]["work_id"],
            other["world_ref"]["branch_id"],
            source_cut=initial["source_cut"],
        )
    with pytest.raises(DomainError, match="outside this grant"):
        service.report(reader, work, branch)
    with pytest.raises(DomainError, match="Release unavailable"):
        service.report(
            other["principal"],
            other["world_ref"]["work_id"],
            other["world_ref"]["branch_id"],
            chapters=[chapter],
        )


def test_immutable_review_observations_in_v4_and_v3(kernel_env):
    k = kernel_env["kernel"]
    f, _, binding = prepared(k)
    issue = literary_issue(k, f, binding["ordered_draft_refs"][0], hard=True)
    proof = review_binding(k, f, binding, literary_issues=[issue])
    stats = StatisticsService(k)
    scope = (f["principal"], f["world_ref"]["work_id"], f["world_ref"]["branch_id"])
    raised = stats.rebuild(*scope, version=4)
    assert raised["metrics"]["literary.consistency_issue_observations"] == 1
    assert raised["metrics"]["literary.consistency_severity.BLOCKER"] == 1
    assert sum(
        n for key, n in raised["metrics"].items() if key.startswith("literary.review_dimension.")
    ) == len(proof["review_refs"])
    assert stats.verify_statistics(*scope, version=4, source_cut=raised["source_cut"])["consistent"]
    older = stats.rebuild(*scope, version=3, source_cut=raised["source_cut"])
    assert "literary.consistency_issue_observations" not in older["metrics"]
    assert older["metrics"]["literary.issue.HARD_CONSISTENCY"] == 1
    companion = LiteraryStatisticsService(k).report(*scope, source_cut=raised["source_cut"])
    assert (
        companion["character_consistency_failure_rate"] is None
        and companion["literary_quality_score"] is None
    )


def test_legacy_receipt_index_and_unadopted_plan_do_not_change_old_cut(kernel_env, monkeypatch):
    from copy import deepcopy

    from test_release import setup_batch

    from novel_factory.application import literary_statistics
    from novel_factory.application.planning import PlanningService

    k = kernel_env["kernel"]
    with monkeypatch.context() as patch:
        patch.setattr(literary_statistics, "record_selection", lambda *args, **kwargs: None)
        f = setup_batch(k)
    p, wr = f["principal"], f["world_ref"]
    work, branch = wr["work_id"], wr["branch_id"]
    scope = (p, work, branch)
    reports = LiteraryStatisticsService(k)
    prior = reports.report(*scope)
    assert prior["plan_goals"] is None
    assert reports.index_history(*scope)["indexed"]["PLAN_ADOPT"] == 1
    indexed = reports.report(*scope)
    assert indexed["plan_goals"][0]["by_status"] == {"VIABLE": 2}
    assert reports.report(*scope, source_cut=prior["source_cut"]) == prior
    assert reports.index_history(*scope)["indexed"]["PLAN_ADOPT"] == 0
    planner = PlanningService(k)
    plan = deepcopy(f["plan"])
    plan.update(plan_revision_id=str(uuid4()), prior_plan_ref=f["plan_ref"])
    plan["nodes"][0]["status"] = "ABANDONED"
    candidate = planner.save_plan(p, plan, "new-candidate")
    assert reports.report(*scope)["plan_goals"] == indexed["plan_goals"]
    planner.adopt_plan(p, wr, candidate, f["plan_ref"], "select-new")
    current = reports.report(*scope)
    assert len(current["plan_goals"]) == 1
    assert current["plan_goals"][0]["plan_ref"] == candidate
    assert current["plan_goals"][0]["by_status"] == {"VIABLE": 1, "ABANDONED": 1}
    assert reports.report(*scope, source_cut=indexed["source_cut"]) == indexed
    fork = k.fork(p, wr, "CANDIDATE", "separate-branch")["branch_id"]
    assert reports.report(p, work, fork)["plan_goals"] is None
    with pytest.raises(DomainError, match="Unavailable statistics cut"):
        reports.report(p, work, fork, source_cut=indexed["source_cut"])
    with pytest.raises(DomainError, match="Unknown publication report version"):
        reports.report(*scope, version=3)
