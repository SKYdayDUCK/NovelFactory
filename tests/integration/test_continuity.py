"""P4-01 real release/approval/Retcon integration; prose is TEST_FIXTURE only."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from uuid import uuid4

import pytest
from editorial_helpers import approve_binding, binding_for
from test_history_repair import _reviewed_gift_repair
from test_release import accept_draft, release_request, setup_batch, write_scene

from novel_factory.application.continuity import ContinuityService
from novel_factory.application.release import ReleaseService
from novel_factory.domain.editorial import text_span
from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import point

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def uid():
    return str(uuid4())


def volume(k, fixture=None):
    f = setup_batch(k, fixture=fixture)
    accepted = [accept_draft(k, f, write_scene(k, f, i, text)) for i, text in
                enumerate(["北窗的雨声很轻。", "南岸的水声很远。"])]
    binding = binding_for(f, accepted)
    proof = approve_binding(k, f, binding)
    request = release_request(f, binding, proof["approval_ref"])
    frozen = ReleaseService(k).freeze(f["principal"], request, "volume:" + uid())
    return f, frozen, request


def reviewer(k, f, *, branch_ids=None):
    return k.issue_grant(f["principal"], f["world_ref"]["work_id"], role_id="volume_director",
        branch_ids=branch_ids, capabilities=["READ", "ARTIFACT_WRITE", "APPROVE", "PLAN_READ"],
        planes=["WORLD", "PLAN"], max_story_time=point(1000))["principal"]


@pytest.fixture(scope="module")
def book(kernel_env):
    k = kernel_env["kernel"]
    f, first, first_request = volume(k)
    _, second, _ = volume(k, f)
    return {"kernel": k, "f": f, "first": first, "second": second,
            "first_request": first_request, "reviewer": reviewer(k, f)}


def entry(f, release, ordinal, *, prior=None, kind="CHARACTER_GROWTH"):
    original = "北窗的雨声很轻。"
    return {"entry_id": uid(), "thread_id": prior["thread_id"] if prior else uid(),
        "volume_ordinal": ordinal, "kind": kind, "plane": "LITERARY", "status": "DEVELOPING",
        "entity_ids": [f["ids"]["protagonist"]], "story_interval": {"start": point(1), "end": point(1, 1), "bounds": "[)"},
        "interpretation": "工程夹具：人物观察的距离可能变化；这不是心理事实或文学质量判断。",
        "epistemic_status": "LITERARY_INTERPRETATION", "source_events": [],
        "source_texts": [{"release_ref": release["release_ref"],
            "text_ref": release["manifest"]["ordered_artifacts"][0]["artifact_ref"], "span": text_span(original, 0, len(original))}],
        "supersedes_entry_ids": [prior["entry_id"]] if prior else [], "planned_payoff_at": None}


def request(f, releases, entries=(), *, series_id=None, prior=None, operation="APPEND", reason=None):
    return {"schema_version": "2.3", "series_id": series_id or uid(), "operation": operation,
        "world_ref": f["world_ref"], "expected_prior_ref": prior,
        "ordered_release_refs": [r["release_ref"] for r in releases], "entries": list(entries),
        "revision_reason": reason, "idempotency_key": "prepare:" + uid()}


def qualify(service, p, reviewer_id, wid, body, *, acknowledge=None):
    candidate = service.prepare(p, body)
    delivered = service.read_candidate(reviewer_id, wid, candidate, "read:" + uid())
    affected = [v["ordinal"] for v in delivered["candidate"]["impact"]["affected_volumes"]]
    review_payload = {"schema_version": "2.3", "candidate_ref": candidate,
        "reading_ref": delivered["reading_ref"], "decision": "APPROVED",
        "reason": "独立身份的确定性审核夹具；原文实际交付，未宣称文学评价。",
        "acknowledged_affected_ordinals": affected if acknowledge is None else acknowledge,
        "idempotency_key": "review:" + uid()}
    approved = service.review(reviewer_id, wid, review_payload)
    publish = {"schema_version": "2.3", "candidate_ref": candidate, "review_ref": approved,
               "idempotency_key": "publish:" + uid()}
    return publish, delivered, review_payload


def test_cross_volume_growth_disclosure_and_foreshadow_keep_original_history(book):
    k, f = book["kernel"], book["f"]
    p, wid = f["principal"], f["world_ref"]["work_id"]
    s = ContinuityService(k)
    first = entry(f, book["first"], 1)
    foreshadow = entry(f, book["first"], 1, kind="FORESHADOW")
    foreshadow.update(status="OPEN", planned_payoff_at=point(40))
    initial = request(f, [book["first"]], [first, foreshadow])
    pub, _, _ = qualify(s, p, book["reviewer"], wid, initial)
    a = s.publish(p, wid, pub)
    before = s.inspect(p, wid, a["snapshot_ref"])["snapshot"]
    evolution = entry(f, book["second"], 2, prior=first)
    resolved = entry(f, book["second"], 2, prior=foreshadow, kind="FORESHADOW")
    resolved.update(status="RESOLVED")
    reader_entry = entry(f, book["second"], 2, kind="READER_DISCLOSURE")
    reader_entry.update(plane="READER", epistemic_status="TEXT_ASSERTION_NOT_WORLD_FACT")
    next_request = request(f, [book["first"], book["second"]], [first, foreshadow, evolution, resolved, reader_entry],
        series_id=initial["series_id"], prior=a["snapshot_ref"])
    pub, _, _ = qualify(s, p, book["reviewer"], wid, next_request)
    b = s.publish(p, wid, pub)
    current = s.inspect(p, wid, b["snapshot_ref"])
    assert current["snapshot"]["active_thread_entries"][first["thread_id"]] == evolution["entry_id"]
    assert current["snapshot"]["active_thread_entries"][foreshadow["thread_id"]] == resolved["entry_id"]
    assert current["snapshot"]["request"]["entries"][-1]["epistemic_status"] == "TEXT_ASSERTION_NOT_WORLD_FACT"
    assert current["qualification"] == "NO_NEW_IMPACT_SINCE_REVIEW"
    assert s.inspect(p, wid, a["snapshot_ref"])["snapshot"] == before
    with k.db.uow(write=False) as repo:
        assert k.head(repo, wid) == f["world_ref"]
        assert not repo.many("events", {"work_id": wid})
    assert b["world_effects_applied"] == 0


def test_serial_prefix_is_protected_and_reordered_edition_requires_explicit_review(book):
    k, f = book["kernel"], book["f"]
    p, wid, s = f["principal"], f["world_ref"]["work_id"], ContinuityService(k)
    initial = request(f, [book["first"], book["second"]])
    pub, _, _ = qualify(s, p, book["reviewer"], wid, initial)
    frozen = s.publish(p, wid, pub)
    with pytest.raises(DomainError) as protected:
        ReleaseService(k).freeze(p, {**book["first_request"], "prior_release_ref": book["first"]["release_ref"],
            "revision_reason": "Attempt to advance a protected volume"}, "protected:" + uid())
    assert protected.value.code == "SERIAL_PREFIX_PROTECTED"
    changed = request(f, [book["second"], book["first"]], series_id=initial["series_id"], prior=frozen["snapshot_ref"])
    with pytest.raises(DomainError) as reordered:
        s.prepare(p, changed)
    assert reordered.value.code == "SERIAL_PREFIX_PROTECTED"
    changed.update(operation="REVISE", revision_reason="显式重排新版，旧版保留且独立审核全部后续卷。")
    candidate = s.prepare(p, changed)
    reading = s.read_candidate(book["reviewer"], wid, candidate, "read:" + uid())
    bad = {"schema_version": "2.3", "candidate_ref": candidate, "reading_ref": reading["reading_ref"],
        "decision": "APPROVED", "reason": "漏审第二卷", "acknowledged_affected_ordinals": [1], "idempotency_key": uid()}
    with pytest.raises(DomainError) as missing:
        s.review(book["reviewer"], wid, bad)
    assert missing.value.code == "CONTINUITY_IMPACT_UNREVIEWED"
    good = {**bad, "acknowledged_affected_ordinals": [1, 2], "idempotency_key": uid()}
    approved = s.review(book["reviewer"], wid, good)
    newer = s.publish(p, wid, {"schema_version": "2.3", "candidate_ref": candidate,
        "review_ref": approved, "idempotency_key": uid()})
    assert s.inspect(p, wid, frozen["snapshot_ref"])["snapshot"]["request"]["ordered_release_refs"] == initial["ordered_release_refs"]
    assert s.inspect(p, wid, newer["snapshot_ref"])["snapshot"]["request"]["operation"] == "REVISE"


def test_distinct_review_delivery_and_live_authorization_are_required(book):
    k, f = book["kernel"], book["f"]
    p, wid, s = f["principal"], f["world_ref"]["work_id"], ContinuityService(k)
    candidate = s.prepare(p, request(f, [book["first"]]))
    reading = s.read_candidate(p, wid, candidate, uid())
    own = {"schema_version": "2.3", "candidate_ref": candidate, "reading_ref": reading["reading_ref"],
        "decision": "APPROVED", "reason": "Self review", "acknowledged_affected_ordinals": [], "idempotency_key": uid()}
    with pytest.raises(DomainError) as self_review:
        s.review(p, wid, own)
    assert self_review.value.code == "SELF_APPROVAL_DENIED"
    other = reviewer(k, f)
    with pytest.raises(DomainError) as stolen_read:
        s.review(other, wid, {**own, "idempotency_key": uid()})
    assert stolen_read.value.code == "INVALID_READING_COVERAGE"
    pub, _, _ = qualify(s, p, other, wid, request(f, [book["first"]]))
    k.revoke_grant(p, other.grant_id)
    with pytest.raises(DomainError) as revoked:
        s.publish(p, wid, pub)
    assert revoked.value.code in {"VIEW_DENIED", "APPROVAL_REVOKED"}


def test_actor_and_restricted_horizon_cannot_use_series_artifact_as_a_read_bypass(book):
    k, f = book["kernel"], book["f"]
    p, wid, s = f["principal"], f["world_ref"]["work_id"], ContinuityService(k)
    content = entry(f, book["first"], 1, kind="FORESHADOW")
    content.update(status="OPEN", planned_payoff_at=point(40))
    pub, _, _ = qualify(s, p, book["reviewer"], wid, request(f, [book["first"]], [content]))
    result = s.publish(p, wid, pub)
    actor = k.issue_grant(p, wid, role_id="character_simulator", actor_id=f["ids"]["protagonist"],
        capabilities=["READ"], planes=["BELIEF"], max_story_time=point(100))["principal"]
    with pytest.raises(DomainError) as denied:
        s.inspect(actor, wid, result["snapshot_ref"])
    assert denied.value.code == "VIEW_DENIED"
    bounded = k.issue_grant(p, wid, role_id="volume_director",
        capabilities=["READ", "PLAN_READ"], planes=["WORLD", "PLAN"], max_story_time=point(10))["principal"]
    with pytest.raises(DomainError) as future:
        s.inspect(bounded, wid, result["snapshot_ref"])
    assert future.value.code == "VIEW_DENIED"


@pytest.mark.concurrency
def test_same_series_concurrent_publish_has_one_winner_and_no_world_effect(book):
    k, f = book["kernel"], book["f"]
    p, wid, s = f["principal"], f["world_ref"]["work_id"], ContinuityService(k)
    sid = uid()
    first, _, _ = qualify(s, p, book["reviewer"], wid, request(f, [book["first"]], series_id=sid))
    second, _, _ = qualify(s, p, book["reviewer"], wid, request(f, [book["first"], book["second"]], series_id=sid))
    def publish(payload):
        try:
            return s.publish(p, wid, payload)
        except DomainError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(publish, [first, second]))
    assert sum(isinstance(item, dict) for item in results) == 1
    assert "CONTINUITY_CONFLICT" in results


@pytest.mark.fault
def test_publish_rollback_and_lost_reply_recover_exact_receipt(book):
    k, f = book["kernel"], book["f"]
    p, wid, s = f["principal"], f["world_ref"]["work_id"], ContinuityService(k)
    body = request(f, [book["first"]])
    pub, _, _ = qualify(s, p, book["reviewer"], wid, body)
    def crash_before(stage):
        if stage == "before_commit":
            raise RuntimeError("before series commit")
    with pytest.raises(RuntimeError, match="before"):
        s.publish(p, wid, pub, fault=crash_before)
    with k.db.uow(write=False) as repo:
        assert repo.one("selections", {"work_id": wid, "selection_kind": "CONTINUITY:" + body["series_id"]}) is None
    def lose_response(stage):
        if stage == "after_commit":
            raise RuntimeError("lost series response")
    with pytest.raises(RuntimeError, match="lost"):
        s.publish(p, wid, pub, fault=lose_response)
    recovered = s.publish(p, wid, pub)
    assert recovered == s.publish(p, wid, pub)
    assert recovered["selection_epoch"] == 1
    with k.db.uow(write=False) as repo:
        rows = repo.many("artifacts", {"work_id": wid, "kind": "CONTINUITY_SNAPSHOT"})
        assert sum(row["payload"]["request"]["series_id"] == body["series_id"] for row in rows) == 1


def test_retcon_propagates_to_later_volumes_and_requires_fresh_evidence(kernel_env):
    k = kernel_env["kernel"]
    history = _reviewed_gift_repair(k)
    f, first, _ = volume(k, history)
    _, second, _ = volume(k, history)
    p, wid, old = f["principal"], f["world_ref"]["work_id"], history["old"]
    s = ContinuityService(k)
    old_event = history["loan"]["receipt"]["accepted_event_refs"][0]
    resource = entry(f, first, 1, kind="RESOURCE_HISTORY")
    resource.update(plane="WORLD_EVIDENCE", epistemic_status="EVENT_REFERENCE",
        source_events=[{"world_ref": old, "event_ref": old_event}],
        story_interval={"start": point(20), "end": point(21), "bounds": "[)"})
    initial = request(f, [first, second], [resource])
    pub, _, _ = qualify(s, p, reviewer(k, f), wid, initial)
    initial_receipt = s.publish(p, wid, pub)
    old_export = ReleaseService(k).export_release(p, wid, first["release_ref"])
    new = k.promote(p, old, history["repair"]["world_ref"], "continuity-retcon:" + uid())
    impact = s.inspect(p, wid, initial_receipt["snapshot_ref"])
    assert impact["qualification"] == "HISTORICAL_EDITION_REVIEW_REQUIRED"
    assert [v["ordinal"] for v in impact["current_impact"]["affected_volumes"]] == [1, 2]
    assert old_event["occurrence_id"] in impact["current_impact"]["changed_occurrence_ids"]
    new_f, third, _ = volume(k, {**f, "world_ref": new})
    evolved = entry(new_f, third, 3, prior=resource, kind="RESOURCE_HISTORY")
    evolved.update(plane="WORLD_EVIDENCE", epistemic_status="EVENT_REFERENCE",
        source_events=[{"world_ref": new, "event_ref": history["repair"]["receipt"]["accepted_event_refs"][0]}],
        story_interval={"start": point(20), "end": point(21), "bounds": "[)"})
    successor = request(new_f, [first, second, third], [resource, evolved],
        series_id=initial["series_id"], prior=initial_receipt["snapshot_ref"])
    with pytest.raises(DomainError) as stale:
        s.prepare(p, successor)
    assert stale.value.code == "CONTINUITY_REVISION_REQUIRED"
    successor.update(operation="REVISE", revision_reason="借用修订为赠与，旧版只作历史来源，新版重审后续卷。")
    candidate = s.prepare(p, successor)
    with pytest.raises(DomainError) as insufficient_history_scope:
        s.read_candidate(reviewer(k, new_f), wid, candidate, uid())
    assert insufficient_history_scope.value.code == "VIEW_DENIED"
    scoped_reviewer = reviewer(k, new_f, branch_ids=[old["branch_id"], new["branch_id"]])
    pub, delivery, _ = qualify(s, p, scoped_reviewer, wid, successor)
    assert len(delivery["original_texts"]) == 6
    rewritten = s.publish(p, wid, pub)
    current = s.inspect(p, wid, rewritten["snapshot_ref"])
    assert current["qualification"] == "NO_NEW_IMPACT_SINCE_REVIEW"
    assert current["snapshot"]["entry_evidence_status"][resource["entry_id"]] == "HISTORICAL_SOURCE_ONLY"
    assert current["snapshot"]["entry_evidence_status"][evolved["entry_id"]] == "CURRENT_SOURCE_BOUND"
    assert current["snapshot"]["historical_release_ordinals"] == [1, 2]
    assert ReleaseService(k).export_release(p, wid, first["release_ref"]) == old_export
    assert s.publish(p, wid, pub) == rewritten
    with k.db.uow(write=False) as repo:
        assert k.head(repo, wid) == new
        assert k.revision(repo, old)["selection"][old_event["occurrence_id"]] == old_event["event_version_id"]
    # Once the changed history has been explicitly reviewed, later volumes can
    # append while keeping the superseded old evidence as immutable history.
    _, fourth, _ = volume(k, new_f)
    resumed = request(new_f, [first, second, third, fourth], [resource, evolved],
        series_id=initial["series_id"], prior=rewritten["snapshot_ref"])
    pub, _, _ = qualify(s, p, scoped_reviewer, wid, resumed)
    appended = s.publish(p, wid, pub)
    continued = s.inspect(p, wid, appended["snapshot_ref"])
    assert continued["qualification"] == "NO_NEW_IMPACT_SINCE_REVIEW"
    assert continued["snapshot"]["entry_evidence_status"][resource["entry_id"]] == "HISTORICAL_SOURCE_ONLY"
    assert len(continued["snapshot"]["volumes"]) == 4


def test_task_delivery_is_generation_bound_and_never_exposes_future_plan_fields(book):
    from editorial_helpers import make_review_task
    k, f = book["kernel"], book["f"]
    p, wid, s = f["principal"], f["world_ref"]["work_id"], ContinuityService(k)
    content = entry(f, book["first"], 1, kind="FORESHADOW")
    content.update(status="OPEN", planned_payoff_at=point(40))
    pub, _, _ = qualify(s, p, book["reviewer"], wid, request(f, [book["first"]], [content]))
    frozen = s.publish(p, wid, pub)
    with k.db.uow(write=False) as repo:
        binding = deepcopy(k.artifact(repo, book["first_request"]["editorial_approval_ref"], wid)["payload"]["binding"])
    binding["literary_refs"] = [frozen["snapshot_ref"]]
    writer, task = make_review_task(k, f, binding, "scene_draft", role="writer")
    delivered = s.read_for_task(writer, task["task_id"], task["generation"], frozen["snapshot_ref"])
    assert delivered["entries"][0]["entry_id"] == content["entry_id"]
    assert "planned_payoff_at" not in delivered["entries"][0]
    with pytest.raises(DomainError) as raw:
        k.get_artifact(writer, wid, frozen["snapshot_ref"], task_id=task["task_id"],
            generation=task["generation"], context_bundle_id=task["context_bundle_id"],
            world_ref=f["world_ref"], story_time=point(1000), knowledge_cut={"plane": "WORLD"})
    assert raw.value.code == "VIEW_DENIED"
    with pytest.raises(DomainError) as budget:
        s.read_for_task(writer, task["task_id"], task["generation"], frozen["snapshot_ref"], budget_bytes=1)
    assert budget.value.code == "CONTEXT_BUDGET_EXCEEDED"
    k.cancel_task(writer, task["task_id"], task["generation"], "cancel-continuity:" + uid())
    with pytest.raises(DomainError) as cancelled:
        s.read_for_task(writer, task["task_id"], task["generation"], frozen["snapshot_ref"])
    assert cancelled.value.code in {"STALE_GENERATION", "TASK_NOT_ACCEPTABLE", "TASK_CANCELLED"}


def test_event_added_after_release_still_invalidates_derived_volume_memory(kernel_env):
    k = kernel_env["kernel"]
    published_fixture, first, _ = volume(k)
    history = _reviewed_gift_repair(k, fixture=published_fixture)
    f, old = history, history["old"]
    p, wid, s = f["principal"], old["work_id"], ContinuityService(k)
    old_event = history["loan"]["receipt"]["accepted_event_refs"][0]
    with k.db.uow(write=False) as repo:
        release_cut = k.revision(repo, first["manifest"]["world_ref"])
        assert old_event["occurrence_id"] not in release_cut["selection"]
    resource = entry(f, first, 1, kind="RESOURCE_HISTORY")
    resource.update(plane="WORLD_EVIDENCE", epistemic_status="EVENT_REFERENCE",
        source_events=[{"world_ref": old, "event_ref": old_event}],
        story_interval={"start": point(20), "end": point(21), "bounds": "[)"})
    future_source = deepcopy(resource)
    future_source["story_interval"] = {"start": point(1), "end": point(2), "bounds": "[)"}
    with pytest.raises(DomainError) as time_leak:
        s.prepare(p, request(f, [first], [future_source]))
    assert time_leak.value.code == "INVALID_EVIDENCE"
    pub, _, _ = qualify(s, p, reviewer(k, f), wid, request(f, [first], [resource]))
    initial = s.publish(p, wid, pub)
    k.promote(p, old, history["repair"]["world_ref"], "memory-after-release:" + uid())
    impact = s.inspect(p, wid, initial["snapshot_ref"])["current_impact"]
    assert impact["changed_occurrence_ids"] == [old_event["occurrence_id"]]
    assert "SELECTED_EVENT_CHANGED" in impact["affected_volumes"][0]["reasons"]
