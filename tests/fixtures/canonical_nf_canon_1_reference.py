"""NF-CANON-1: exact JSON fingerprints, not a claim to implement RFC 8785."""
from __future__ import annotations

import hashlib
import json
from typing import Any
from .errors import DomainError

MAX_SAFE_INTEGER = 9007199254740991


def _check(value: Any) -> None:
    if value is None or type(value) is bool:
        return
    if type(value) is int:
        if abs(value) > MAX_SAFE_INTEGER:
            raise DomainError("INVALID_ARGUMENT", "Integer outside the JSON safe range")
        return
    if type(value) is str:
        try:
            value.encode("utf-8", "strict")
        except UnicodeEncodeError as exc:
            raise DomainError("INVALID_ARGUMENT", "Invalid Unicode") from exc
        return
    if type(value) is list:
        for item in value:
            _check(item)
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise DomainError("INVALID_ARGUMENT", "JSON object keys must be strings")
            _check(key)
            _check(item)
        return
    raise DomainError("INVALID_ARGUMENT", "Canonical JSON accepts no floats or custom objects")


def canonical_bytes(value: Any) -> bytes:
    _check(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def strict_json_loads(content: str | bytes) -> Any:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise DomainError("INVALID_ARGUMENT", "Duplicate JSON object key")
            result[key] = value
        return result
    def reject_number(value):
        raise DomainError("INVALID_ARGUMENT", "Floats are not allowed in canonical JSON")
    try:
        value = json.loads(content, object_pairs_hook=pairs, parse_float=reject_number,
                           parse_constant=reject_number)
    except (ValueError, UnicodeError) as exc:
        raise DomainError("INVALID_ARGUMENT", "Malformed JSON") from exc
    _check(value)
    return value
