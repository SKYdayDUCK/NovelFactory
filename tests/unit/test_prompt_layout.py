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


def test_revised_prose_preserves_stable_creative_material_prefix_and_frozen_values():
    source = {"prose": ["第一版旧稿。", "不能丢弃的段落。"],
              "scene": [{"purpose": "同一场景", "opening": ["先", "后"]}],
              "style": [{"principles": ["简洁", "保留声音"]}],
              "continuity": [{"interpretation": "既有承接"}], "facts": [{"meaning": "known"}]}
    frozen = canonical_bytes(source)
    first = {"creative_materials": source, "assignment": "修订第一版"}
    second = {"creative_materials": {**source, "prose": ["第二版旧稿。", "仍须保留的段落。"]}, "assignment": "修订第二版"}
    before, after = material_json(first), material_json(second)
    prefix = before.split('"prose":', 1)[0]
    assert after.startswith(prefix)
    assert all('"' + key + '":' in prefix for key in ("scene", "style", "continuity", "facts"))
    assert json.loads(before) == first and json.loads(after) == second
    assert canonical_bytes(source) == frozen
    assert material_json({**first, "creative_materials": dict(reversed(list(source.items())))}) == before
    messages = role_messages("frozen role and schema", second, {"task_id": "new-task", "generation": 2})
    assert messages[1]["content"] == after
    assert "new-task" not in messages[0]["content"] + messages[1]["content"]
    assert '"task_id":"new-task"' in messages[2]["content"]


def test_explicit_context_recipe_array_priority_is_preserved():
    source = [{"section": "prose", "items": ["先给正文", "再给下一段"]},
              {"section": "scene", "items": [{"purpose": "后给场景"}]}]
    packet = {"creative_materials": source}
    assert material_json(packet) == canonical_bytes(packet).decode()
    assert json.loads(material_json(packet))["creative_materials"] == source


def test_author_length_policy_is_control_guidance_without_changing_story_materials():
    packet = {"assignment": "write and review the story", "creative_materials": {"prose": ["原文保留。"]},
              "length": {"min": 1500, "max": 2500}}
    messages = role_messages("frozen role and schema", packet, {"task_id": "bound-task"})
    assert messages[0]["content"] == "frozen role and schema"
    assert json.loads(messages[1]["content"]) == packet
    assert "advisory only" in messages[-1]["content"]
    assert "add chapters solely to meet a length target" in messages[-1]["content"]
    assert "factual, character-knowledge and literary review requirements" in messages[-1]["content"]
    assert '"task_id":"bound-task"' in messages[-1]["content"]
