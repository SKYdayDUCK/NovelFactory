"""Deliver only authorized edit locations to the writer of a returned manuscript."""
from __future__ import annotations

import re

from novel_factory.application.creative_review import (
    CreativeReviewService, _bound_draft, _current_manuscript, _latest_decision, _part, _review_history, _task,
)
from novel_factory.application.editorial import ReviewService
from novel_factory.application.kernel import require
from novel_factory.application.narrative import NarrativeService
from novel_factory.contracts.registry import validate_contract
from novel_factory.domain.reconciliation import span_for, verify_span


# A reviewer's prose may cite other chapters, private motives or their own
# identity. These directions depend only on the classified issue, never on
# private notes, counter-readings or passages outside the writer's scope.
_DIRECTIONS = {
    "UNUSED_EXPOSITION": "只调整这段设定说明，让它与眼前的选择相连。",
    "ENGINEERING_SERIALIZATION": "将选中记录改写为场景中人物可以感知的信息。",
    "REPEATED_NAME_FORM": "在保持设定的前提下，使选中的名称易于辨认。",
    "MOTIVATION_GAP": "在选中行动附近补足人物改变选择的原因。",
    "MISSING_CONSEQUENCE": "在选中位置补明行动带来的直接后果。",
    "UNBRIDGED_MOVEMENT": "在选中位置交代人物如何到达这里。",
    "SHARED_VOICE": "只调整这句对白，使它符合说话者的语气。",
    "EMOTION_AS_RULE": "让选中位置的情绪通过人物的具体反应显现。",
    "REPEATED_IMAGE": "调整选中的意象，使这次出现承担明确的叙事作用。",
    "MECHANICAL_CADENCE": "只调整选中的重复句，保留其余段落。",
    "EXCESS_SUMMARY": "收短选中位置的解释，让人物行动承接其含义。",
    "UNBRIDGED_SEAM": "在选中的章节边界补足场景衔接。",
    "REPEATED_SEAM": "调整选中的章节边界，避免无意重复。",
    "DROPPED_PROMISE": "在选中位置交代先前叙事承诺的进展或有意悬置。",
    "REPEATED_BEAT": "只调整选中的情节节拍，使其承担新的作用。",
    "WORLD_CONTRADICTION": "核对选中陈述与已授权设定，修正陈述或明确其归属。",
    "POV_LEAK": "核对选中位置的视角，让人物只依据其已知信息行动。",
}


def authorize_returned_revision(service, repo, principal, task_id, generation, report_ref, worker_id, live_proof):
    kernel = service.kernel
    task, _, spec, _, runtime = _task(kernel, repo, principal, task_id, generation, "artifact_put", worker_id)
    require(task["task_kind"] == "targeted_revision" and report_ref in spec["input_artifacts"]
            and "draft_submit" in runtime.get("mcp_requested_tools", []), "VIEW_DENIED",
            "Writer needs an assigned, frozen revision instruction")
    report, details = _review_history(kernel, repo, task["work_id"], report_ref)
    require(report["scope"]["world_ref"] == spec["world_ref"] and report["target_ref"] in spec["input_artifacts"]
            and principal.principal_id != report["review_binding"]["reviewer"]["principal_id"],
            "VIEW_DENIED", "Review and manuscript must belong to this independent writer assignment")
    service._current_review(repo, report, details, live_proof)
    decision = _latest_decision(kernel, repo, task["work_id"], report_ref)
    require(decision and decision["action"] == "RETURN" and details["round"] < report["rework_limit"],
            "REVIEW_REQUIRED", "The editor must return this manuscript for another bounded revision")
    require(principal.principal_id != decision["editor_id"], "VIEW_DENIED",
            "The deciding editor cannot act as the revision writer")
    return task, spec, report, details, decision


def pending_revision(service, repo, principal, task, spec, report_ref, report, details):
    kernel, work = service.kernel, task["work_id"]
    require(_part(repo, work, report_ref, "revision", "CREATIVE_REVIEW_REVISION", optional=True) is None,
            "EDIT_CONFLICT", "The review already produced its directed manuscript")
    for ref in details["scope_draft_refs"]:
        _current_manuscript(repo, work, task["branch_id"], ref)
    draft = _bound_draft(kernel, repo, principal, task, spec, report["target_ref"])
    NarrativeService(kernel)._writer_binding(repo, principal, task["task_id"], task["generation"], draft["scene_ref"])
    text = ReviewService(kernel)._text(repo, draft["text_ref"], work)
    require(len(report["findings"]) == len(details["findings"]) and draft["text_ref"] == details["text_ref"],
            "INVALID_EVIDENCE", "Mismatched findings or source manuscript")
    eligible = {}
    for note, finding in zip(details["findings"], report["findings"], strict=True):
        if finding["requested_change"] and finding["judgment"] in {"CONTRADICTION", "PREFERENCE"}:
            require(finding["target_ref"] == draft["text_ref"] and note["span"] == finding["text_span"]
                    and note["finding_id"] not in eligible, "INVALID_EVIDENCE", "Invalid directed revision anchor")
            verify_span(text, finding["text_span"])
            eligible[note["finding_id"]] = note, finding
    return draft, text, eligible


class CreativeRevisionBriefService:
    def __init__(self, kernel):
        self.kernel = kernel

    def read(self, principal, task_id, generation, *, report_ref, worker_id=None):
        validate_contract("ArtifactRef", report_ref)
        require(isinstance(worker_id, str) and worker_id, "VIEW_DENIED", "The bound writer lease is required")
        # Reject misrouted requests before any external independence refresh.
        with self.kernel.db.uow(write=False) as repo:
            task, _, spec, _, _ = _task(self.kernel, repo, principal, task_id, generation, "artifact_put", worker_id)
            require(task["task_kind"] == "targeted_revision" and report_ref in spec["input_artifacts"],
                    "VIEW_DENIED", "Revision brief unavailable to this assignment")
        service = CreativeReviewService(self.kernel)
        live = service._refresh_report(principal, task_id, generation, report_ref, writing=True)
        with self.kernel.objects.lock(), self.kernel.db.uow(write=False) as repo:
            self.kernel.authorize(repo, principal, task["work_id"], "READ", task["branch_id"])
            repo.one("branches", {"branch_id": task["branch_id"]}, lock="share")
            repo.one("tasks", {"task_id": task_id}, lock="share")
            task, spec, report, details, _ = authorize_returned_revision(
                service, repo, principal, task_id, generation, report_ref, worker_id, live)
            draft, text, eligible = pending_revision(service, repo, principal, task, spec, report_ref, report, details)
            findings = []
            for identifier, (note, finding) in eligible.items():
                span = finding["text_span"]
                excerpt = span_for(text, span["start"], min(span["end"], span["start"] + 512))
                change = finding["requested_change"]
                # A local excerpt or a purely positional instruction cannot
                # transmit another character's knowledge. Other free-form
                # suggestions stay inside their original reviewer/editor scope.
                if not (change in text or re.fullmatch(r"只改第[一二三四五六七八九十百0-9]{1,3}处[。]?", change)):
                    change = _DIRECTIONS.get(note["pattern"], "只修改选中的文字，保留未标记段落。")
                findings.append({"finding_id": identifier, "text_span": span, "quote_span": excerpt,
                                 "quote": text[excerpt["start"]:excerpt["end"]], "requested_change": change})
            return {"report_ref": report_ref, "draft_ref": report["target_ref"], "text_ref": draft["text_ref"],
                    "round": details["round"] + 1, "findings": findings}
