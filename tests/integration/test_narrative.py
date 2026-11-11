"""P2-04 real PostgreSQL acceptance, permissions and text/world reconciliation.

Text fixtures are declared engineering inputs, not external model experiments.
"""
from copy import deepcopy
from uuid import uuid4

import pytest

from novel_factory.application.narrative import NarrativeService
from novel_factory.application.planning import PlanningService
from novel_factory.contracts.registry import validate_contract
from novel_factory.domain.errors import DomainError
from novel_factory.domain.reconciliation import span_for
from novel_factory.fixtures import load_fixture, point, run_command
from test_planning import make_plan
from test_release import runtime_grant, task_for

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def uid():
    return str(uuid4())


def assertion(text, **changes):
    candidate = {"assertion_id": uid(), "classification": "SURFACE_DETAIL", "span": span_for(text), "summary": text,
        "event_ref": None, "world_check": None, "knowledge_use": None, "proposed_command": None,
        "disclosure_id": None, "declared_delta_index": None}
    candidate.update(changes)
    return candidate


def arrange(k, f=None):
    f = f or load_fixture(k)
    planning = PlanningService(k)
    plan, plan_ref = make_plan(planning, f)
    planning.adopt_plan(f["principal"], f["world_ref"], plan_ref, None, "adopt:" + plan["plan_revision_id"])
    return {**f, "plan": plan, "plan_ref": plan_ref, "narrative_id": uid()}


def scene_for(k, f, *, event_refs=(), checks=(), ordinal=0, disclosures=(), interval=None):
    scene = {"schema_version": "2.2", "scene_contract": {"schema_version": "2.0", "scene_id": uid(),
        "input_world_ref": f["world_ref"], "story_interval": interval or {"start": point(1), "end": point(2, 1), "bounds": "[)"},
        "event_refs": list(event_refs), "entry_commitments": [], "exit_commitments": [],
        "pov_actor_id": f["ids"]["protagonist"], "narrator_mode": "limited", "narrative_revision_id": f["narrative_id"],
        "arrangement_version": "narrative-tests-1", "reader_cut_ordinal": ordinal, "allowed_invention": ["surface detail"],
        "style_refs": [], "dependency_fingerprint": f["plan_ref"]["sha256"]},
        "plan_ref": f["plan_ref"], "plan_node_ids": [f["plan"]["nodes"][0]["node_id"]], "batch_ref": None,
        "prior_scene_ref": None, "required_event_refs": list(event_refs), "required_predecessor_scene_ids": [],
        "entry_checks": [], "exit_checks": list(checks), "allowed_disclosures": list(disclosures), "literary_purpose": "counterexample evidence"}
    ref = NarrativeService(k).save_scene(f["principal"], scene, "scene:" + scene["scene_contract"]["scene_id"])
    writer = runtime_grant(k, f, "writer")
    task = task_for(k, f, writer, [ref, f["plan_ref"]], story_time=scene["scene_contract"]["story_interval"]["end"])
    PlanningService(k).bind_task_for_controller(f["principal"], task["task_id"], task["generation"], f["plan_ref"], scene["plan_node_ids"], "bind:" + task["task_id"])
    return {"scene": scene, "scene_ref": ref, "writer": writer, "task": task}


def draft_for(k, f, text, *, deltas=(), **scene_options):
    item = scene_for(k, f, **scene_options)
    ref = NarrativeService(k).store_draft(item["writer"], item["task"]["task_id"], item["task"]["generation"],
        item["scene_ref"], text, "draft:" + item["task"]["task_id"], declared_important_deltas=list(deltas))
    return {**item, "draft_ref": ref, "text": text}


def extract(k, f, item, assertions):
    s = NarrativeService(k)
    reader = runtime_grant(k, f, "reviewer")
    task = task_for(k, f, reader, [item["draft_ref"]], "extract", story_time=item["scene"]["scene_contract"]["story_interval"]["end"])
    s.bind_reader_task(f["principal"], task["task_id"], task["generation"], [item["draft_ref"]], "reader:" + task["task_id"])
    read = s.read_text(reader, task["task_id"], task["generation"], item["draft_ref"])
    ref = s.submit_extraction(reader, task["task_id"], task["generation"], item["draft_ref"], read["delivery_ref"], assertions, "extract:" + task["task_id"])
    return {"reader": reader, "reader_task": task, "extraction_ref": ref, "delivery": read}


def test_world_event_can_be_narrated_twice_without_effect_reapplication(kernel_env):
    k = kernel_env["kernel"]; f = load_fixture(k, "white_bridge"); ids = f["ids"]
    transfer = run_command(k, f["principal"], f["world_ref"], "RESOURCE_TRANSFER", {
        "from_account": ids["warehouse"], "to_account": ids["relief"], "resource_id": ids["grain"],
        "quantity": "10", "unit": "bag", "at": point(2)})
    f = arrange(k, {**f, "world_ref": transfer["world_ref"]}); s = NarrativeService(k)
    event = transfer["receipt"]["accepted_event_refs"][0]
    before = None
    with k.db.uow(write=False) as repo:
        before = k.revision(repo, f["world_ref"])["snapshot"]
    for index, text in enumerate(["仓吏亲眼看着十袋粮食交出。", "他又想起那十袋粮食被交出的样子。"]):
        item = draft_for(k, f, text, event_refs=[event], ordinal=index)
        extracted = extract(k, f, item, [assertion(text, classification="EXISTING_EVENT_PRESENTATION", event_ref=event)])
        result = s.reconcile(f["principal"], item["draft_ref"], extracted["extraction_ref"], f["world_ref"], "reconcile:" + str(index))
        assert result["status"] == "CLEAR"
        accepted = s.accept_narrative(f["principal"], item["draft_ref"], result["reconciliation_ref"], f["world_ref"], "accept:" + str(index))
        assert accepted == s.accept_narrative(f["principal"], item["draft_ref"], result["reconciliation_ref"], f["world_ref"], "accept:" + str(index))
        with k.db.uow(write=False) as repo:
            assert k.artifact(repo, accepted, f["world_ref"]["work_id"])["payload"]["event_refs"] == [event]
            assert k.head(repo, f["world_ref"]["work_id"]) == f["world_ref"]
            assert k.revision(repo, f["world_ref"])["snapshot"] == before
            assert len(repo.many("events", {"work_id": f["world_ref"]["work_id"]})) == 1


def test_text_first_delta_must_use_formal_kernel_acceptance_then_exact_alignment(kernel_env):
    k = kernel_env["kernel"]; f = arrange(k); s = NarrativeService(k); ids = f["ids"]
    text = "铁匠把剑赠给了旅人。"
    item = draft_for(k, f, text, deltas=["赠剑"])
    command = {"template_id": "TRANSFER_ITEM", "args": {"item_id": ids["sword"], "from_id": ids["smith"],
        "to_id": ids["protagonist"], "transfer_kind": "GIFT", "at": point(2)}}
    claim = assertion(text, classification="NEW_WORLD_EFFECT", proposed_command=command, declared_delta_index=0)
    extracted = extract(k, f, item, [claim])
    blocked = s.reconcile(f["principal"], item["draft_ref"], extracted["extraction_ref"], f["world_ref"], "pending")
    assert blocked["status"] == "BLOCKED" and len(blocked["pending_delta_refs"]) == 1
    with pytest.raises(DomainError) as pending:
        s.accept_narrative(f["principal"], item["draft_ref"], blocked["reconciliation_ref"], f["world_ref"], "accept-pending")
    assert pending.value.code == "NARRATIVE_BLOCKED"
    with k.db.uow(write=False) as repo:
        assert k.head(repo, f["world_ref"]["work_id"]) == f["world_ref"]
        assert repo.many("events", {"work_id": f["world_ref"]["work_id"]}) == []
    accepted_world = run_command(k, f["principal"], f["world_ref"], command["template_id"], command["args"])
    target = accepted_world["world_ref"]
    with pytest.raises(DomainError) as stale:
        s.accept_narrative(f["principal"], item["draft_ref"], blocked["reconciliation_ref"], target, "silent-rebase")
    assert stale.value.code == "NARRATIVE_ALIGNMENT_REQUIRED"
    resolution = {"assertion_id": claim["assertion_id"], "event_ref": accepted_world["receipt"]["accepted_event_refs"][0]}
    aligned = s.build_alignment(f["principal"], item["draft_ref"], extracted["extraction_ref"], target, "aligned", resolution_event_refs=[resolution])
    assert aligned["status"] == "CLEAR"
    with pytest.raises(DomainError) as no_alignment:
        s.accept_narrative(f["principal"], item["draft_ref"], aligned["reconciliation_ref"], target, "no-explicit-alignment")
    assert no_alignment.value.code == "NARRATIVE_ALIGNMENT_REQUIRED"
    accepted = s.accept_narrative(f["principal"], item["draft_ref"], aligned["reconciliation_ref"], target, "accepted", alignment_ref=aligned["alignment_ref"])
    with k.db.uow(write=False) as repo:
        payload = k.artifact(repo, accepted, target["work_id"])["payload"]
        assert payload["world_effects_applied"] == 0
        assert k.head(repo, target["work_id"]) == target
        alignment = k.artifact(repo, aligned["alignment_ref"], target["work_id"])["payload"]
        validate_contract("AlignmentReport", alignment, "narrative")
        request = {key: alignment[key] for key in ("task_id", "generation", "context_bundle_id", "scene_contract_ref", "draft_ref", "dependency_fingerprint")}
    request.update(world_ref=target, alignment_ref=aligned["alignment_ref"], idempotency_key="legacy-shape")
    assert s.submit_draft(f["principal"], request) == accepted


def test_reader_inputs_do_not_grant_text_or_hidden_material_access(kernel_env):
    k = kernel_env["kernel"]; f = arrange(k); s = NarrativeService(k)
    item = draft_for(k, f, "这段尚未向该审核任务授权的正文。")
    reader = runtime_grant(k, f, "reviewer")
    task = task_for(k, f, reader, [item["draft_ref"]], "review", story_time=point(2, 1))
    with pytest.raises(DomainError) as denied:
        s.read_text(reader, task["task_id"], task["generation"], item["draft_ref"])
    assert denied.value.code == "VIEW_DENIED"
    with pytest.raises(DomainError):
        s.read_agent_material(reader, task["task_id"], task["generation"], task["context_bundle_id"], item["draft_ref"])
    with pytest.raises(DomainError):
        s.bind_reader_task(reader, task["task_id"], task["generation"], [item["draft_ref"]], "self-authorize")
    s.bind_reader_task(f["principal"], task["task_id"], task["generation"], [item["draft_ref"]], "authorized")
    assert s.read_text(reader, task["task_id"], task["generation"], item["draft_ref"])["text"] == item["text"]
    actor = k.issue_grant(f["principal"], f["world_ref"]["work_id"], role_id="character_actor", actor_id=f["ids"]["protagonist"],
        capabilities=["READ", "ARTIFACT_WRITE"], planes=["BELIEF"], max_story_time=point(100))["principal"]
    for method, ref in ((s.load_draft, item["draft_ref"]), (s.load_scene, item["scene_ref"])):
        with pytest.raises(DomainError) as actor_denied:
            method(actor, f["world_ref"]["work_id"], ref)
        assert actor_denied.value.code == "VIEW_DENIED"
    k.cancel_task(reader, task["task_id"], task["generation"], "cancel-reader")
    with pytest.raises(DomainError):
        s.read_text(reader, task["task_id"], task["generation"], item["draft_ref"])


def test_forged_clear_report_and_wrong_span_never_receive_acceptance(kernel_env):
    k = kernel_env["kernel"]; f = arrange(k); s = NarrativeService(k)
    item = draft_for(k, f, "北窗的雨声很轻。")
    extracted = extract(k, f, item, [assertion(item["text"])])
    result = s.reconcile(f["principal"], item["draft_ref"], extracted["extraction_ref"], f["world_ref"], "valid")
    with k.db.uow(write=False) as repo:
        report = k.artifact(repo, result["reconciliation_ref"], f["world_ref"]["work_id"])["payload"]
    forged = deepcopy(report); forged["report_id"] = uid()
    forged_ref = k._blob(forged)
    # Fault injection: even a valid saved report is not an execution record.
    with k.objects.lock(), k.db.uow() as repo:
        k._register_artifact(repo, forged_ref, f["world_ref"]["work_id"], forged, "RECONCILIATION_REPORT", f["principal"].principal_id)
    with pytest.raises(DomainError) as not_verified:
        s.accept_narrative(f["principal"], item["draft_ref"], forged_ref, f["world_ref"], "forged")
    assert not_verified.value.code == "UNVERIFIED_REPORT"
    claim = assertion(item["text"]); claim["span"]["quote_sha256"] = "0" * 64
    with pytest.raises(DomainError) as wrong_span:
        s.submit_extraction(extracted["reader"], extracted["reader_task"]["task_id"], extracted["reader_task"]["generation"],
            item["draft_ref"], extracted["delivery"]["delivery_ref"], [claim], "bad-span")
    assert wrong_span.value.code == "INVALID_EVIDENCE"


def test_unrelated_head_progress_keeps_cut_but_revoked_writer_cannot_be_adopted(kernel_env):
    k = kernel_env["kernel"]; f = arrange(k); s = NarrativeService(k)
    item = draft_for(k, f, "北窗的雨声很轻。")
    extracted = extract(k, f, item, [assertion(item["text"])])
    result = s.reconcile(f["principal"], item["draft_ref"], extracted["extraction_ref"], f["world_ref"], "fixed")
    updated = run_command(k, f["principal"], f["world_ref"], "ADVANCE_SCOPE", {"scope_id": f["ids"]["sword2"], "target": point(3)})
    accepted = s.accept_narrative(f["principal"], item["draft_ref"], result["reconciliation_ref"], f["world_ref"], "unrelated")
    with k.db.uow(write=False) as repo:
        assert k.head(repo, f["world_ref"]["work_id"]) == updated["world_ref"]
        assert k.artifact(repo, accepted, f["world_ref"]["work_id"])["payload"]["world_ref"] == f["world_ref"]
    k.revoke_grant(f["principal"], item["writer"].grant_id)
    with k.db.uow(write=False) as repo, pytest.raises(DomainError) as revoked:
        s.validate_for_acceptance(repo, f["principal"], item["draft_ref"], result["reconciliation_ref"], f["world_ref"])
    assert revoked.value.code == "VIEW_DENIED"


def test_valid_belief_cannot_cite_forged_or_other_actor_evidence(kernel_env):
    from novel_factory.domain.propositions import encode_proposition
    k = kernel_env["kernel"]; f = load_fixture(k); ids = f["ids"]
    gift = run_command(k, f["principal"], f["world_ref"], "TRANSFER_ITEM", {"item_id": ids["sword"], "from_id": ids["smith"],
        "to_id": ids["protagonist"], "transfer_kind": "GIFT", "at": point(1, 2)})
    evidence = {"kind": "EVENT", "world_ref": gift["world_ref"], "event_ref": gift["receipt"]["accepted_event_refs"][0]}
    cognition = run_command(k, f["principal"], gift["world_ref"], "RECORD_COGNITION", {"actor_id": ids["protagonist"],
        "statement": encode_proposition(ids["sword"], "possessed_by", {"kind": "ENTITY", "entity_id": ids["protagonist"]}),
        "record_kind": "BELIEF", "epistemic_mode": "OBSERVED", "evidence_refs": [evidence], "at": point(1, 3)})
    advanced = run_command(k, f["principal"], cognition["world_ref"], "ADVANCE_SCOPE", {"scope_id": ids["sword"], "target": point(2)})
    f = arrange(k, {**f, "world_ref": advanced["world_ref"]}); s = NarrativeService(k)
    item = draft_for(k, f, "旅人知道剑在自己手里。")
    use = {"actor_id": ids["protagonist"], "subject_id": ids["sword"], "predicate": "possessed_by", "at": point(1, 3),
           "mode": "KNOWS", "value": {"kind": "ENTITY", "entity_id": ids["protagonist"]}, "evidence_refs": [evidence]}
    real = extract(k, f, item, [assertion(item["text"], classification="SPEECH_OR_BELIEF", knowledge_use=use)])
    assert s.reconcile(f["principal"], item["draft_ref"], real["extraction_ref"], f["world_ref"], "real-knowledge")["status"] == "CLEAR"
    unrelated = k.put_artifact(f["principal"], f["world_ref"]["work_id"], b"A genuine object that this actor did not acquire", "EVIDENCE")
    forged = deepcopy(use); forged["evidence_refs"] = [{"kind": "ARTIFACT", "artifact_ref": unrelated}]
    bad = extract(k, f, item, [assertion(item["text"], classification="SPEECH_OR_BELIEF", knowledge_use=forged)])
    assert s.reconcile(f["principal"], item["draft_ref"], bad["extraction_ref"], f["world_ref"], "forged-known")["status"] == "BLOCKED"
    other = deepcopy(use); other["actor_id"] = ids["friend"]
    alien = extract(k, f, item, [assertion(item["text"], classification="SPEECH_OR_BELIEF", knowledge_use=other)])
    assert s.reconcile(f["principal"], item["draft_ref"], alien["extraction_ref"], f["world_ref"], "other-actor")["status"] == "BLOCKED"
    says = deepcopy(use); says["mode"] = "SAYS"
    speech = extract(k, f, item, [assertion(item["text"], classification="SPEECH_OR_BELIEF", knowledge_use=says, event_ref=gift["receipt"]["accepted_event_refs"][0])])
    assert s.reconcile(f["principal"], item["draft_ref"], speech["extraction_ref"], f["world_ref"], "gift-not-speech")["status"] == "BLOCKED"


def test_cross_branch_text_requires_new_scene_task_and_source_qualification(kernel_env):
    k = kernel_env["kernel"]; f = arrange(k); s = NarrativeService(k); p = f["principal"]
    old = draft_for(k, f, "桥下的雨声还没有停。")
    source = extract(k, f, old, [assertion(old["text"])])
    before = s.reconcile(p, old["draft_ref"], source["extraction_ref"], f["world_ref"], "original")
    s.accept_narrative(p, old["draft_ref"], before["reconciliation_ref"], f["world_ref"], "accept-original")
    candidate_wr = k.fork(p, f["world_ref"], "CANDIDATE", "candidate")
    with pytest.raises(DomainError) as rebind:
        s.build_alignment(p, old["draft_ref"], source["extraction_ref"], candidate_wr, "rename-branch")
    assert rebind.value.code == "BRANCH_CHANGED"
    target = arrange(k, {**f, "world_ref": candidate_wr})
    revised = deepcopy(old["scene"])
    revised.update(plan_ref=target["plan_ref"], plan_node_ids=[target["plan"]["nodes"][0]["node_id"]], prior_scene_ref=old["scene_ref"])
    revised["scene_contract"].update(input_world_ref=candidate_wr, narrative_revision_id=uid(), arrangement_version="candidate-requalified")
    scene_ref = s.save_scene(p, revised, "candidate-scene")
    writer = k.issue_grant(p, candidate_wr["work_id"], role_id="writer", branch_ids=[candidate_wr["branch_id"]],
        capabilities=["READ", "ARTIFACT_WRITE", "TASK_CREATE", "TASK_CLAIM", "COMPILE"], planes=["WORLD"], max_story_time=point(100))["principal"]
    task = task_for(k, target, writer, [scene_ref, target["plan_ref"], old["draft_ref"]], story_time=point(2, 1))
    PlanningService(k).bind_task_for_controller(p, task["task_id"], task["generation"], target["plan_ref"], revised["plan_node_ids"], "target-plan")
    with pytest.raises(DomainError) as old_source_read:
        s.bind_reader_task(p, task["task_id"], task["generation"], [old["draft_ref"]], "cross-branch-read")
    assert old_source_read.value.code == "VIEW_DENIED"
    # The authorized controller supplies the chosen text; old actor/task read
    # permissions are not widened. New body, task, world and evidence are made.
    new_ref = s.store_draft(writer, task["task_id"], task["generation"], scene_ref, old["text"], "candidate-draft", prior_draft_ref=old["draft_ref"])
    reader = k.issue_grant(p, candidate_wr["work_id"], role_id="reviewer", branch_ids=[candidate_wr["branch_id"]],
        capabilities=["READ", "ARTIFACT_WRITE", "TASK_CREATE", "TASK_CLAIM", "COMPILE"], planes=["WORLD"], max_story_time=point(100))["principal"]
    rt = task_for(k, target, reader, [new_ref], "new-extraction", story_time=point(2, 1))
    s.bind_reader_task(p, rt["task_id"], rt["generation"], [new_ref], "target-reading")
    delivery = s.read_text(reader, rt["task_id"], rt["generation"], new_ref)
    extraction = s.submit_extraction(reader, rt["task_id"], rt["generation"], new_ref, delivery["delivery_ref"], [assertion(old["text"])], "target-extraction")
    report = s.reconcile(p, new_ref, extraction, candidate_wr, "target-reconcile")
    accepted = s.accept_narrative(p, new_ref, report["reconciliation_ref"], candidate_wr, "target-accept")
    with k.db.uow(write=False) as repo:
        assert k.artifact(repo, accepted, candidate_wr["work_id"])["payload"]["world_ref"] == candidate_wr
        assert k.artifact(repo, new_ref, candidate_wr["work_id"])["payload"]["prior_draft_ref"] == old["draft_ref"]
        assert k.head(repo, candidate_wr["work_id"]) == f["world_ref"]  # No promotion or publication.


def test_unreliable_reader_disclosure_is_accepted_text_without_world_truth(kernel_env):
    k = kernel_env["kernel"]; f = arrange(k); s = NarrativeService(k)
    disclosure = {"disclosure_id": uid(), "statement": "叙述者声称桥下有龙，未认定为事实", "truth_status": "UNRELIABLE_CLAIM",
                  "source_event_refs": [], "world_check": None}
    item = draft_for(k, f, "桥下藏着龙。至少故事是这样讲的。", disclosures=[disclosure])
    candidate = assertion(item["text"], classification="READER_DISCLOSURE_CHANGE", disclosure_id=disclosure["disclosure_id"])
    extracted = extract(k, f, item, [candidate])
    report = s.reconcile(f["principal"], item["draft_ref"], extracted["extraction_ref"], f["world_ref"], "unreliable")
    accepted = s.accept_narrative(f["principal"], item["draft_ref"], report["reconciliation_ref"], f["world_ref"], "accept-unreliable")
    with k.db.uow(write=False) as repo:
        payload = k.artifact(repo, accepted, f["world_ref"]["work_id"])["payload"]
        assert payload["accepted_disclosures"] == [disclosure]
        assert payload["world_effects_applied"] == 0
        assert k.head(repo, f["world_ref"]["work_id"]) == f["world_ref"]
        assert not repo.many("events", {"work_id": f["world_ref"]["work_id"]})
        assert repo.many("invalidations", {"work_id": f["world_ref"]["work_id"], "scope_key": "ARTIFACT_SELECTION:NARRATIVE_SCENE:" + item["scene"]["scene_contract"]["scene_id"]})
    unproven = deepcopy(disclosure); unproven["truth_status"] = "WORLD_FACT"
    with pytest.raises(DomainError) as false_fact:
        scene_for(k, f, disclosures=[unproven])
    assert false_fact.value.code == "INVALID_ARGUMENT"


def test_actor_material_errors_do_not_reveal_artifact_existence(kernel_env):
    from novel_factory.fixtures import create_bound_task
    k = kernel_env["kernel"]; f = arrange(k); s = NarrativeService(k)
    item = draft_for(k, f, "尚未向角色披露的作者正文。")
    actor = k.issue_grant(f["principal"], f["world_ref"]["work_id"], role_id="character_actor", actor_id=f["ids"]["protagonist"],
        capabilities=["READ", "ARTIFACT_WRITE", "COMPILE", "TASK_CREATE", "TASK_CLAIM"], planes=["BELIEF"], max_story_time=point(100))["principal"]
    task = create_bound_task(k, actor, f["world_ref"], point(2, 1), plane="BELIEF", actor_id=f["ids"]["protagonist"])
    current = k.get_task(actor, task["task_id"])
    nonexistent = {"artifact_id": uid(), "artifact_revision_id": uid(), "sha256": "0" * 64}
    errors = []
    for ref in [item["draft_ref"], nonexistent]:
        with pytest.raises(DomainError) as denied:
            s.read_agent_material(actor, current["task_id"], current["generation"], current["context_bundle_id"], ref)
        errors.append((denied.value.code, denied.value.safe_message))
    assert errors[0] == errors[1] == ("VIEW_DENIED", "Literary material is unavailable for this assignment")
