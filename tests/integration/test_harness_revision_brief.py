"""Returned edit locations travel through the assigned writer's real stdio MCP surface."""
import asyncio
import json

import pytest

from novel_factory.application.creative_review import CreativeReviewService
from novel_factory.application.creative_revision_brief import CreativeRevisionBriefService
from novel_factory.application.review_independence import ReviewIndependenceService, native_case_path
from novel_factory.domain.errors import DomainError
from novel_factory.domain.reconciliation import verify_span
from test_harness_literary_mcp import review_ports
from test_harness_review_workflow import CHANGE, LINE, _args, _repeat, _reviewer, _writer, manuscript
from test_p5_mcp import error, exercise


pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def test_brief_waits_for_an_editor_and_releases_only_writer_scoped_edits(manuscript, kernel_env, tmp_path, monkeypatch):
    kernel, world, source = manuscript
    review = CreativeReviewService(kernel)
    reviewer, args, reading = _reviewer(kernel, world, source, tmp_path, monkeypatch)
    private_note = "另一篇未分配的稿件让读者知道了渡船的真实来历。"
    proposal = _repeat(reading)
    proposal.update(requested_change=private_note, counterinterpretation="这段隐喻只在审读者的笔记中出现。")
    question = {**proposal, "disposition": "QUESTION", "reason": "这是仅供主编参考的分歧。"}
    args["observations"] = [proposal, question]
    result = review.review(*_args(reviewer), **args)
    ref = result["report_ref"]
    writer = _writer(kernel, world, source, ref)
    brief = CreativeRevisionBriefService(kernel)
    with pytest.raises(DomainError) as pending:
        brief.read(*_args(writer), report_ref=ref, worker_id=writer.worker_id)
    assert pending.value.code == "REVIEW_REQUIRED"
    with pytest.raises(DomainError):
        brief.read(*_args(writer), report_ref=ref, worker_id="not-the-lease-owner")
    with pytest.raises(DomainError):
        kernel.get_artifact(writer.principal, world["world_ref"]["work_id"], ref)
    review.decide(world["principal"], *_args(reviewer)[1:], report_ref=ref, action="RETURN",
                  reason="只退回这一处改写。", idempotency_key="return-position")
    selector = {"task_id": writer.task["task_id"], "generation": writer.task["generation"]}
    port, _ = review_ports(tmp_path, kernel_env, world, writer, selector)
    binding = ReviewIndependenceService(kernel).describe(world["principal"], manuscript_ref=source["draft_ref"],
        reviewer_task_id=reviewer.task["task_id"], reviewer_generation=reviewer.task["generation"])
    case = native_case_path(binding)

    async def run(session):
        names = {tool.name for tool in (await session.list_tools()).tools}
        assert {"nf_harness_revise", "nf_harness_revision_brief"} <= names
        assert not {"nf_harness_review_decide", "nf_harness_review_submit"} & names
        request = {"report_ref": ref}
        response = await session.call_tool("nf_harness_revision_brief", request)
        assert not response.isError, response.content
        returned = response.structuredContent
        assert set(returned) == {"report_ref", "draft_ref", "text_ref", "round", "findings"}
        assert returned["draft_ref"] == source["draft_ref"] and returned["round"] == 1
        finding, = returned["findings"]
        assert finding["finding_id"] == result["findings"][0]["finding_id"] and finding["quote"] == LINE
        assert finding["requested_change"] and finding["requested_change"] != private_note
        assert verify_span(source["text"], finding["quote_span"]) is None
        wire = json.dumps(returned, ensure_ascii=False)
        for restricted in (private_note, proposal["counterinterpretation"], question["reason"],
                           reviewer.principal.principal_id, result["details_ref"]["artifact_revision_id"]):
            assert restricted not in wire
        archived = case.with_suffix(".unavailable")
        case.rename(archived)
        await error(session, "nf_harness_revision_brief", request, "INDEPENDENT_REVIEW_REQUIRED")
        archived.rename(case)
        revised = await session.call_tool("nf_harness_revise", {
            "report_ref": ref, "scene_ref": world["scene_refs"][0],
            "replacements": [{"finding_id": finding["finding_id"], "replacement": CHANGE}],
            "idempotency_key": "brief-to-revision",
        })
        assert not revised.isError, revised.content
        consumed = await session.call_tool("nf_harness_revision_brief", request)
        assert consumed.isError and consumed.structuredContent["error"]["code"] in {"EDIT_CONFLICT", "TASK_REBIND_REQUIRED"}

    asyncio.run(exercise(port, run))
