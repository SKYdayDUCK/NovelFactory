"""The finite fixture extractor must read facts from text, not scene answers."""
from copy import deepcopy

from novel_factory.fixtures import fixture_definition
from novel_factory.orchestration.short_volume_extraction import extract_fixture_assertions
from novel_factory.orchestration.short_volume_fixture import TEXTS


def inputs():
    ids = fixture_definition("white_bridge")["ids"]
    source = {"event_ref": {"occurrence_id": ids["warehouse"], "event_version_id": ids["relief"]},
        "payload": {"effects": [
            {"kind": "RESOURCE_DELTA", "account_id": ids["warehouse"], "resource_id": ids["grain"], "quantity": "-30", "unit": "bag"},
            {"kind": "RESOURCE_DELTA", "account_id": ids["relief"], "resource_id": ids["grain"], "quantity": "30", "unit": "bag"},
        ]}}
    return ids, {"joint": source}


def test_changed_quantity_is_extracted_and_conflicts_with_the_selected_event():
    ids, sources = inputs()
    text = TEXTS[2].replace("三十袋", "四十袋")
    assertions, audit = extract_fixture_assertions(text, 2, ids, sources)
    transfer = next(row["parsed"] for row in audit["observations"] if row["rule"] == "transfer")
    assert transfer == {"kind": "RESOURCE_TRANSFER", "quantity": "40", "event_effect_match": False}
    assert any(a["classification"] == "HISTORICAL_CONFLICT" for a in assertions)
    balances = [a["world_check"]["expected_value"]["quantity"] for a in assertions
                if a["world_check"] and a["world_check"]["predicate"].startswith("resource:")]
    assert balances == ["70", "40"]


def test_changed_status_becomes_active_check_instead_of_copying_destroyed():
    ids, sources = inputs()
    assertions, _ = extract_fixture_assertions(TEXTS[2].replace("铜汛印被压毁了", "铜汛印仍完好"), 2, ids, sources)
    checks = [a["world_check"] for a in assertions if a["world_check"] and a["world_check"]["predicate"] == "lifecycle"]
    assert len(checks) == 1 and checks[0]["expected_value"] == {"kind": "STRING", "value": "ACTIVE"}


def test_unrecognised_new_material_claim_fails_closed_but_prose_is_not_a_golden_hash():
    ids, sources = inputs()
    prose_change = TEXTS[2].replace("潮面缓缓低下去", "潮面一点一点地低下去")
    accepted, _ = extract_fixture_assertions(prose_change, 2, ids, sources)
    assert not any(a["classification"] in {"UNCERTAIN", "HISTORICAL_CONFLICT"} for a in accepted)
    assertions, audit = extract_fixture_assertions(prose_change + "\n\n铜汛印焕然一新。", 2, ids, sources)
    assert any(a["classification"] == "UNCERTAIN" for a in assertions)
    assert any(row.get("parsed", {}).get("status") == "UNRECOGNISED_VALUE" for row in audit["observations"])
    unsupported, audit = extract_fixture_assertions(prose_change + "\n\n林澈说铜汛印又归了苏弦。", 2, ids, sources)
    assert any(a["classification"] == "UNCERTAIN" for a in unsupported)
    assert any(row.get("status") == "UNRECOGNISED_CRITICAL_SPAN" for row in audit["observations"])


def test_matching_event_identity_does_not_hide_changed_transfer_effects():
    ids, sources = inputs()
    sources = deepcopy(sources)
    sources["joint"]["payload"]["effects"][0]["quantity"] = "-20"
    assertions, _ = extract_fixture_assertions(TEXTS[2], 2, ids, sources)
    assert any(a["classification"] == "HISTORICAL_CONFLICT" for a in assertions)


def test_unsupported_negation_or_ambiguous_numeral_cannot_match_a_valid_substring():
    ids, sources = inputs()
    for replacement in ("并非北仓留下七十袋", "北仓留下七十袋并非事实", "北仓留下三四十袋"):
        text = TEXTS[2].replace("北仓留下七十袋", replacement)
        assertions, _ = extract_fixture_assertions(text, 2, ids, sources)
        assert any(a["classification"] == "UNCERTAIN" for a in assertions)
