"""P2-04: exact evidence, epistemic honesty and zero effects from narration."""
from copy import deepcopy
from uuid import uuid4

import pytest

from novel_factory.domain.errors import DomainError
from novel_factory.domain.reconciliation import evaluate, knowledge_check, span_for, state_check, verify_span
from novel_factory.domain.reducer import replay
from novel_factory.fixtures import fixture_definition, point


def uid():
    return str(uuid4())


def assertion(text, **changes):
    value = {"assertion_id": uid(), "classification": "SURFACE_DETAIL", "span": span_for(text), "summary": text,
             "event_ref": None, "world_check": None, "knowledge_use": None, "proposed_command": None,
             "disclosure_id": None, "declared_delta_index": None}
    value.update(changes)
    return value


def context():
    fixture = fixture_definition("white_bridge")
    genesis = deepcopy(fixture["genesis"]); genesis["relations"] = []
    revision = {"snapshot": replay(genesis, []), "selection": {}, "frontiers": fixture["frontiers"]}
    scene = {"entry_checks": [], "exit_checks": [], "required_event_refs": [], "allowed_disclosures": []}
    return revision, scene, {"declared_important_deltas": []}, fixture["ids"]


def test_spans_are_exact_unicode_codepoints_without_normalization():
    text = "剑\U0001f5e1e\u0301已还。"
    span = span_for(text, 1, 4)
    verify_span(text, span)
    with pytest.raises(DomainError, match="span"):
        verify_span(text.replace("e\u0301", "é"), span)
    with pytest.raises(DomainError):
        span_for(text, True, 4)
    with pytest.raises(DomainError):
        span_for(text, 0, 1000)


def test_repeated_event_presentation_adds_no_world_effects():
    revision, scene, draft, _ = context(); text = "他又记起交剑的那一天。"
    event = {"occurrence_id": uid(), "event_version_id": uid()}
    revision["selection"][event["occurrence_id"]] = event["event_version_id"]
    scene["required_event_refs"] = [event]
    before = deepcopy(revision)
    claims = [assertion(text, classification="EXISTING_EVENT_PRESENTATION", event_ref=event) for _ in range(2)]
    result = evaluate(revision, scene, draft, claims, text)
    assert not result["blockers"]
    assert result["event_refs"] == [event]
    assert revision == before


def test_missing_required_occurrence_and_replaced_version_block_acceptance():
    revision, scene, draft, _ = context(); text = "风从桥下吹过。"
    event = {"occurrence_id": uid(), "event_version_id": uid()}
    scene["required_event_refs"] = [event]
    result = evaluate(revision, scene, draft, [assertion(text)], text)
    assert any(b["code"] == "IMPORTANT_EFFECT_COVERAGE_CONFLICT" for b in result["blockers"])
    revision["selection"][event["occurrence_id"]] = uid()
    result = evaluate(revision, scene, draft, [assertion(text, classification="EXISTING_EVENT_PRESENTATION", event_ref=event)], text)
    assert any(b["code"] == "EVENT_SELECTION_CONFLICT" for b in result["blockers"])


def test_world_first_no_loss_cannot_hide_two_bag_burn_in_text():
    revision, scene, draft, ids = context(); text = "火烧掉了两袋粮食。"
    check = {"check_id": uid(), "plane": "WORLD", "subject_id": ids["warehouse"], "predicate": "resource:" + ids["grain"] + ":bag",
             "at": point(1), "expected_status": "VALUE", "expected_value": {"kind": "RESOURCE_BALANCE", "account_id": ids["warehouse"],
             "resource_id": ids["grain"], "quantity": "98", "unit": "bag"}, "actor_id": None, "known_at": None}
    result = evaluate(revision, scene, draft, [assertion(text, classification="HISTORICAL_CONFLICT", world_check=check)], text)
    assert any(b["code"] == "STATE_CONFLICT" for b in result["blockers"])
    assert revision["snapshot"]["resource_history"][0]["quantity"] == "100"


def test_text_first_change_stays_pending_until_explicit_validated_resolution():
    revision, scene, draft, ids = context(); text = "他交出了剑。"
    claim = assertion(text, classification="NEW_WORLD_EFFECT", proposed_command={"template_id": "TRANSFER_ITEM", "args": {
        "item_id": ids["sword"], "from_id": ids["smith"], "to_id": ids["protagonist"], "transfer_kind": "GIFT", "at": point(2)}})
    before = deepcopy(revision)
    assert evaluate(revision, scene, draft, [claim], text)["blockers"]
    # This set is an internal application output after the event closure match,
    # never accepted from a model as proof of world acceptance.
    assert not evaluate(revision, scene, draft, [claim], text, {claim["assertion_id"]})["blockers"]
    assert revision == before


def test_guess_is_not_knowing_and_new_utterance_still_needs_event():
    revision, scene, draft, ids = context(); text = "他猜剑也许已经还了。"
    use = {"actor_id": ids["protagonist"], "subject_id": ids["sword"], "predicate": "legal_owner", "at": point(1),
           "mode": "GUESSES", "value": {"kind": "ENTITY", "entity_id": ids["protagonist"]}, "evidence_refs": []}
    assert knowledge_check(revision, use) == "PASS"
    claim = assertion(text, classification="SPEECH_OR_BELIEF", knowledge_use=use)
    assert not evaluate(revision, scene, draft, [claim], text)["blockers"]
    known = deepcopy(use); known["mode"] = "KNOWS"
    assert knowledge_check(revision, known) == "UNRESOLVED"
    spoken = deepcopy(claim); spoken["knowledge_use"]["mode"] = "SAYS"
    assert evaluate(revision, scene, draft, [spoken], text)["blockers"]


def test_unadvanced_world_is_unresolved_instead_of_negative_fact():
    revision, _, _, ids = context()
    check = {"plane": "WORLD", "subject_id": ids["sword"], "predicate": "active_use", "at": point(40),
             "expected_status": "NONE", "expected_value": None, "actor_id": None, "known_at": None}
    assert state_check(revision, check) == "UNRESOLVED"


def test_declared_important_delta_requires_its_own_classified_span():
    revision, scene, draft, _ = context(); text = "他获得了神剑。"
    draft["declared_important_deltas"] = ["新神器"]
    assert evaluate(revision, scene, draft, [], text)["blockers"]
    surface = assertion(text, declared_delta_index=0)
    assert evaluate(revision, scene, draft, [surface], text)["blockers"]


def test_belief_citations_are_bound_to_that_actor_and_acquisition_time():
    revision, _, _, ids = context()
    source = {"kind": "ARTIFACT", "artifact_ref": {"artifact_id": uid(), "artifact_revision_id": uid(), "sha256": "a" * 64}}
    value = {"kind": "ENTITY", "entity_id": ids["friend"]}
    revision["snapshot"]["cognitions"].append({"actor_id": ids["protagonist"], "record_kind": "BELIEF",
        "subject_id": ids["sword"], "predicate": "possessed_by", "acquired_at": point(1),
        "object_value": value, "evidence_refs": [source]})
    use = {"actor_id": ids["protagonist"], "subject_id": ids["sword"], "predicate": "possessed_by", "at": point(1),
           "mode": "BELIEVES", "value": value, "evidence_refs": [source]}
    assert knowledge_check(revision, use) == "PASS"
    forged = deepcopy(use); forged["evidence_refs"][0]["artifact_ref"]["sha256"] = "b" * 64
    assert knowledge_check(revision, forged) == "FAIL"
    other_actor = deepcopy(use); other_actor["actor_id"] = ids["friend"]
    assert knowledge_check(revision, other_actor) == "UNRESOLVED"
    premature = deepcopy(use); premature["at"] = point(0)
    assert knowledge_check(revision, premature) == "UNRESOLVED"
    guessed = deepcopy(forged); guessed["mode"] = "GUESSES"
    assert knowledge_check(revision, guessed) == "FAIL"
