"""Final creative-review decisions require a currently enrolled, independent editor."""
from datetime import UTC, datetime, timedelta
import json

import pytest

from novel_factory.application.creative_review import CreativeReviewService
from novel_factory.application.review_independence import native_signature
from novel_factory.domain.canonical import fingerprint
from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import point
from test_harness_review_workflow import _args, _formal, _repeat, _reviewer, manuscript
from test_review_independence import enroll_native_case

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def test_retain_requires_separate_editor_enrollment(manuscript, tmp_path, monkeypatch):
    kernel, world, draft = manuscript
    service = CreativeReviewService(kernel)
    job, args, read = _reviewer(kernel, world, draft, tmp_path, monkeypatch)
    args.update(observations=[_repeat(read)], rework_limit=0)
    report = service.review(*_args(job), **args)
    formal = _formal(kernel, world, service, job, args, read, report, retain=True)
    with pytest.raises(DomainError) as denied:
        service.decide(world["principal"], *_args(job)[1:], report_ref=report["report_ref"], action="RETAIN",
                       reason="保留独立阅读的结论。", idempotency_key="retain", native_decision=formal)
    assert denied.value.code == "INDEPENDENT_REVIEW_REQUIRED"


def test_editor_groups_and_receipts_follow_the_actual_reading(manuscript, tmp_path, monkeypatch):
    kernel, world, draft = manuscript
    service = CreativeReviewService(kernel)
    editor = world["principal"]
    job, args, read = _reviewer(kernel, world, draft, tmp_path, monkeypatch)
    args.update(observations=[_repeat(read)], rework_limit=0)
    report = service.review(*_args(job), **args)
    formal = _formal(kernel, world, service, job, args, read, report, retain=True)
    _, task_id, generation = _args(job)

    def register(**groups):
        return enroll_native_case(kernel, tmp_path, monkeypatch, draft["draft_ref"], task_id, generation,
                                  controller=editor, **groups)

    def retain(actual_editor):
        return service.decide(actual_editor, task_id, generation, report_ref=report["report_ref"], action="RETAIN",
                              reason="保留重复句及原阅读意见。", idempotency_key="retain", native_decision=formal)

    for group in ("workshop-authors", "external-editors"):
        with pytest.raises(DomainError, match="qualified"):
            register(editor_group=group)
    register(editor_group="workshop-authors", producer_group="different-roster")
    with pytest.raises(DomainError, match="qualified"):
        retain(editor)
    case = register(editor_group="volume-editors")
    actual = kernel.issue_grant(editor, world["world_ref"]["work_id"], role_id="literary_editor",
                                branch_ids=[world["world_ref"]["branch_id"]], capabilities=["READ", "APPROVE"],
                                max_story_time=point(1000))["principal"]
    with pytest.raises(DomainError, match="qualified"):
        retain(actual)
    result = retain(editor)
    with kernel.db.uow(write=False) as repo:
        work = world["world_ref"]["work_id"]
        record = kernel.artifact(repo, result["decision_ref"], work)["payload"]
        reading = kernel.artifact(repo, args["independence_ref"], work)["payload"]
    signed = json.loads(case.read_text())["case"]
    assert record["editor_independence"] == [{"manuscript_ref": draft["draft_ref"],
        "binding_sha256": fingerprint(signed["binding"]), "editor_evidence_sha256": fingerprint(signed),
        "reading_evidence_sha256": reading["evidence_digest"]}]
    signed["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    case.write_text(json.dumps({"case": signed, "signature": native_signature(signed)}))
    with pytest.raises(DomainError, match="qualified"):
        retain(editor)
    case.unlink()
    with pytest.raises(DomainError, match="qualified"):
        retain(editor)
