import json

import pytest
from jsonschema import Draft202012Validator

from novel_factory.application.agent_gateway import OUTPUT_CONTRACTS, _inline_schema
from novel_factory.application.prompt_schema import prompt_schema
from novel_factory.domain.canonical import canonical_bytes


def expand(value, definitions):
    if isinstance(value, list):
        return [expand(item, definitions) for item in value]
    if not isinstance(value, dict):
        return value
    if "$ref" in value:
        assert set(value) == {"$ref"} and value["$ref"].startswith("#/$defs/")
        return expand(definitions[value["$ref"].split("/")[-1]], definitions)
    return {key: expand(part, definitions) for key, part in value.items()}


def test_compact_role_contracts_expand_exactly_to_original_constraints():
    reductions = {}
    for name, version in set(OUTPUT_CONTRACTS.values()):
        original = _inline_schema(name, version)
        compact = json.loads(prompt_schema(name, version))
        Draft202012Validator.check_schema(compact)
        definitions = compact.pop("$defs", {})

        assert expand(compact, definitions) == original
        reductions[name] = len(canonical_bytes(original)) - len(prompt_schema(name, version).encode())
    assert all(saved >= 0 for saved in reductions.values())
    assert reductions["ActionProposal"] > 0


def test_fact_reader_gets_conditional_assertion_fields_and_exact_quotes():
    from novel_factory.application.rolling_facts import assignment

    instructions = assignment()
    assert len(instructions) <= 32_000
    schema = json.loads(instructions.split("内层 JSON Schema：", 1)[1])
    Draft202012Validator.check_schema(schema)
    schema_check = Draft202012Validator(schema)
    claim = {"classification": "SURFACE_DETAIL", "quote": "灯亮了。", "summary": "光线明亮",
             "event_ref": None, "world_check": None, "knowledge_use": None, "proposed_command": None,
             "disclosure_id": None, "declared_delta_index": None}
    assert schema_check.is_valid({"assertions": [claim]})
    assert not schema_check.is_valid({"assertions": [{**claim, "quote": ""}]})
    assert not schema_check.is_valid({"assertions": [{**claim, "classification": "NEW_WORLD_EFFECT"}]})
    assert not schema_check.is_valid({"assertions": [{**claim, "classification": "EXISTING_EVENT_PRESENTATION"}]})


def test_lossless_payload_normalization_only_accepts_empty_metadata_and_known_design_fields():
    from novel_factory.application.prompt_schema import validated_payload
    from novel_factory.domain.errors import DomainError

    design = {"title": "回声", "protagonist": "林", "place": "市集", "storyline": "追查回声",
              "world_rules": ["回声会重复"], "chapter_goals": ["寻找声音"]}
    def decode(extra, *, unresolved=None):
        envelope = {"payload": {**design, **extra}, "unresolved_questions": unresolved or []}
        return validated_payload(envelope, "FirstWorldDesign", "production_profile")

    assert decode({"chapter_goals_note": None, "unresolved_questions": []}) == design
    for extra in ({"chapter_goals_note": ""}, {"made_up_note": None}, {"opening_note": None},
                  {"mystery": None}, {"unresolved_questions": ["?"]}):
        with pytest.raises(DomainError):
            decode(extra)
    with pytest.raises(DomainError):
        decode({"unresolved_questions": []}, unresolved=["?"])
    with pytest.raises(DomainError):
        validated_payload({"payload": {"text": "中文", "text_note": None, "event_refs": [],
                                      "important_deltas": [], "evidence_refs": []},
                           "unresolved_questions": []}, "AgentTextOutput", "agent")


@pytest.mark.parametrize("name", ["FirstWorldDesign", "OpeningWorldDesign", "EnvironmentalOpeningWorldDesign"])
def test_window_constraint_specializes_each_native_design_schema(name):
    from novel_factory.application.idea_window import design_prompt_schema

    binding = {"role_id": "world_designer", "task_kind": "initial_world",
               "output_contract": {"version": "production_profile", "name": name},
               "assignment": json.dumps({"first_window_limit": 2, "window_constraints_version": 1})}
    schema = Draft202012Validator(json.loads(design_prompt_schema(binding)))
    candidate = {"title": "输入", "protagonist": "甲", "place": "乙", "storyline": "合成流程测试。",
                 "world_rules": ["测试规则。"], "chapter_goals": ["一", "二"],
                 "opening": {"situation": "测试", "counterpart": "丙", "other_place": "丁",
                             "protagonist_knowledge": "甲方信息", "counterpart_knowledge": "丙方信息",
                             "environment": {"moving_entity": "雨"}}}
    assert schema.is_valid(candidate)
    assert not schema.is_valid({**candidate, "chapter_goals": ["一", "二", "三"]})
    assert not schema.is_valid({**candidate, "chapter_goals": ["甲", "甲"]})
