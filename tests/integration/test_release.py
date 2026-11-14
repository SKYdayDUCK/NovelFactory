"""P2-06 real-PG writing batches, review-bound release CAS and recovery.

The prose/review choices are explicit engineering fixtures, never evidence of
real-model literary quality. All acceptance and review artifacts use services.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from editorial_helpers import approve_binding, binding_for

from novel_factory.application.editorial import ReviewService
from novel_factory.application.narrative import NarrativeService
from novel_factory.application.planning import PlanningService
from novel_factory.application.queries import WorldQueryService
from novel_factory.application.release import ReleaseService
from novel_factory.contracts.registry import validate_contract
from novel_factory.domain.canonical import fingerprint
from novel_factory.domain.errors import DomainError
from novel_factory.domain.reconciliation import span_for
from novel_factory.fixtures import entity_value, load_fixture, point, run_command

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def uid():
    return str(uuid4())


def task_for(k, f, principal, inputs, kind="scene", *, wr=None, story_time=None):
    wr = wr or f["world_ref"]
    story_time = story_time or point(1, 1)
    with k.db.uow(write=False) as repo:
        grant = repo.one("grants", {"grant_id": principal.grant_id})
    manifest = k.put_artifact(principal, wr["work_id"],
        {"subject_ids": [f["ids"]["sword"]], "predicates": ["possessed_by", "lifecycle"]}, "INPUT_MANIFEST")
    tid = uid()
    spec = {"schema_version": "2.0", "task_id": tid, "generation": 0, "role_id": grant["role_id"],
        "task_kind": kind, "state": "READY", "world_ref": wr, "story_time": story_time,
        "context_bundle_id": None, "policy_version": "nf-policy-1", "dependency_fingerprint": None,
        "runtime_contract_version": fingerprint("P206-engineering-fixture"), "capability_grant_id": principal.grant_id,
        "input_artifacts": [manifest, *inputs], "budget": {"currency": "USD", "max_amount": "1", "token_budget": 50000},
        "wall_deadline": (datetime.now(UTC) + timedelta(hours=1)).isoformat()}
    k.create_task(principal, spec, "create:" + tid)
    task = k.claim_task(principal, "worker:" + tid, lease_seconds=3600, task_id=tid)
    recipe = "WRITER" if grant["role_id"] == "writer" else "REVIEWER" if grant["role_id"] == "reviewer" else "ADJUDICATOR"
    WorldQueryService(k).context_compile(principal, {"schema_version": "2.1", "task_id": tid, "generation": task["generation"],
        "recipe_id": recipe, "input_manifest_ref": manifest, "read_contract": {"revision": {"mode": "FIXED", "world_ref": wr},
        "process_policy": "ALLOW_PARTIAL", "index_policy": "AUTHORITATIVE", "max_wait_ms": 0},
        "knowledge_cut": {"plane": "WORLD"}, "subject_time": {"kind": "POINT", "at": story_time},
        "token_budget": 50000, "idempotency_key": "compile:" + tid})
    return k.get_task(principal, tid)


def runtime_grant(k, f, role):
    return k.issue_grant(f["principal"], f["world_ref"]["work_id"], role_id=role,
        capabilities=["READ", "ARTIFACT_WRITE", "TASK_CREATE", "TASK_CLAIM", "COMPILE"],
        planes=["WORLD"], max_story_time=point(100))["principal"]


def setup_batch(k, *, count=2, dependent=False, fixture=None):
    f = fixture if fixture is not None else load_fixture(k)
    p, wr = f["principal"], f["world_ref"]
    planning = PlanningService(k)
    volume_id, narrative_id = uid(), uid()
    key_prefix = volume_id + ":" if fixture is not None else ""
    volume = {"schema_version": "2.0", "volume_id": volume_id, "contract_revision_id": uid(), "input_world_ref": wr,
        "creative_brief": "固定世界下两条独立场景，交接不靠私聊", "focus_subject_ids": [f["ids"]["protagonist"]],
        "narrative_intents": ["保留人物的迟疑与不同声线"], "protected_commitments": [], "open_choices": ["后继仍须正式接受"],
        "completion_or_stop_conditions": ["通过精确版本的整卷审核"], "budget": {"max_semantic_calls": 20, "max_candidate_branches": 1},
        "upstream_literary_refs": [], "dependency_fingerprint": fingerprint(wr)}
    volume_ref = planning.save_volume_contract(p, volume, key_prefix + "volume")
    nodes = [{"node_id": uid(), "goal": "描写第" + str(i) + "条线的未决选择", "status": "VIABLE",
        "target_time": {"kind": "EXACT", "at": point(1)}, "subject_ids": [f["ids"]["protagonist"]],
        "hard_preconditions": [], "risk_notes": ["这是一份工程夹具"], "failure_exit": "保留未决问题", "fulfillment_event_refs": []} for i in range(count)]
    plan = {"schema_version": "2.1", "plan_id": uid(), "plan_revision_id": uid(), "volume_id": volume_id,
        "input_world_ref": wr, "volume_contract_ref": volume_ref, "prior_plan_ref": None, "nodes": nodes,
        "published_prefix_node_ids": [], "dependencies": [], "decision": {"reason": "固定输入工程验收",
        "considered_options": [{"option_id": uid(), "summary": "显式前驱交接", "disposition": "SELECTED", "reason": "可验证"}]}}
    plan_ref = planning.save_plan(p, plan, key_prefix + "plan")
    planning.adopt_plan(p, wr, plan_ref, None, key_prefix + "adopt")
    return batch_for_plan(k, f, plan, plan_ref, narrative_id=narrative_id, key_prefix=key_prefix, dependent=dependent)


def batch_for_plan(k, f, plan, plan_ref, *, narrative_id, key_prefix, dependent=False):
    p, wr = f["principal"], f["world_ref"]
    narrative, release = NarrativeService(k), ReleaseService(k)
    volume_id, nodes = plan["volume_id"], plan["nodes"]
    scene_ids = [uid() for _ in nodes]
    interval = {"start": point(1), "end": point(1, 1), "bounds": "[)"}
    slots = [{"scene_id": sid, "story_interval": interval, "reader_cut_ordinal": i,
              "required_predecessor_scene_ids": scene_ids[:2] if dependent and i == 2 else []} for i, sid in enumerate(scene_ids)]
    batch_request = {"schema_version": "2.2", "world_ref": wr, "volume_id": volume_id, "plan_ref": plan_ref,
        "style_refs": [], "narrative_revision_id": narrative_id, "arrangement_version": "arrangement-1", "scene_slots": slots}
    batch_ref = release.create_writing_batch(p, batch_request, key_prefix + "batch")
    scenes, scene_refs = [], []
    for i, slot in enumerate(slots):
        contract = {"schema_version": "2.0", "scene_id": slot["scene_id"], "input_world_ref": wr,
            "story_interval": interval, "event_refs": [], "entry_commitments": [], "exit_commitments": [],
            "pov_actor_id": f["ids"]["protagonist"], "narrator_mode": "limited", "narrative_revision_id": narrative_id,
            "arrangement_version": "arrangement-1", "reader_cut_ordinal": i, "allowed_invention": ["非持久的感官细节"],
            "style_refs": [], "dependency_fingerprint": fingerprint({"world": wr, "plan": plan_ref, "slot": slot})}
        check = {"check_id": uid(), "plane": "WORLD", "subject_id": f["ids"]["sword"], "predicate": "possessed_by",
                 "at": point(1), "expected_status": "VALUE", "expected_value": entity_value(f["ids"]["smith"]),
                 "actor_id": None, "known_at": None}
        scene = {"schema_version": "2.2", "scene_contract": contract, "plan_ref": plan_ref,
            "plan_node_ids": [nodes[i]["node_id"]], "batch_ref": batch_ref, "prior_scene_ref": None,
            "required_event_refs": [], "required_predecessor_scene_ids": slot["required_predecessor_scene_ids"],
            "entry_checks": [check], "exit_checks": [], "allowed_disclosures": [], "literary_purpose": "保持独立叙事声线"}
        scene_refs.append(narrative.save_scene(p, scene, key_prefix + "scene:" + str(i)))
        scenes.append(scene)
    return {**f, "plan": plan, "plan_ref": plan_ref, "volume_id": volume_id, "narrative_revision_id": narrative_id,
            "batch_ref": batch_ref, "batch_request": batch_request, "scenes": scenes, "scene_refs": scene_refs}


def writer_for(k, f, index, parents=(), *, extra_inputs=()):
    writer = runtime_grant(k, f, "writer")
    task = task_for(k, f, writer, [f["plan_ref"], f["batch_ref"], f["scene_refs"][index], *parents, *extra_inputs])
    PlanningService(k).bind_task_for_controller(f["principal"], task["task_id"], task["generation"], f["plan_ref"],
        f["scenes"][index]["plan_node_ids"], "planbind:" + task["task_id"])
    return writer, task


def write_scene(k, f, index, text, parents=(), *, prior_draft_ref=None, declared_important_deltas=()):
    writer, task = writer_for(k, f, index, parents, extra_inputs=[prior_draft_ref] if prior_draft_ref else [])
    ReleaseService(k).bind_writer_task(writer, task["task_id"], task["generation"], f["scene_refs"][index],
        list(parents), "writebind:" + task["task_id"])
    ref = NarrativeService(k).store_draft(writer, task["task_id"], task["generation"], f["scene_refs"][index], text,
        "draft:" + task["task_id"], handoff_refs=list(parents), prior_draft_ref=prior_draft_ref,
        declared_important_deltas=list(declared_important_deltas))
    return {"writer": writer, "task": task, "draft_ref": ref, "text": text}


def accept_draft(k, f, draft, *, target_wr=None):
    service = NarrativeService(k)
    target_wr = target_wr or f["world_ref"]
    reader = runtime_grant(k, f, "reviewer")
    task = task_for(k, f, reader, [draft["draft_ref"]], "fact", wr=target_wr)
    service.bind_reader_task(f["principal"], task["task_id"], task["generation"], [draft["draft_ref"]], "readbind:" + task["task_id"])
    delivery = service.read_text(reader, task["task_id"], task["generation"], draft["draft_ref"])
    assertions = [{"assertion_id": uid(), "classification": "SURFACE_DETAIL", "span": span_for(draft["text"]),
        "summary": "测试输入明确仅有表层感官表达，不新增交易", "event_ref": None, "world_check": None,
        "knowledge_use": None, "proposed_command": None, "disclosure_id": None, "declared_delta_index": None}]
    extraction_ref = service.submit_extraction(reader, task["task_id"], task["generation"], draft["draft_ref"],
        delivery["delivery_ref"], assertions, "extract:" + task["task_id"])
    if target_wr == f["world_ref"]:
        report = service.reconcile(f["principal"], draft["draft_ref"], extraction_ref, target_wr, "reconcile:" + task["task_id"])
    else:
        report = service.build_alignment(f["principal"], draft["draft_ref"], extraction_ref, target_wr, "align:" + task["task_id"])
    acceptance = service.accept_narrative(f["principal"], draft["draft_ref"], report["reconciliation_ref"], target_wr,
        "accept:" + task["task_id"], alignment_ref=report["alignment_ref"])
    return {**draft, "reconciliation_ref": report["reconciliation_ref"], "alignment_ref": report["alignment_ref"],
            "acceptance_ref": acceptance, "extraction_ref": extraction_ref}


def release_request(f, binding, approval_ref, *, prior_release_ref=None, revision_reason=None):
    return {"schema_version": "2.2", "world_ref": binding["world_ref"], "volume_id": f["volume_id"],
        "plan_ref": binding["plan_ref"], "batch_refs": [f["batch_ref"]],
        "ordered_acceptance_refs": binding["ordered_acceptance_refs"], "editorial_approval_ref": approval_ref,
        "narrative_revision_id": binding["narrative_revision_id"], "arrangement_version": binding["arrangement_version"],
        "disclosure_revision_id": binding["disclosure_revision_id"], "style_refs": binding["style_refs"],
        "literary_refs": binding["literary_refs"], "prior_release_ref": prior_release_ref, "revision_reason": revision_reason}


def complete_candidate(k):
    f = setup_batch(k)
    accepted = [accept_draft(k, f, write_scene(k, f, i, text)) for i, text in
                enumerate(["北窗的雨声很轻。", "南岸的水声很远。"])]
    binding = binding_for(f, accepted)
    proof = approve_binding(k, f, binding)
    return f, accepted, binding, proof, release_request(f, binding, proof["approval_ref"])


def assert_no_release(k, f):
    with k.db.uow(write=False) as repo:
        assert not repo.many("artifacts", {"work_id": f["world_ref"]["work_id"], "kind": "RELEASE_RECORD"})
        assert repo.one("selections", {"work_id": f["world_ref"]["work_id"], "selection_kind": "RELEASE:" + f["volume_id"]}) is None


def test_batch_rejects_mixed_inputs_cycle_and_actor_access(kernel_env):
    k = kernel_env["kernel"]
    f = setup_batch(k)
    s = ReleaseService(k)
    assert s.create_writing_batch(f["principal"], f["batch_request"], "batch") == f["batch_ref"]
    bad = deepcopy(f["batch_request"])
    bad["scene_slots"][0]["required_predecessor_scene_ids"] = [bad["scene_slots"][1]["scene_id"]]
    bad["scene_slots"][1]["required_predecessor_scene_ids"] = [bad["scene_slots"][0]["scene_id"]]
    with pytest.raises(DomainError) as cycle:
        s.create_writing_batch(f["principal"], bad, "cycle")
    assert cycle.value.code == "SCENE_DEPENDENCY_CYCLE"
    wrong = deepcopy(f["scenes"][0]); wrong["scene_contract"]["narrative_revision_id"] = uid()
    with pytest.raises(DomainError) as cut:
        NarrativeService(k).save_scene(f["principal"], wrong, "wrong-version")
    assert cut.value.code == "BATCH_BINDING_MISMATCH"
    actor = k.issue_grant(f["principal"], f["world_ref"]["work_id"], role_id="character_actor", actor_id=f["ids"]["protagonist"],
        capabilities=["READ", "ARTIFACT_WRITE"], planes=["BELIEF"], max_story_time=point(100))["principal"]
    with pytest.raises(DomainError) as denied:
        s.ready_scenes(actor, f["batch_ref"])
    assert denied.value.code == "VIEW_DENIED"
    absent = {**f["batch_ref"], "artifact_revision_id": uid()}
    with pytest.raises(DomainError) as hidden:
        s.ready_scenes(actor, absent)
    assert (hidden.value.code, hidden.value.safe_message) == (denied.value.code, denied.value.safe_message)


@pytest.mark.concurrency
def test_two_real_writer_tasks_parallel_and_successor_cannot_skip_handoffs(kernel_env):
    k = kernel_env["kernel"]
    f = setup_batch(k, count=3, dependent=True)
    s = ReleaseService(k)
    ids = [scene["scene_contract"]["scene_id"] for scene in f["scenes"]]
    readiness = s.ready_scenes(f["principal"], f["batch_ref"])
    assert readiness["ready_scene_ids"] == ids[:2]
    writer, task = writer_for(k, f, 2)
    with pytest.raises(DomainError) as missing:
        s.bind_writer_task(writer, task["task_id"], task["generation"], f["scene_refs"][2], [], "missing")
    assert missing.value.code == "SCENE_DEPENDENCY_PENDING"
    with pytest.raises(DomainError) as bypass:
        NarrativeService(k).store_draft(writer, task["task_id"], task["generation"], f["scene_refs"][2], "不合法的提前后继。", "bypass")
    assert bypass.value.code == "SCENE_DEPENDENCY_PENDING"
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(write_scene, k, f, i, text) for i, text in enumerate(["北窗的雨声很轻。", "南岸的水声很远。"])]
        drafts = [future.result() for future in futures]
    assert len({draft["task"]["task_id"] for draft in drafts}) == 2
    for draft in drafts:
        with pytest.raises(DomainError) as plan_denied:
            PlanningService(k).get_plan(draft["writer"], f["world_ref"]["work_id"], f["plan_ref"])
        assert plan_denied.value.code == "VIEW_DENIED"
    accepted = [accept_draft(k, f, draft) for draft in drafts]
    handoffs = [item["acceptance_ref"] for item in accepted]
    assert s.ready_scenes(f["principal"], f["batch_ref"], handoffs)["ready_scene_ids"] == ids[2:]
    # The previously created generation cannot adopt new inputs in place.
    with pytest.raises(DomainError) as stale_inputs:
        s.bind_writer_task(writer, task["task_id"], task["generation"], f["scene_refs"][2], handoffs, "mutate-inputs")
    assert stale_inputs.value.code == "TASK_REBIND_REQUIRED"
    followup = write_scene(k, f, 2, "两种雨声隔着一阵风。", handoffs)
    accept_draft(k, f, followup)
    with k.db.uow(write=False) as repo:
        assert k.head(repo, f["world_ref"]["work_id"]) == f["world_ref"]
        assert repo.many("events", {"work_id": f["world_ref"]["work_id"]}) == []


def test_release_complete_exact_manifest_idempotency_and_immutable_prior(kernel_env):
    k = kernel_env["kernel"]
    f, _, _, _, request = complete_candidate(k)
    s, p, wid = ReleaseService(k), f["principal"], f["world_ref"]["work_id"]
    frozen = s.freeze(p, request, "freeze")
    assert s.freeze(p, request, "freeze") == frozen
    validate_contract("ReleaseManifest", frozen["manifest"])
    assert frozen["manifest"]["world_ref"] == f["world_ref"]
    assert frozen["external_delivery"] == "NOT_REQUESTED" and frozen["world_effects_applied"] == 0
    old = s.get_release(p, wid, frozen["release_ref"])
    assert s.export_release(p, wid, frozen["release_ref"])["text"] == "北窗的雨声很轻。\n\n南岸的水声很远。"
    with pytest.raises(DomainError) as duplicate:
        s.freeze(p, request, "different-key-same-prior")
    assert duplicate.value.code == "RELEASE_SELECTION_CONFLICT"
    updated = {**request, "prior_release_ref": frozen["release_ref"], "revision_reason": "显式再冻结保留既有文本，检验版本追溯。"}
    second = s.freeze(p, updated, "next-freeze")
    assert second["selection_epoch"] == 2
    assert second["manifest"]["prior_release_id"] == frozen["manifest"]["release_id"]
    assert s.get_release(p, wid, frozen["release_ref"]) == old
    with k.db.uow(write=False) as repo:
        assert k.head(repo, wid) == f["world_ref"]
        assert len(repo.many("artifacts", {"work_id": wid, "kind": "RELEASE_RECORD"})) == 2
        assert not repo.many("events", {"work_id": wid})


@pytest.mark.concurrency
def test_competing_first_releases_use_cas_not_completion_order_overwrite(kernel_env):
    k = kernel_env["kernel"]
    f, _, _, _, request = complete_candidate(k)
    barrier = Barrier(2)

    def freeze(key):
        def pause(stage):
            if stage == "after_objects":
                barrier.wait(timeout=20)
        try:
            return ReleaseService(k).freeze(f["principal"], request, key, fault=pause)
        except DomainError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(freeze, key) for key in ("candidate-a", "candidate-b")]
        results = [future.result() for future in futures]
    assert sum(isinstance(result, dict) for result in results) == 1
    assert "RELEASE_SELECTION_CONFLICT" in results
    with k.db.uow(write=False) as repo:
        assert len(repo.many("artifacts", {"work_id": f["world_ref"]["work_id"], "kind": "RELEASE_RECORD"})) == 1


@pytest.mark.fault
def test_release_rollback_and_lost_receipt_recovery_do_not_duplicate_effects(kernel_env):
    k = kernel_env["kernel"]
    f, _, _, _, request = complete_candidate(k)
    s = ReleaseService(k)

    def fail_before(stage):
        if stage == "before_commit":
            raise RuntimeError("injected before release transaction commit")

    with pytest.raises(RuntimeError, match="before release"):
        s.freeze(f["principal"], request, "recover", fault=fail_before)
    assert_no_release(k, f)

    def lose_receipt(stage):
        if stage == "after_commit":
            raise RuntimeError("injected lost release receipt")

    with pytest.raises(RuntimeError, match="lost release receipt"):
        s.freeze(f["principal"], request, "recover", fault=lose_receipt)
    result = s.freeze(f["principal"], request, "recover")
    assert result["selection_epoch"] == 1
    with k.db.uow(write=False) as repo:
        assert len(repo.many("artifacts", {"work_id": f["world_ref"]["work_id"], "kind": "RELEASE_RECORD"})) == 1
        assert k.head(repo, f["world_ref"]["work_id"]) == f["world_ref"]


@pytest.mark.fault
def test_approval_revocation_between_preflight_and_freeze_is_synchronous(kernel_env):
    k = kernel_env["kernel"]
    f, _, _, proof, request = complete_candidate(k)

    def revoke(stage):
        if stage == "after_objects":
            ReviewService(k).revoke_editorial(f["principal"], f["world_ref"], proof["approval_ref"], "发现需要重新独立审核", "revoke")

    with pytest.raises(DomainError) as stale:
        ReleaseService(k).freeze(f["principal"], request, "revoked-freeze", fault=revoke)
    assert stale.value.code == "EDITORIAL_APPROVAL_REVOKED"
    assert_no_release(k, f)


@pytest.mark.fault
def test_review_cancellation_between_preflight_and_freeze_blocks_commit(kernel_env):
    k = kernel_env["kernel"]
    f, _, _, proof, request = complete_candidate(k)
    reader = proof["reader_tasks"]["fact"]

    def cancel(stage):
        if stage == "after_objects":
            k.cancel_task(reader["principal"], reader["task"]["task_id"], reader["task"]["generation"], "cancel-review")

    with pytest.raises(DomainError) as stale:
        ReleaseService(k).freeze(f["principal"], request, "cancelled-freeze", fault=cancel)
    # The editorial authority guard deliberately translates cancellation to
    # REVIEW_REVOKED before the lower-level reader task validator is invoked.
    assert stale.value.code == "REVIEW_REVOKED"
    assert_no_release(k, f)


def test_approval_cannot_be_reused_for_other_disclosure_order_or_missing_scene(kernel_env):
    k = kernel_env["kernel"]
    f, _, _, _, request = complete_candidate(k)
    s = ReleaseService(k)
    changed = {**request, "disclosure_revision_id": uid()}
    with pytest.raises(DomainError) as disclosure:
        s.freeze(f["principal"], changed, "wrong-disclosure")
    assert disclosure.value.code == "EDITORIAL_BINDING_MISMATCH"
    reversed_order = {**request, "ordered_acceptance_refs": list(reversed(request["ordered_acceptance_refs"]))}
    with pytest.raises(DomainError) as order:
        s.freeze(f["principal"], reversed_order, "wrong-order")
    assert order.value.code == "RELEASE_ORDER_MISMATCH"
    incomplete = {**request, "ordered_acceptance_refs": request["ordered_acceptance_refs"][:1]}
    with pytest.raises(DomainError) as missing:
        s.freeze(f["principal"], incomplete, "missing-scene")
    assert missing.value.code == "RELEASE_INCOMPLETE"
    assert_no_release(k, f)


def test_explicit_new_world_alignment_reuses_exact_text_but_requires_new_approval(kernel_env):
    k = kernel_env["kernel"]
    f, accepted, old_binding, proof, old_request = complete_candidate(k)
    advance = run_command(k, f["principal"], f["world_ref"], "ADVANCE_SCOPE", {"scope_id": f["ids"]["sword2"], "target": point(2)})
    s = ReleaseService(k)
    # An unrelated HEAD advance does not invalidate the explicitly fixed cut.
    first = s.freeze(f["principal"], old_request, "old-fixed-cut")
    assert first["manifest"]["world_ref"] == f["world_ref"]
    aligned = [accept_draft(k, f, draft, target_wr=advance["world_ref"]) for draft in accepted]
    binding = binding_for(f, aligned, world_ref=advance["world_ref"])
    binding["disclosure_revision_id"] = old_binding["disclosure_revision_id"]
    request = release_request(f, binding, proof["approval_ref"], prior_release_ref=first["release_ref"],
                              revision_reason="明确采用新的统一世界切面，原文逐字保留并重审。")
    with pytest.raises(DomainError) as old_approval:
        s.freeze(f["principal"], request, "old-approval-on-new-cut")
    assert old_approval.value.code == "EDITORIAL_BINDING_MISMATCH"
    new_proof = approve_binding(k, f, binding)
    request["editorial_approval_ref"] = new_proof["approval_ref"]
    second = s.freeze(f["principal"], request, "new-cut")
    assert second["manifest"]["world_ref"] == advance["world_ref"]
    assert second["manifest"]["ordered_artifacts"] == first["manifest"]["ordered_artifacts"]
    record = s.get_release(f["principal"], f["world_ref"]["work_id"], second["release_ref"])["record"]
    assert record["diff"]["world_changed"] and record["diff"]["changed_text_scene_ids"] == []
    assert len(record["alignment_refs"]) == 2


def test_newly_accepted_draft_invalidates_old_manifest_even_when_scene_is_unchanged(kernel_env):
    k = kernel_env["kernel"]
    f, accepted, binding, _, request = complete_candidate(k)
    replacement = accept_draft(k, f, write_scene(k, f, 0, "北窗的雨声细得像一根线。", prior_draft_ref=accepted[0]["draft_ref"]))
    s = ReleaseService(k)
    with pytest.raises(DomainError) as stale:
        s.freeze(f["principal"], request, "old-text-after-acceptance")
    assert stale.value.code == "NARRATIVE_SELECTION_CONFLICT"
    new_binding = {**binding, "ordered_draft_refs": [replacement["draft_ref"], accepted[1]["draft_ref"]],
        "ordered_acceptance_refs": [replacement["acceptance_ref"], accepted[1]["acceptance_ref"]],
        "reconciliation_refs": [replacement["reconciliation_ref"], accepted[1]["reconciliation_ref"]]}
    substituted = release_request(f, new_binding, request["editorial_approval_ref"])
    with pytest.raises(DomainError) as wrong_approval:
        s.freeze(f["principal"], substituted, "new-text-old-approval")
    assert wrong_approval.value.code == "EDITORIAL_BINDING_MISMATCH"
    assert_no_release(k, f)


def test_changed_predecessor_requires_new_successor_handoff_even_after_independent_reviews(kernel_env):
    k = kernel_env["kernel"]
    f = setup_batch(k, count=3, dependent=True)
    first = accept_draft(k, f, write_scene(k, f, 0, "窗北的雨声。"))
    second = accept_draft(k, f, write_scene(k, f, 1, "窗南的风声。"))
    third = accept_draft(k, f, write_scene(k, f, 2, "风雨的声音叠在一起。", [first["acceptance_ref"], second["acceptance_ref"]]))
    replacement = accept_draft(k, f, write_scene(k, f, 0, "窗北的雨声忽远忽近。", prior_draft_ref=first["draft_ref"]))
    binding = binding_for(f, [replacement, second, third])
    proof = approve_binding(k, f, binding)
    with pytest.raises(DomainError) as handoff:
        ReleaseService(k).freeze(f["principal"], release_request(f, binding, proof["approval_ref"]), "stale-predecessor-text")
    assert handoff.value.code == "STALE_SCENE_HANDOFF"
    assert_no_release(k, f)
