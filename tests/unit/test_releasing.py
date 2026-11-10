"""P2-06: writing DAGs are independent of completion and story chronology."""
from copy import deepcopy
from uuid import uuid4

import pytest

from novel_factory.contracts.registry import validate_contract
from novel_factory.domain.errors import DomainError
from novel_factory.domain.releasing import (
    manifest_digest,
    release_diff,
    scene_readiness,
    validate_release_order,
    validate_scene_slots,
)


def slot(name, ordinal, parents=(), *, tick=10):
    return {"scene_id": name, "reader_cut_ordinal": ordinal,
            "story_interval": {"start": {"tick": tick, "phase": 0}, "end": {"tick": tick + 1, "phase": 0}, "bounds": "[)"},
            "required_predecessor_scene_ids": list(parents)}


def scene(item):
    return {"scene_contract": {"scene_id": item["scene_id"], "reader_cut_ordinal": item["reader_cut_ordinal"]},
            "required_predecessor_scene_ids": item["required_predecessor_scene_ids"]}


def test_independent_scenes_ready_together_successor_waits_for_exact_predecessors():
    slots = [slot("east", 0), slot("west", 1), slot("meeting", 2, ["east", "west"])]
    initial = scene_readiness(slots, [])
    assert initial["ready_scene_ids"] == ["east", "west"]
    assert initial["blocked_scenes"] == [{"scene_id": "meeting", "missing_predecessor_scene_ids": ["east", "west"]}]
    assert scene_readiness(slots, ["west"])["ready_scene_ids"] == ["east"]
    assert scene_readiness(slots, ["west", "east"])["ready_scene_ids"] == ["meeting"]


@pytest.mark.parametrize("slots,code", [
    ([slot("a", 0, ["b"]), slot("b", 1, ["a"])], "SCENE_DEPENDENCY_CYCLE"),
    ([slot("a", 0, ["missing"])], "INVALID_ARGUMENT"),
    ([slot("a", 0), slot("b", 0)], "INVALID_ARGUMENT"),
    ([slot("a", 0), slot("a", 1)], "INVALID_ARGUMENT"),
])
def test_ambiguous_or_unresolvable_batch_dependencies_rejected(slots, code):
    with pytest.raises(DomainError) as error:
        validate_scene_slots(slots)
    assert error.value.code == code


def test_flashback_story_time_does_not_rewrite_reader_order():
    slots = [slot("later", 0, tick=30), slot("earlier_memory", 1, tick=10)]
    validate_scene_slots(slots)
    assert validate_release_order([scene(item) for item in slots], ["later", "earlier_memory"]) == ["later", "earlier_memory"]
    with pytest.raises(DomainError, match="reader order|narrative arrangement"):
        validate_release_order([scene(item) for item in reversed(slots)], ["later", "earlier_memory"])


def test_release_cannot_silently_omit_blocked_or_repeated_scene():
    a, b = slot("a", 0), slot("b", 1, ["a"])
    for values in [[scene(a)], [scene(a), scene(a)]]:
        with pytest.raises(DomainError) as error:
            validate_release_order(values, ["a", "b"])
        assert error.value.code == "RELEASE_INCOMPLETE"
    with pytest.raises(DomainError) as error:
        validate_release_order([scene(b)], ["b"])
    assert error.value.code == "RELEASE_INCOMPLETE"


def test_manifest_digest_binds_contents_and_versions_without_self_reference():
    value = {"release_id": "fixed", "world_ref": {"revision": "r1"}, "ordered_artifacts": [{"sha256": "text-a"}],
             "disclosure_revision_id": "d1", "approval_refs": ["approval-a"]}
    original = manifest_digest(value)
    value["manifest_sha256"] = original
    assert manifest_digest(value) == original
    for field, replacement in [("world_ref", {"revision": "r2"}), ("disclosure_revision_id", "d2"),
                               ("approval_refs", ["approval-b"]), ("ordered_artifacts", [{"sha256": "text-b"}])]:
        changed = deepcopy(value)
        changed[field] = replacement
        assert manifest_digest(changed) != original


def test_release_diff_reports_text_contract_order_and_cut_separately():
    old_manifest = {"world_ref": "w1", "style_refs": [], "literary_refs": [], "disclosure_revision_id": "d1",
        "ordered_artifacts": [{"scene_id": "a", "ordinal": 0, "artifact_ref": "a1"},
                              {"scene_id": "b", "ordinal": 1, "artifact_ref": "b1"}]}
    old_record = {"plan_ref": "p1", "ordered_scene_refs": ["as1", "bs1"]}
    new_manifest = {**old_manifest, "world_ref": "w2", "disclosure_revision_id": "d2", "ordered_artifacts": [
        {"scene_id": "b", "ordinal": 0, "artifact_ref": "b1"}, {"scene_id": "a", "ordinal": 1, "artifact_ref": "a2"}]}
    new_record = {"plan_ref": "p1", "ordered_scene_refs": ["bs2", "as2"]}
    diff = release_diff(old_manifest, old_record, new_manifest, new_record)
    assert diff["changed_text_scene_ids"] == ["a"]
    assert diff["changed_contract_scene_ids"] == ["a", "b"]
    assert diff["reordered_scene_ids"] == ["a", "b"]
    assert diff["world_changed"] and diff["disclosure_changed"]
    assert not diff["plan_changed"] and not diff["style_changed"]


def test_release_contract_is_closed_and_requires_exact_world_and_approval_refs():
    def uid():
        return str(uuid4())
    ref = {"artifact_id": uid(), "artifact_revision_id": uid(), "sha256": "a" * 64}
    wr = {"work_id": uid(), "branch_id": uid(), "world_revision_id": uid()}
    payload = {"schema_version": "2.2", "world_ref": wr, "volume_id": uid(), "plan_ref": ref,
        "batch_refs": [ref], "ordered_acceptance_refs": [ref], "editorial_approval_ref": ref,
        "narrative_revision_id": uid(), "arrangement_version": "v1", "disclosure_revision_id": uid(),
        "style_refs": [], "literary_refs": [], "prior_release_ref": None, "revision_reason": None}
    assert validate_contract("ReleaseRequest", payload, "release") == payload
    mutations = [dict(payload, world_effects_applied=1), dict(payload, world_ref={"mode": "LATEST"}),
                 dict(payload, editorial_approval_ref=uid()), dict(payload, ordered_acceptance_refs=[])]
    for invalid in mutations:
        with pytest.raises(DomainError) as error:
            validate_contract("ReleaseRequest", invalid, "release")
        assert error.value.code == "INVALID_ARGUMENT"
