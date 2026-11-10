"""Pure writing dependency and immutable release comparison rules.

Story chronology, reader order, and task completion order remain separate.
Only explicitly accepted predecessor artifacts satisfy a writing dependency.
"""
from __future__ import annotations

from novel_factory.domain.canonical import fingerprint
from novel_factory.domain.errors import DomainError
from novel_factory.domain.temporal import validate_interval


def _require(condition, code, message):
    if not condition:
        raise DomainError(code, message)


def validate_scene_slots(slots):
    """Validate a bounded DAG without imposing story chronology on reader order."""
    by_id = {slot["scene_id"]: slot for slot in slots}
    _require(len(by_id) == len(slots), "INVALID_ARGUMENT", "Duplicate scene slot")
    ordinals = [slot["reader_cut_ordinal"] for slot in slots]
    _require(len(set(ordinals)) == len(ordinals), "INVALID_ARGUMENT", "Duplicate reader ordinal")
    pending = {}
    for key, slot in by_id.items():
        validate_interval(slot["story_interval"])
        parents = slot["required_predecessor_scene_ids"]
        _require(len(parents) == len(set(parents)), "INVALID_ARGUMENT", "Duplicate scene dependency")
        _require(set(parents) <= by_id.keys() and key not in parents,
                 "INVALID_ARGUMENT", "Unknown or self-referential scene dependency")
        pending[key] = set(parents)
    resolved = set()
    while pending:
        ready = {key for key, parents in pending.items() if parents <= resolved}
        _require(ready, "SCENE_DEPENDENCY_CYCLE", "Writing dependencies must form a DAG")
        resolved.update(ready)
        pending = {key: parents for key, parents in pending.items() if key not in ready}
    return by_id


def scene_readiness(slots, accepted_scene_ids):
    by_id = validate_scene_slots(slots)
    accepted = set(accepted_scene_ids)
    _require(accepted <= by_id.keys(), "INVALID_ARGUMENT", "Handoff is outside this writing batch")
    ready, blocked = [], []
    for slot in sorted(slots, key=lambda item: item["reader_cut_ordinal"]):
        key = slot["scene_id"]
        if key in accepted:
            continue
        missing = sorted(set(slot["required_predecessor_scene_ids"]) - accepted)
        if missing:
            blocked.append({"scene_id": key, "missing_predecessor_scene_ids": missing})
        else:
            ready.append(key)
    return {"ready_scene_ids": ready, "blocked_scenes": blocked,
            "accepted_scene_ids": sorted(accepted)}


def validate_release_order(scene_payloads, expected_scene_ids):
    """Require exact selected batch coverage and the already reviewed reader order."""
    ids = [scene["scene_contract"]["scene_id"] for scene in scene_payloads]
    _require(len(ids) == len(set(ids)) and set(ids) == set(expected_scene_ids),
             "RELEASE_INCOMPLETE", "Release must cover exactly the selected batch scene slots")
    ordinals = [scene["scene_contract"]["reader_cut_ordinal"] for scene in scene_payloads]
    _require(ordinals == sorted(set(ordinals)), "RELEASE_ORDER_MISMATCH",
             "Text order differs from the reviewed narrative arrangement")
    for scene in scene_payloads:
        _require(set(scene["required_predecessor_scene_ids"]) <= set(ids),
                 "RELEASE_INCOMPLETE", "A required scene handoff is missing")
    return ids


def manifest_digest(manifest):
    """The core manifest hashes its canonical body excluding its own digest."""
    return fingerprint({key: value for key, value in manifest.items() if key != "manifest_sha256"})


def release_diff(prior_manifest, prior_record, manifest, record):
    old_items = {item["scene_id"]: item for item in prior_manifest["ordered_artifacts"]} if prior_manifest else {}
    new_items = {item["scene_id"]: item for item in manifest["ordered_artifacts"]}
    old_scenes = dict(zip(old_items, prior_record["ordered_scene_refs"], strict=True)) if prior_record else {}
    new_scenes = dict(zip(new_items, record["ordered_scene_refs"], strict=True))
    shared = old_items.keys() & new_items.keys()
    return {
        "added_scene_ids": sorted(new_items.keys() - old_items.keys()),
        "removed_scene_ids": sorted(old_items.keys() - new_items.keys()),
        "changed_text_scene_ids": sorted(key for key in shared if old_items[key]["artifact_ref"] != new_items[key]["artifact_ref"]),
        "changed_contract_scene_ids": sorted(key for key in shared if old_scenes[key] != new_scenes[key]),
        "reordered_scene_ids": sorted(key for key in shared if old_items[key]["ordinal"] != new_items[key]["ordinal"]),
        "world_changed": prior_manifest is not None and prior_manifest["world_ref"] != manifest["world_ref"],
        "plan_changed": prior_record is not None and prior_record["plan_ref"] != record["plan_ref"],
        "style_changed": prior_manifest is not None and prior_manifest["style_refs"] != manifest["style_refs"],
        "literary_changed": prior_manifest is not None and prior_manifest["literary_refs"] != manifest["literary_refs"],
        "disclosure_changed": prior_manifest is not None and prior_manifest["disclosure_revision_id"] != manifest["disclosure_revision_id"],
    }
