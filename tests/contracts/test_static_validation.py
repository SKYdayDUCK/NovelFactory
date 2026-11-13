"""Compare both paths on malformed discriminator, numeric and conditional values."""
from copy import deepcopy
import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from novel_factory.contracts.registry import _REGISTRY, _source, _static_plan
from novel_factory.contracts.static_validation import StaticValidationPlan

EXAMPLES = Path(__file__).resolve().parents[2] / 'contracts' / 'examples'


def test_static_and_standard_validators_agree_on_mutated_wire_contracts():
    index = json.loads((EXAMPLES / 'r1_index.json').read_text())
    compiled = _static_plan()
    assert compiled.supported
    for filename, definition in index['examples'].items():
        payload = json.loads((EXAMPLES / filename).read_text())
        source = _source(definition, '2.1')
        ordinary = Draft202012Validator(
            {'$ref': source['$id'] + '#/$defs/' + definition},
            registry=_REGISTRY, format_checker=FormatChecker(),
        )
        optimized = compiled.validator(source, definition)
        candidates = [payload, {}, False, {'extra': True, **payload}]
        for field in payload:
            missing = deepcopy(payload)
            del missing[field]
            candidates.extend((missing, {**payload, field: None}, {**payload, field: True}))
        for candidate in candidates:
            assert optimized.is_valid(candidate) == ordinary.is_valid(candidate), (filename, candidate)


def test_dynamic_subschema_forces_complete_bundle_fallback():
    uri = 'https://example.test/contracts'
    schema = {'$id': uri, '$schema': 'https://json-schema.org/draft/2020-12/schema',
              '$defs': {'Root': {'type': 'object', 'properties': {'value': {'$dynamicRef': '#x'}}}}}
    assert not StaticValidationPlan({uri: schema}).supported
