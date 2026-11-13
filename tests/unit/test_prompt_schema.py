import json

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
