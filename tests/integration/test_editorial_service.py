"""P2-05 PostgreSQL source-delivery, review, editing and approval invariants."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from uuid import uuid4

import pytest

from novel_factory.application.editorial import ReviewService, memory_dependency
from novel_factory.application.narrative import NarrativeService
from novel_factory.application.release import ReleaseService
from novel_factory.domain.canonical import fingerprint
from novel_factory.domain.editorial import text_span
from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import point, run_command

from editorial_helpers import (approve_binding, binding_for, decision_request, make_review_task,
                               review_binding, review_request)
from test_release import accept_draft, setup_batch, write_scene

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def uid():
    return str(uuid4())


def prepared(k):
    f = setup_batch(k)
    accepted = [accept_draft(k, f, write_scene(k, f, i, text)) for i, text in enumerate([
        "她笑了一下，笑意却没有抵达眼底。\r\n门外的风还在。",
        "他未把那笑当作宽恕。檐水落下，隔开一句未出口的话。"])]
    return f, accepted, binding_for(f, accepted)


def validate_approval(k, principal, ref, binding):
    with k.db.uow(write=False) as repo:
        return ReviewService(k).validate_editorial_approval(repo, principal, ref,
            binding["world_ref"], binding["ordered_draft_refs"], binding["reconciliation_refs"],
            binding["narrative_revision_id"], binding["arrangement_version"], binding["style_refs"], binding["literary_refs"],
            plan_ref=binding["plan_ref"], ordered_acceptance_refs=binding["ordered_acceptance_refs"],
            disclosure_revision_id=binding["disclosure_revision_id"])


def literary_issue(k, f, draft_ref, *, hard=False):
    draft = NarrativeService(k).load_draft(f["principal"], f["world_ref"]["work_id"], draft_ref)
    text = k.objects.read(draft["text_ref"]["sha256"]).decode()
    evidence = {"kind": "ARTIFACT", "artifact_ref": draft["text_ref"], "span": text_span(text, 0, min(8, len(text)))}
    return {"issue_id": uid(), "category": "HARD_CONSISTENCY" if hard else "LITERARY",
        "severity": "BLOCKER" if hard else "SUGGESTION", "subject_ref": draft_ref,
        "description": "工程反例：需新的独立事实检查。" if hard else "微笑可能误读为已经宽恕，也可理解为掩饰或短暂释然。",
        "evidence_refs": [evidence], "suggested_change": "结合下一场原文决定保留或修订。", "disposition": "OPEN",
        "issue_kind": "HARD_CONFLICT" if hard else "LITERARY_CHOICE"}


def test_full_text_multiscale_review_and_three_phase_independent_approval(kernel_env):
    k = kernel_env["kernel"]; f, _, binding = prepared(k); s = ReviewService(k)
    issue = literary_issue(k, f, binding["ordered_draft_refs"][0])
    decision = {"issue_id": issue["issue_id"], "action": "RETAIN_WITH_REASON",
        "reason": "后场明确没有把笑等同宽恕，保留混合情绪与两条声线。", "evidence_refs": issue["evidence_refs"], "replacement_draft_refs": []}
    result = approve_binding(k, f, binding, literary_issues=[issue], retain_decisions=[decision])
    assert s.approve_editorial(f["principal"], result["approval_request"]) == result["approval_ref"]
    report = validate_approval(k, f["principal"], result["approval_ref"], binding)
    assert report["approval"]["disposition"] == "ACCEPTED"
    assert len(result["edit_decision_refs"]) == 3
    assert len(result["review_refs"]) == 5
    for request, ref in zip(result["decision_requests"], result["edit_decision_refs"]):
        assert s.record_decision(f["principal"], request) == ref
    with k.db.uow(write=False) as repo:
        assert k.head(repo, f["world_ref"]["work_id"]) == f["world_ref"]
        assert not repo.many("events", {"work_id": f["world_ref"]["work_id"]})
        assert not repo.many("approvals", {"work_id": f["world_ref"]["work_id"]})
    changed = deepcopy(binding); changed["disclosure_revision_id"] = uid()
    with pytest.raises(DomainError) as stale:
        validate_approval(k, f["principal"], result["approval_ref"], changed)
    assert stale.value.code == "EDITORIAL_BINDING_MISMATCH"


def test_exact_delivery_gaps_other_reader_and_wrong_dimension_are_rejected(kernel_env):
    k = kernel_env["kernel"]; f, _, binding = prepared(k); s = ReviewService(k)
    reader, task = make_review_task(k, f, binding, "fact")
    draft_ref = binding["ordered_draft_refs"][0]
    first = s.read_text(reader, task["task_id"], task["generation"], draft_ref, 0, 4, idempotency_key="first")
    assert s.read_text(reader, task["task_id"], task["generation"], draft_ref, 0, 4, idempotency_key="first")["coverage_ref"] == first["coverage_ref"]
    request = review_request(binding, task, [first["coverage_ref"]], "SCENE", "FACT", [draft_ref])
    with pytest.raises(DomainError) as missing:
        s.submit_review(reader, request)
    assert missing.value.code == "READING_COVERAGE_INCOMPLETE"
    excerpt = s.read_text(reader, task["task_id"], task["generation"], draft_ref,
                          read_mode="EXCERPT", idempotency_key="excerpt")
    request["coverage_refs"].append(excerpt["coverage_ref"])
    with pytest.raises(DomainError) as still_missing:
        s.submit_review(reader, request)
    assert still_missing.value.code == "READING_COVERAGE_INCOMPLETE"
    rest = s.read_text(reader, task["task_id"], task["generation"], draft_ref, 4, None, idempotency_key="rest")
    request["coverage_refs"].append(rest["coverage_ref"])
    ref = s.submit_review(reader, request)
    assert s.submit_review(reader, request) == ref
    another, other_task = make_review_task(k, f, binding, "fact")
    wrong = review_request(binding, other_task, request["coverage_refs"], "SCENE", "FACT", [draft_ref])
    with pytest.raises(DomainError) as stolen:
        s.submit_review(another, wrong)
    assert stolen.value.code == "INVALID_READING_COVERAGE"
    wrong_mode = deepcopy(request); wrong_mode.update(dimension="EPISTEMIC", idempotency_key="different-mode")
    with pytest.raises(DomainError) as mode:
        s.submit_review(reader, wrong_mode)
    assert mode.value.code == "VIEW_DENIED"
    k.revoke_grant(f["principal"], reader.grant_id)
    with pytest.raises(DomainError) as revoked:
        s.read_text(reader, task["task_id"], task["generation"], draft_ref, idempotency_key="after-revoke")
    assert revoked.value.code == "VIEW_DENIED"


def test_skip_structure_false_resolution_and_unchanged_aesthetic_loop_are_blocked(kernel_env):
    k = kernel_env["kernel"]; f, _, binding = prepared(k); s = ReviewService(k)
    issue = literary_issue(k, f, binding["ordered_draft_refs"][0])
    retained = {"issue_id": issue["issue_id"], "action": "RETAIN_WITH_REASON", "reason": "保持有意的双重情绪。",
                "evidence_refs": issue["evidence_refs"], "replacement_draft_refs": []}
    proof = approve_binding(k, f, binding, literary_issues=[issue], retain_decisions=[retained], approve=False)
    first = proof["decision_requests"][0]
    fresh_session = s.start_editing(f["principal"], {**proof["session_request"], "max_rounds": 3, "idempotency_key": "fresh"})
    skip = deepcopy(first); skip.update(session_ref=fresh_session, phase="LANGUAGE", idempotency_key="skip")
    with pytest.raises(DomainError) as ordering:
        s.record_decision(f["principal"], skip)
    assert ordering.value.code == "EDIT_ORDER_VIOLATION"
    correct = deepcopy(first); correct.update(session_ref=fresh_session, idempotency_key="first-correct")
    prior = s.record_decision(f["principal"], correct)
    repeated = deepcopy(correct); repeated.update(expected_prior_decision_ref=prior, idempotency_key="repeat")
    with pytest.raises(DomainError) as loop:
        s.record_decision(f["principal"], repeated)
    assert loop.value.code == "EDITORIAL_NO_PROGRESS"
    for phase in ("SEAM", "LANGUAGE"):
        request = decision_request(fresh_session, prior, phase, binding, proof["review_refs"], [])
        prior = s.record_decision(f["principal"], request)
    request = decision_request(fresh_session, prior, "LANGUAGE", binding, proof["review_refs"], [])
    with pytest.raises(DomainError) as budget:
        s.record_decision(f["principal"], request)
    assert budget.value.code == "EDITORIAL_BUDGET_EXHAUSTED"
    hard = literary_issue(k, f, binding["ordered_draft_refs"][0], hard=True)
    hard_proof = review_binding(k, f, binding, literary_issues=[hard])
    hard_session = s.start_editing(f["principal"], {**proof["session_request"], "idempotency_key": "hard-session"})
    waive = deepcopy(correct); waive.update(session_ref=hard_session, review_refs=hard_proof["review_refs"], idempotency_key="waive")
    waive["issue_decisions"] = [{**retained, "issue_id": hard["issue_id"], "evidence_refs": hard["evidence_refs"]}]
    with pytest.raises(DomainError) as unwaivable:
        s.record_decision(f["principal"], waive)
    assert unwaivable.value.code == "HARD_CONFLICT_UNRESOLVED"


def test_literary_memory_keeps_counterevidence_without_becoming_world_fact(kernel_env):
    k = kernel_env["kernel"]; f, _, binding = prepared(k); s = ReviewService(k)
    keeper, task = make_review_task(k, f, binding, "literary_memory", role="evidence_maintainer")
    reads = [s.read_text(keeper, task["task_id"], task["generation"], ref, idempotency_key="read:" + str(i))
             for i, ref in enumerate(binding["ordered_draft_refs"])]
    summary = k.put_artifact(keeper, f["world_ref"]["work_id"], "她可以笑而尚未原谅；他已开始承认这层距离。".encode(), "TEXT")
    evidence = [{"kind": "ARTIFACT", "artifact_ref": r["text_ref"], "span": r["span"]} for r in reads]
    candidate = {"schema_version": "2.0", "candidate_id": uid(), "task_id": task["task_id"], "generation": task["generation"],
        "world_ref": binding["world_ref"], "memory_kind": "LITERARY_MEMORY", "plane": "LITERARY", "audience": "WRITER",
        "actor_id": f["ids"]["protagonist"], "source_refs": [evidence[0]], "counterevidence_refs": [evidence[1]],
        "source_coverage": "COMPLETE", "coverage_note": "两场完整原文已交付；解释不是被批准的心理事实。",
        "summary_ref": summary, "method_revision_sha256": fingerprint("P205-evidence-first-v1"), "dependency_fingerprint": "0" * 64,
        "narrative_revision_id": binding["narrative_revision_id"], "entry_kind": "RELATIONSHIP_ASYMMETRY",
        "unresolved_items": ["微笑的心理意义仍保留反解释。"]}
    candidate["dependency_fingerprint"] = memory_dependency(candidate, binding["ordered_draft_refs"])
    request = {"schema_version": "2.1", "candidate": candidate, "source_draft_refs": binding["ordered_draft_refs"],
        "coverage_refs": [r["coverage_ref"] for r in reads], "idempotency_key": "memory"}
    ref = s.save_memory(keeper, request)
    assert s.save_memory(keeper, request) == ref
    with k.db.uow(write=False) as repo:
        stored = k.artifact(repo, ref, f["world_ref"]["work_id"], kind="LITERARY_MEMORY")["payload"]
        assert stored["candidate"]["counterevidence_refs"] == [evidence[1]]
        assert stored["candidate"]["unresolved_items"]
        assert k.head(repo, f["world_ref"]["work_id"]) == f["world_ref"]
    forged = deepcopy(request); forged["candidate"]["memory_kind"] = "FACT_SUMMARY"
    forged["candidate"]["plane"] = "WORLD"; forged["idempotency_key"] = "world-poison"
    with pytest.raises(DomainError) as fact:
        s.save_memory(keeper, forged)
    assert fact.value.code == "INVALID_ARGUMENT"
    missed = deepcopy(request); missed["coverage_refs"] = [reads[0]["coverage_ref"]]; missed["idempotency_key"] = "false-complete"
    with pytest.raises(DomainError) as coverage:
        s.save_memory(keeper, missed)
    assert coverage.value.code == "READING_COVERAGE_INCOMPLETE"
    updated_binding = deepcopy(binding); updated_binding["literary_refs"] = [ref]
    writer, reading_task = make_review_task(k, f, updated_binding, "scene_draft", role="writer")
    delivered = json.loads(s.read_memory_for_task(writer, reading_task["task_id"], reading_task["generation"],
                                                 reading_task["context_bundle_id"], ref))
    assert delivered["interpretation"] == "她可以笑而尚未原谅；他已开始承认这层距离。"
    assert delivered["counterevidence_refs"] == [evidence[1]]
    assert delivered["authority"] == "DERIVED_LITERARY_INTERPRETATION_NOT_WORLD_FACT_OR_ACTOR_BELIEF"
    guessed, guessed_task = make_review_task(k, f, updated_binding, "scene_draft", role="writer", bind_read_scope=False)
    with pytest.raises(DomainError) as outside_source_scope:
        s.read_memory_for_task(guessed, guessed_task["task_id"], guessed_task["generation"], guessed_task["context_bundle_id"], ref)
    assert outside_source_scope.value.code == "VIEW_DENIED"
    approved = approve_binding(k, f, updated_binding)
    assert validate_approval(k, f["principal"], approved["approval_ref"], updated_binding)


def test_revoked_review_cancelled_task_and_editorial_revocation_invalidate_approval(kernel_env):
    k = kernel_env["kernel"]; f, _, binding = prepared(k); s = ReviewService(k)
    result = approve_binding(k, f, binding)
    s.revoke_editorial(f["principal"], binding["world_ref"], result["approval_ref"], "重新核查接缝。", "revoke")
    with pytest.raises(DomainError) as revoked:
        validate_approval(k, f["principal"], result["approval_ref"], binding)
    assert revoked.value.code == "EDITORIAL_APPROVAL_REVOKED"
    second = approve_binding(k, f, binding)
    literary = second["reader_tasks"]["literary"]
    k.cancel_task(literary["principal"], literary["task"]["task_id"], literary["task"]["generation"], "cancel-review")
    with pytest.raises(DomainError) as cancelled:
        validate_approval(k, f["principal"], second["approval_ref"], binding)
    assert cancelled.value.code == "REVIEW_REVOKED"
    third = approve_binding(k, f, binding)
    k.revoke_grant(f["principal"], third["reader_tasks"]["fact"]["principal"].grant_id)
    with pytest.raises(DomainError) as grant:
        validate_approval(k, f["principal"], third["approval_ref"], binding)
    assert grant.value.code == "REVIEW_REVOKED"


def test_self_review_does_not_become_independent_approval(kernel_env):
    k = kernel_env["kernel"]; f, _, binding = prepared(k); s = ReviewService(k)
    result = approve_binding(k, f, binding, approve=False)
    reader, task = make_review_task(k, f, binding, "fact", role="control_operator", principal=f["principal"])
    deliveries = [s.read_text(reader, task["task_id"], task["generation"], ref, idempotency_key="self:" + str(i))
                  for i, ref in enumerate(binding["ordered_draft_refs"])]
    request = review_request(binding, task, [d["coverage_ref"] for d in deliveries], "SCENE", "FACT", [binding["ordered_draft_refs"][0]])
    self_report = s.submit_review(reader, request)
    approval = deepcopy(result["approval_request"]); approval["review_refs"][0] = self_report; approval["idempotency_key"] = "self-approve"
    with pytest.raises(DomainError) as separate:
        s.approve_editorial(f["principal"], approval)
    assert separate.value.code == "APPROVAL_SEPARATION_REQUIRED"


def test_distinct_voice_style_can_be_explicitly_adopted_at_a_new_world_cut(kernel_env):
    k = kernel_env["kernel"]; f = setup_batch(k); s = ReviewService(k); n = NarrativeService(k)
    anchor_text = "甲说：先等等。乙道：此事尚有未明之处。"
    anchor = k.put_artifact(f["principal"], f["world_ref"]["work_id"], anchor_text.encode(), "TEXT")
    style = {"schema_version": "2.1", "style_id": uid(), "style_revision_id": uid(), "world_ref": f["world_ref"],
        "principles": ["允许不同人物在不同语域中表述相似的迟疑。"], "method_refs": [],
        "voices": [{"actor_id": f["ids"]["protagonist"], "name": "甲的短句", "register": "直接而保留停顿",
            "anchors": [{"kind": "ARTIFACT", "artifact_ref": anchor, "span": text_span(anchor_text, 0, 7)}],
            "allowed_variations": ["情绪更强时可以更简短，也可以沉默。"]},
            {"actor_id": f["ids"]["friend"], "name": "乙的迂回", "register": "谨慎、稍带书面色彩",
            "anchors": [{"kind": "ARTIFACT", "artifact_ref": anchor, "span": text_span(anchor_text, 7, len(anchor_text))}],
            "allowed_variations": ["关系靠近后允许偶尔直接，不固定成标签。"]}]}
    style_ref = s.save_style(f["principal"], style, "style")
    request = deepcopy(f["batch_request"]); request["style_refs"] = [style_ref]
    batch_ref = ReleaseService(k).create_writing_batch(f["principal"], request, "styled-batch")
    for index, scene in enumerate(f["scenes"]):
        scene["scene_contract"]["style_refs"] = [style_ref]
        scene.update(batch_ref=batch_ref, prior_scene_ref=f["scene_refs"][index])
        f["scene_refs"][index] = n.save_scene(f["principal"], scene, "styled-scene:" + str(index))
    f["batch_ref"] = batch_ref; f["batch_request"] = request
    drafts = [write_scene(k, f, i, text) for i, text in enumerate(["她只看着雨，半晌没有开口。", "他望了望同一片雨。句末留下一点迟疑。"])]
    accepted = [accept_draft(k, f, draft) for draft in drafts]
    original = binding_for(f, accepted); original["style_refs"] = [style_ref]
    old = approve_binding(k, f, original)
    updated = run_command(k, f["principal"], f["world_ref"], "DESTROY_ITEM", {"item_id": f["ids"]["sword2"], "at": point(2)})["world_ref"]
    adopted = [accept_draft(k, f, draft, target_wr=updated) for draft in drafts]
    binding = binding_for(f, adopted, world_ref=updated); binding["style_refs"] = [style_ref]
    with pytest.raises(DomainError) as old_approval:
        validate_approval(k, f["principal"], old["approval_ref"], binding)
    assert old_approval.value.code == "EDITORIAL_BINDING_MISMATCH"
    fresh = approve_binding(k, f, binding)
    assert validate_approval(k, f["principal"], fresh["approval_ref"], binding)
    with k.db.uow(write=False) as repo:
        assert k.artifact(repo, style_ref, f["world_ref"]["work_id"])["payload"]["voices"] == style["voices"]
        assert len(repo.many("events", {"work_id": f["world_ref"]["work_id"]})) == 1


@pytest.mark.concurrency
def test_two_editors_cannot_advance_the_same_prior_decision_twice(kernel_env):
    k = kernel_env["kernel"]; f, _, binding = prepared(k); s = ReviewService(k)
    proof = approve_binding(k, f, binding, approve=False)
    session = s.start_editing(f["principal"], {**proof["session_request"], "idempotency_key": "parallel-session"})
    requests = []
    for index in range(2):
        request = deepcopy(proof["decision_requests"][0]); request.update(session_ref=session, idempotency_key="concurrent:" + str(index))
        requests.append(request)
    def decide(request):
        try:
            return s.record_decision(f["principal"], request)
        except DomainError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(decide, requests))
    assert sum(isinstance(r, dict) for r in results) == 1
    assert results.count("EDIT_CONFLICT") == 1
