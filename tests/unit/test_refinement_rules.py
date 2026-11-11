"""R1-G/V2-05: transition trace equivalence is stricter than matching endpoints."""
from copy import deepcopy
from uuid import uuid4

import pytest

from novel_factory.domain.errors import DomainError
from novel_factory.domain.refinement import prove_refinement
from novel_factory.domain.world import replay, resource_at
from novel_factory.fixtures import fixture_definition, point


def reference():
    return {"artifact_id": str(uuid4()), "artifact_revision_id": str(uuid4()), "sha256": "1" * 64}


def world():
    fixture = fixture_definition("white_bridge")
    genesis = deepcopy(fixture["genesis"])
    for relation in genesis["relations"]:
        source = reference()
        relation.update(relation_id=str(uuid4()), relation_version_id=str(uuid4()), plane="WORLD",
                        actor_id=None, evidence_refs=[], origin={"kind": "RECORD",
                        "record_id": source["artifact_id"], "record_version_id": source["artifact_revision_id"],
                        "payload_ref": source})
    return genesis, fixture["ids"]


def event(effects, participants, at=None):
    return {"schema_version": "2.1" if any(e["kind"] == "RELATION_TRANSITION" for e in effects) else "2.0",
            "occurrence_id": str(uuid4()), "event_version_id": str(uuid4()),
            "effect_group_id": str(uuid4()), "representation_id": str(uuid4()),
            "occurrence_time": {"kind": "EXACT", "at": at or point(30)}, "participants": participants,
            "causal_parents": [], "ruleset_ref": reference(), "adjudication_ref": reference(),
            "engine_version": "nf-reducer-1", "effects": effects}


def transfer(ids, quantity="30", *, target="relief", prefix="transfer"):
    return [{"kind": "RESOURCE_DELTA", "effect_key": prefix + suffix,
             "account_id": ids[account], "resource_id": ids["grain"], "unit": "bag",
             "quantity": sign + quantity}
            for account, suffix, sign in [("warehouse", ":out", "-"), (target, ":in", "")]]


def step(macro, effects=None, at=None):
    return {"step_id": str(uuid4()), "at": at or macro["occurrence_time"]["at"],
            "participant_ids": macro["participants"], "depends_on": [],
            "effects": deepcopy(macro["effects"] if effects is None else effects)}


def prove(genesis, macro, steps, extra=()):
    return prove_refinement(genesis, [macro, *extra],
                            {k: macro[k] for k in ("occurrence_id", "event_version_id")}, steps)


def test_three_way_atomic_partition_preserves_identity_all_cuts_and_exact_resources():
    genesis, ids = world()
    effects = transfer(ids, "15", target="relief", prefix="a") + transfer(ids, "10", target="recipient", prefix="b")
    effects += transfer(ids, "5", target="reserve", prefix="c")
    macro = event(effects, [ids[k] for k in ("warehouse", "relief", "recipient", "reserve", "grain")])
    steps = [step(macro, transfer(ids, "5", prefix="a1")), step(macro, transfer(ids, "10", prefix="a2")),
             step(macro, transfer(ids, "10", target="recipient", prefix="b1")),
             step(macro, transfer(ids, "5", target="reserve", prefix="c1"))]
    original = deepcopy(macro)
    replacement, report = prove(genesis, macro, steps)
    assert macro == original
    assert replacement["occurrence_id"] == macro["occurrence_id"]
    assert replacement["effect_group_id"] == macro["effect_group_id"]
    assert replacement["event_version_id"] != macro["event_version_id"]
    assert replacement["representation_id"] != macro["representation_id"]
    assert replacement["causal_parents"] == macro["causal_parents"]
    assert report["event_count_before"] == report["event_count_after"] == 1
    assert {tuple(item["at"].values()) for item in report["boundaries"]} >= {(30, 0), (29, 2147483647)}
    assert all(cut["before_sha256"] == cut["after_sha256"] for cut in report["boundaries"])
    snapshot = replay(genesis, [replacement])
    assert resource_at(snapshot, ids["warehouse"], ids["grain"], "bag", point(31))["quantity"] == "70"
    again, _ = prove(genesis, macro, list(reversed(steps)))
    assert again == replacement


def test_same_final_delivery_with_early_partial_delivery_is_not_equivalent():
    genesis, ids = world()
    macro = event(transfer(ids), [ids[k] for k in ("warehouse", "relief", "grain")])
    steps = [step(macro, transfer(ids, "15", prefix="early"), point(25)),
             step(macro, transfer(ids, "15", prefix="late"))]
    with pytest.raises(DomainError) as failure:
        prove(genesis, macro, steps)
    assert failure.value.code == "REFINEMENT_NOT_EQUIVALENT"


@pytest.mark.parametrize("kind,mode", [("OBSERVATION", "HEARD"), ("OBSERVATION", "OBSERVED"),
                                        ("BELIEF", "INFERRED"), ("PRIVATE_DECISION", "DECIDED")])
@pytest.mark.parametrize("change", ["time", "actor", "evidence", "statement"])
def test_intermediate_message_knowledge_private_decision_and_source_must_match(kind, mode, change):
    genesis, ids = world()
    effect = {"kind": "COGNITIVE_RECORD_ADD", "effect_key": "notice", "actor_id": ids["friend"],
              "record_kind": kind, "epistemic_mode": mode, "statement": "城北有粮，仅为角色收到的说法。",
              "evidence_refs": [{"kind": "ARTIFACT", "artifact_ref": reference()}]}
    macro = event([effect], [ids["friend"], ids["rival"]])
    candidate = step(macro)
    if change == "time":
        candidate["at"] = point(29)
    elif change == "actor":
        candidate["effects"][0]["actor_id"] = ids["rival"]
    elif change == "evidence":
        candidate["effects"][0]["evidence_refs"][0]["artifact_ref"] = reference()
    else:
        candidate["effects"][0]["statement"] = "城北无粮"
    with pytest.raises(DomainError) as failure:
        prove(genesis, macro, [candidate])
    assert failure.value.code == "REFINEMENT_NOT_EQUIVALENT"


def test_temporary_owner_or_holder_excursion_is_not_an_endpoint_equivalence():
    genesis, ids = world()
    effect = {"kind": "RELATION_SET", "effect_key": "holding", "subject_id": ids["sword"],
              "predicate": "possessed_by", "object_id": ids["friend"], "value_status": "VALUE"}
    macro = event([effect], [ids["sword"], ids["friend"], ids["rival"]])
    for predicate in ("legal_owner", "possessed_by"):
        excursion = {**effect, "effect_key": "excursion", "predicate": predicate, "object_id": ids["rival"]}
        with pytest.raises(DomainError) as failure:
            prove(genesis, macro, [step(macro, [excursion], point(29)), step(macro)])
        assert failure.value.code == "REFINEMENT_NOT_EQUIVALENT"


def test_existing_owner_and_holder_intervals_keep_their_boundary_and_identity():
    genesis, ids = world()
    effects = [{"kind": "RELATION_SET", "effect_key": "set:" + predicate,
                "subject_id": ids["sword"], "predicate": predicate,
                "object_id": ids["friend"], "value_status": "VALUE"}
               for predicate in ("legal_owner", "possessed_by", "located_in")]
    macro = event(effects, [ids["sword"], ids["friend"]])
    replacement, report = prove(genesis, macro, [step(macro, [effect]) for effect in reversed(effects)])
    assert report["status"] == "PASS"
    before = replay(genesis, [macro]); after = replay(genesis, [replacement])
    assert {row["relation_id"] for row in before["relations"]} == {row["relation_id"] for row in after["relations"]}


def test_unknown_interpreter_cannot_obtain_equivalence_claim():
    genesis, ids = world()
    macro = event(transfer(ids), [ids[k] for k in ("warehouse", "relief", "grain")])
    macro["engine_version"] = "uninstalled-reducer"
    with pytest.raises(DomainError) as failure:
        prove(genesis, macro, [step(macro)])
    assert failure.value.code == "UNPROVEN_EQUIVALENCE"


def test_injury_validity_boundary_is_part_of_equivalence():
    genesis, ids = world()
    injury = {"kind": "RELATION_TRANSITION", "effect_key": "injury", "operation": "OPEN",
              "relation_id": str(uuid4()), "subject_id": ids["friend"], "predicate": "vital_state",
              "object_value": {"kind": "STRING", "value": "wounded"}, "at": point(30),
              "expected_relation_version_id": None}
    macro = event([injury], [ids["friend"]])
    replacement, proof = prove(genesis, macro, [step(macro)])
    assert proof["status"] == "PASS"
    assert replacement["effects"][0]["relation_id"] == injury["relation_id"]
    candidate = step(macro, [{**injury, "at": point(29)}], point(29))
    with pytest.raises(DomainError) as failure:
        prove(genesis, macro, [candidate])
    assert failure.value.code == "REFINEMENT_NOT_EQUIVALENT"


def test_net_zero_round_trip_cannot_hide_extra_shared_resource_use():
    genesis, ids = world()
    macro = event(transfer(ids), [ids[k] for k in ("warehouse", "relief", "grain")])
    extra = transfer(ids, "80", prefix="extra")
    reverse = [{**e, "effect_key": "reverse:" + e["effect_key"],
                "quantity": e["quantity"][1:] if e["quantity"].startswith("-") else "-" + e["quantity"]}
               for e in extra]
    with pytest.raises(DomainError) as failure:
        prove(genesis, macro, [step(macro), step(macro, extra), step(macro, reverse)])
    assert failure.value.code == "REFINEMENT_NOT_EQUIVALENT"


def test_common_start_balance_rejects_three_way_overcommit_even_with_net_return():
    genesis, ids = world()
    effects = transfer(ids, "60", prefix="one") + transfer(ids, "60", target="recipient", prefix="two")
    effects += [{"kind": "RESOURCE_DELTA", "effect_key": "return:out", "account_id": ids["reserve"],
                 "resource_id": ids["grain"], "unit": "bag", "quantity": "-30"},
                {"kind": "RESOURCE_DELTA", "effect_key": "return:in", "account_id": ids["warehouse"],
                 "resource_id": ids["grain"], "unit": "bag", "quantity": "30"}]
    macro = event(effects, [ids[k] for k in ("warehouse", "relief", "recipient", "reserve", "grain")])
    # The ordinary vector reducer has a nonnegative endpoint; the refinement
    # protocol still refuses to fund simultaneous actions with another receipt.
    assert replay(genesis, [macro])["resources"]
    with pytest.raises(DomainError) as failure:
        prove(genesis, macro, [step(macro, effects[:2]), step(macro, effects[2:4]), step(macro, effects[4:])])
    assert failure.value.code == "RESOURCE_CONFLICT"


def test_exact_causal_parent_reference_is_not_silently_rebound():
    genesis, ids = world()
    macro = event(transfer(ids), [ids[k] for k in ("warehouse", "relief", "grain")])
    successor = event(transfer(ids, "5", target="recipient", prefix="successor"), macro["participants"], point(31))
    successor["causal_parents"] = [{k: macro[k] for k in ("occurrence_id", "event_version_id")}]
    with pytest.raises(DomainError) as failure:
        prove(genesis, macro, [step(macro)], [successor])
    assert failure.value.code == "HARD_RULE_VIOLATION"


def test_unsupported_semantic_fields_and_duplicate_effect_identities_fail_closed():
    genesis, ids = world()
    macro = event(transfer(ids), [ids[k] for k in ("warehouse", "relief", "grain")])
    candidate = step(macro)
    candidate["unregistered_promise"] = "Treat this as equivalent"
    with pytest.raises(DomainError) as failure:
        prove(genesis, macro, [candidate])
    assert failure.value.code == "INVALID_ARGUMENT"
    with pytest.raises(DomainError) as failure:
        prove(genesis, macro, [step(macro), step(macro)])
    assert failure.value.code == "DUPLICATE_EFFECT"
