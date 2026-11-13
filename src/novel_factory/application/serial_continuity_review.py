"""Independent, supervised continuity reading for an intermediate, already released volume."""

import json
from uuid import UUID, uuid5

from .authorization import Principal
from .continuity import ContinuityService
from .idea_continuation import _enroll
from .idea_intake import _audited_design_result
from .kernel import require
from .rolling_production import frozen_volume_scenes
from ..domain.canonical import fingerprint
from ..domain.editorial import text_span


def _proposal(kernel, repo, owner, wr, cursor, volume, release_ref):
    """Index the availability of released passages, without inferring a narrative thread or resolving it."""
    work = wr["work_id"]
    prior = cursor["continuity_ref"]
    entries = (kernel.artifact(repo, prior, work, kind="CONTINUITY_SNAPSHOT")["payload"]["request"]["entries"]
               if prior else [])
    ordinal = len(cursor["release_refs"]) + 1
    fresh = []
    for scene in frozen_volume_scenes(kernel, repo, cursor, volume):
        card = kernel.artifact(repo, scene["scene_ref"], work, kind="SCENE_PLAN")["payload"]
        text_ref = scene["text_ref"]
        prose = kernel.objects.read(text_ref["sha256"]).decode()
        end = min(len(prose), 600)
        require(end > 0, "INVALID_EVIDENCE", "Released scene contains no evidence")
        identity = str(uuid5(UUID(release_ref["artifact_revision_id"]), scene["scene_id"]))
        fresh.append({"entry_id": identity, "thread_id": identity, "kind": "READER_DISCLOSURE", "plane": "READER",
                      "epistemic_status": "TEXT_ASSERTION_NOT_WORLD_FACT", "status": "RESOLVED",
                      "volume_ordinal": ordinal, "entity_ids": [], "story_interval": card["scene_contract"]["story_interval"],
                      "interpretation": "已向读者披露的原文摘录（仅记录文字已公开）：\n" + prose[:end], "source_texts": [{"release_ref": release_ref, "text_ref": text_ref,
                      "span": text_span(prose, 0, end)}], "source_events": [], "supersedes_entry_ids": [],
                      "planned_payoff_at": None})
    require(fresh, "INVALID_EVIDENCE", "Intermediate volume has no released scene")
    return ContinuityService(kernel).prepare(owner, {
        "schema_version": "2.3", "series_id": cursor["series_id"], "operation": "APPEND", "world_ref": wr,
        "expected_prior_ref": prior, "ordered_release_refs": [*cursor["release_refs"], release_ref],
        "entries": [*entries, *fresh], "revision_reason": None, "idempotency_key": "serial-source:" + release_ref["sha256"]})


def _pages(delivery):
    candidate = delivery["candidate"]
    # Only APPEND is admitted. Pre-existing approved memory remains an immutable prefix.
    ordinal = len(candidate["request"]["ordered_release_refs"])
    entries = candidate["request"]["entries"]
    new_entries = [e for e in entries if e["volume_ordinal"] == ordinal and e["plane"] == "READER"]
    prior = [e for e in entries if e["volume_ordinal"] < ordinal and e["plane"] == "READER"]
    pages = []
    for source in delivery["original_texts"]:
        prose = source["text"]
        # Keep only previously approved reader-visible evidence: no plans, actor beliefs or unreviewed facts.
        # For old pages select entries actually sourced here; for new pages add the latest earlier disclosures.
        matches = [e for e in prior if any(r["text_ref"] == source["text_ref"] for r in e["source_texts"])]
        related = matches if source["volume_ordinal"] < ordinal else prior
        previous = [{"entry_id": e["entry_id"], "volume_ordinal": e["volume_ordinal"],
                     "interpretation_excerpt": e["interpretation"][:600],
                     "text_ref": e["source_texts"][0]["text_ref"], "span": e["source_texts"][0]["span"]}
                    for e in related[-4:]]
        for start in range(0, len(prose), 8000):
            end = min(start + 8000, len(prose))
            pages.append({"operation": "review_serial_continuity", "volume_ordinal": source["volume_ordinal"],
                          "source_text_ref": source["text_ref"], "range": [start, end], "source_text": prose[start:end],
                          "previous_reader_evidence": previous,
                          "new_source_entries": [e for e in new_entries if start == 0 and
                                                 any(r["text_ref"] == source["text_ref"] for r in e["source_texts"])],
                          "instruction": "独立核查给定已发布原文与本次读者披露记录。确认来源和视角正确且未把文字当作人物私知。"
                                         "此类摘录只标识文字已向读者公开；披露完成与情节线、谜题或伏笔是否解决无关。"
                                         "对照所给前卷已批准的读者记录。证据不足或有实质错误时 RETURNED，确认此页无误才给 APPROVED。说明理由。",
                          "candidate_sha256": candidate["input_sha256"]})
    return pages


def advance_serial_reading(kernel, repo, gateway, owner, wr, cursor, volume, release_ref):
    work, branch = wr["work_id"], wr["branch_id"]
    service = ContinuityService(kernel)
    key = fingerprint([cursor["run_id"], wr, cursor["continuity_ref"], release_ref])
    state = kernel._prior(repo, owner, work, "P6_SERIAL_READING", key, {})
    if not state:
        candidate = _proposal(kernel, repo, owner, wr, cursor, volume, release_ref)
        material = kernel.artifact(repo, candidate, work, kind="CONTINUITY_CANDIDATE")["payload"]
        if material["impact"]["affected_volumes"]:
            # Reader-only page decisions cannot attest changed event selection or world interpretation.
            return {"status": "CONTINUITY_REVIEW_BLOCKED", "reason": "PRIOR_EDITION_IMPACT_REQUIRES_REVIEW",
                    "candidate_ref": candidate}
        # Control-plane attestation identity. Its token never enters a model task. Only
        # scoped task results bound to all original pages can advance its native decision.
        principal = kernel.issue_grant(owner, work, role_id="volume_director", branch_ids=[branch],
                                       planes=["WORLD","PLAN"], capabilities=["READ","APPROVE","PLAN_READ"],
                                       max_story_time=cursor["allowance"]["story_time"])["principal"]
        delivery = service.read_candidate(principal, work, candidate, "serial-originals:" + key)
        pages = _pages(delivery)
        require(pages, "READING_COVERAGE_INCOMPLETE", "No original passages delivered")
        state = {"candidate_ref": candidate, "reading_ref": delivery["reading_ref"],
                 "reader": {"grant_id": principal.grant_id, "principal_id": principal.principal_id},
                 "pages": len(pages), "manifest_sha256": fingerprint(pages)}
        kernel._receipt(repo, owner, wr, "P6_SERIAL_READING", key, {}, state)
    else:
        principal = Principal(**state["reader"])
        delivery = service.read_candidate(principal, work, state["candidate_ref"], "serial-originals:" + key)
        pages = _pages(delivery)
        require(len(pages) == state["pages"] and fingerprint(pages) == state["manifest_sha256"]
                and delivery["reading_ref"] == state["reading_ref"], "STALE_GENERATION", "Original reading changed")
    for i, page in enumerate(pages):
        slot = key + ":" + str(i)
        if kernel._prior(repo, owner, work, "P6_SERIAL_PAGE_PASS", slot, {}):
            continue
        record = kernel._prior(repo, owner, work, "P6_SERIAL_PAGE", slot, {})
        if record is None:
            # Each actual model judgment uses the unchanged operator review route and original work ledger.
            selected = gateway.with_kernel(kernel)
            selected.output_contracts[("reviewer", "continuity")] = ("AgentDecisionOutput", "agent")
            task_id = _enroll(kernel, repo, selected, owner, wr, cursor, page, [state["candidate_ref"]],
                              reviewing=True, review_kind="continuity")
            record = {"task_id": task_id, "page_index": i, "page_sha256": fingerprint(page)}
            kernel._receipt(repo, owner, wr, "P6_SERIAL_PAGE", slot, {}, record)
            return {"status": "CONTINUITY_REVIEW_ENROLLED", **record}
        task = repo.one("tasks", {"work_id": work, "branch_id": branch, "task_id": record["task_id"]})
        require(task and task["role_id"] == "reviewer" and task["task_kind"] == "continuity",
                "INVALID_EVIDENCE", "Wrong continuity reader")
        if task["state"] in {"FAILED","STALE","PAUSED","WAITING_REVIEW","CANCELLED"}:
            return {"status": "CONTINUITY_REVIEW_BLOCKED", "reason": task["state"], **record}
        if task["state"] != "RESULT_STORED":
            return None
        proof, decision = _audited_design_result(kernel, gateway, repo, task, wr)
        frozen = kernel.artifact(repo, proof["input_ref"], work, kind="AGENT_INPUT")["payload"]
        require(record["page_sha256"] == fingerprint(page) and json.loads(frozen["assignment"]) == page
                and proof["role_id"] == "reviewer" and proof["world_ref"] == wr
                and task["principal_id"] not in {owner.principal_id, principal.principal_id},
                "INVALID_EVIDENCE", "Review does not cover the current original page")
        if decision["decision"] != "APPROVED" or decision["deferred_questions"]:
            return {"status": "CONTINUITY_REVIEW_RETURNED", "reason": "INDEPENDENT_READING", **record,
                    "review_ref": proof["output_ref"]}
        kernel.tasks.accept_in_transaction(repo.conn, task["task_id"], task["generation"])
        kernel._receipt(repo, owner, wr, "P6_SERIAL_PAGE_PASS", slot, {},
                        {**record, "attempt_id": proof["attempt_id"], "review_ref": proof["output_ref"]})
        return {"status": "CONTINUITY_PAGE_REVIEWED", **record}
    service.review(principal, work, {"schema_version": "2.3", "candidate_ref": state["candidate_ref"],
                   "reading_ref": state["reading_ref"], "decision": "APPROVED",
                   "reason": "逐页独立审核已通过；阅读清单与各模型审核收据已冻结。" + state["manifest_sha256"],
                   "acknowledged_affected_ordinals": [],
                   "idempotency_key": "serial-verified:" + key})
    return {"status": "CONTINUITY_REVIEW_APPROVED", "candidate_ref": state["candidate_ref"], "pages": len(pages)}
