"""Strict optional structured belief payload inside legacy statement strings.

A normal statement remains prose. Only this explicit closed interpreter format
can support a typed BELIEF state query. A statement is never WORLD evidence.
"""
from __future__ import annotations
from novel_factory.contracts.registry import validate_contract
from .canonical import canonical_bytes, strict_json_loads
from .errors import DomainError

PREFIX = 'NF-PROP-1:'


def encode_proposition(subject_id: str, predicate: str, object_value: dict, subject_time: dict | None = None) -> str:
    value = {'subject_id': subject_id, 'predicate': predicate, 'object_value': object_value}
    if subject_time is not None:
        value['subject_time'] = subject_time
    validate_contract('CognitiveProposition', value, 'kernel')
    return PREFIX + canonical_bytes(value).decode('utf-8')


def decode_proposition(statement: str) -> dict | None:
    if not statement.startswith(PREFIX):
        return None
    value = strict_json_loads(statement[len(PREFIX):])
    return validate_contract('CognitiveProposition', value, 'kernel')
