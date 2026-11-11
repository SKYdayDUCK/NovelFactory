"""Static schema linking matches the original and never memoizes input values."""
import json
from pathlib import Path

import pytest

from novel_factory.contracts import linked, registry
from novel_factory.domain.errors import DomainError


def test_static_linking_against_stored_examples_and_corruptions():
    root = Path(__file__).resolve().parents[2] / "contracts"
    indices = (
        ("examples/index.json", "2.0"),
        ("invalid_examples/core_index.json", "2.0"),
        ("examples/r1_index.json", "2.1"),
        ("examples/literary_index.json", "literary"),
    )
    for location, version in indices:
        path = root / location
        index = json.loads(path.read_bytes())
        entries = (index if location.startswith("invalid_examples/") else
                   {**index["examples"], **index.get("invalid_examples", {})})
        for file, spec in entries.items():
            name = spec if isinstance(spec, str) else spec.get("definition", spec.get("schema_def"))
            payload = json.loads((path.parent / file).read_bytes())
            variants = [payload, None, {"unexpected": 1}]
            if isinstance(payload, dict):
                variants.extend({k: v for k, v in payload.items() if k != drop} for drop in payload)
            for value in variants:
                expected = registry._validator(name, version).is_valid(value)
                source = registry._source(name, version)
                candidate = linked.linked_validator(source["$id"], name)
                assert candidate is not None
                actual = candidate.is_valid(value)
                assert actual == expected, (file, name, value.keys() if isinstance(value, dict) else None)


def test_candidate_still_validates_each_mutated_instance(monkeypatch):
    monkeypatch.setattr(registry, "_STATIC_SCHEMA_LINKS", True)
    root = Path(__file__).resolve().parents[2] / "contracts/examples"
    request = json.loads((root / "r1_fixed_owner_d25.json").read_bytes())
    result = registry.validate_contract("UnifiedQueryRequest", request, "2.1")
    result["read_contract"].clear()
    assert request["read_contract"]
    request["knowledge_cut"]["plane"] = "bad plane"
    with pytest.raises(DomainError, match="contract constraints failed"):
        registry.validate_contract("UnifiedQueryRequest", request, "2.1")


@pytest.mark.parametrize("unsupported", [
    {"$ref": "#/$defs/child", "maxLength": 2},
    {"$dynamicRef": "#anchor"},
    {"$id": "nested", "type": "string"},
    {"unevaluatedProperties": False},
    {"x-future-vocabulary": {"$ref": "#/$defs/child"}},
    {"$ref": "#/$defs/example"},
])
def test_unhandled_scope_and_recursive_refs_use_original_registry(monkeypatch, unsupported):
    uri = "https://example.invalid/test-linked"
    monkeypatch.setitem(linked._DOCUMENTS, uri, {"$schema": linked._DIALECT, "$defs": {"example": unsupported, "child": {}}})
    linked.linked_validator.cache_clear()
    assert linked.linked_validator(uri, "example") is None


@pytest.mark.parametrize("overrides", [
    {"$schema": "http://json-schema.org/draft-07/schema#"},
    {"$vocabulary": {"https://example.invalid/custom": True}},
])
def test_referenced_document_dialect_falls_back(monkeypatch, overrides):
    root = "https://example.invalid/root"
    other = "https://example.invalid/other"
    monkeypatch.setitem(linked._DOCUMENTS, root, {
        "$schema": linked._DIALECT, "$defs": {"example": {"$ref": other + "#/$defs/child"}}})
    monkeypatch.setitem(linked._DOCUMENTS, other, {
        "$schema": linked._DIALECT, "$defs": {"child": {"type": "integer"}}, **overrides})
    linked.linked_validator.cache_clear()
    assert linked.linked_validator(root, "example") is None


@pytest.mark.parametrize(("ref", "other"), [("A~1B", "A/B"), ("A%20B", "A B")])
def test_escaped_pointer_names_do_not_select_literal_competitor(monkeypatch, ref, other):
    uri = "https://example.invalid/escape"
    monkeypatch.setitem(linked._DOCUMENTS, uri, {
        "$schema": linked._DIALECT,
        "$defs": {"example": {"$ref": "#/$defs/" + ref}, ref: {"type": "string"},
                  other: {"type": "integer"}}})
    linked.linked_validator.cache_clear()
    assert linked.linked_validator(uri, "example") is None


@pytest.mark.parametrize(("root_name", "decoded"), [("A~1B", "A/B"), ("A%20B", "A B"), ("A/B", "A")])
def test_escaped_root_names_use_the_original_pointer(monkeypatch, root_name, decoded):
    uri = "https://example.invalid/root-escape"
    monkeypatch.setitem(linked._DOCUMENTS, uri, {
        "$schema": linked._DIALECT,
        "$defs": {root_name: {"type": "string"}, decoded: {"type": "integer"}}})
    linked.linked_validator.cache_clear()
    assert linked.linked_validator(uri, root_name) is None
