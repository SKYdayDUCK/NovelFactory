"""Changing dispatch identity must not invalidate reusable authorized source bytes."""

import json

from novel_factory.application.prompt_layout import material_json, role_messages
from novel_factory.domain.canonical import canonical_bytes


def test_new_task_reuses_entire_instructions_and_material_prefix_without_losing_binding():
    packet = {"assignment": "review", "creative_materials": {"prose": ["原文", "另段"]}}
    first = role_messages("frozen role and schema", packet, {"task_id": "first", "generation": 1}, exact_envelope=True)
    second = role_messages("frozen role and schema", packet, {"task_id": "second", "generation": 2}, exact_envelope=True)
    assert first[:2] == second[:2]
    assert first[2] != second[2]
    assert '"task_id":"second"' in second[2]["content"]
    assert json.loads(first[1]["content"]) == packet
    assert first[0]["role"] == first[2]["role"] == "system"


def test_changed_assignment_retains_source_prefix_and_every_field_and_list_order():
    source = {"prose": ["a", "b"], "belief": None}
    packet = {"assignment": "old", "creative_materials": source, "other": {"z": 1, "a": False}}
    new = {**packet, "assignment": "new"}
    before, after = material_json(packet), material_json(new)
    assert before.split(',"assignment":')[0] == after.split(',"assignment":')[0]
    assert json.loads(after) == new
    assert len(after.encode()) == len(canonical_bytes(new))
    assert material_json(dict(reversed(list(new.items())))) == after
