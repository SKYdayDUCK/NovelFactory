"""Resolve static references in the bundled Draft 2020-12 schemas once.

The normal Draft validator still checks each instance. No instance, task,
permission, or validation result is cached. Unsupported reference constructs
fall back to the ordinary registry instead of guessing their evaluation scope.
"""
from functools import lru_cache
from urllib.parse import urldefrag, urljoin

from jsonschema import Draft202012Validator, FormatChecker

from ._schema_bundle import SCHEMAS


_DOCUMENTS = {doc["$id"]: doc for doc in SCHEMAS.values()}
_DIALECT = "https://json-schema.org/draft/2020-12/schema"
_DOCUMENT_KEYS = {"$schema", "$id", "$defs", "title", "description"}
_SCHEMA_MAPS = ("properties",)
_SCHEMA_LISTS = ("oneOf", "allOf", "anyOf")
_SINGLE_SCHEMAS = ("items", "additionalProperties", "not", "if", "then", "else")
# Positive list for this closed bundle. New vocabularies, evaluation-dependent
# keywords, dynamic scope, nested resource IDs and annotation collectors use
# the registry until their evaluation has been checked.
_SUPPORTED = {
    "$ref", "required", "type", "maxLength", "minLength", "maxItems", "minItems",
    "minimum", "maximum", "const", "enum", "description", "title", "uniqueItems",
    "format", "pattern", "maxProperties", *_SCHEMA_MAPS, *_SCHEMA_LISTS, *_SINGLE_SCHEMAS,
}


def _plain_name(name):
    return name.isascii() and name.isidentifier()


class _UseRegistry(Exception):
    pass


@lru_cache(maxsize=256)
def linked_validator(document_uri, name):
    """Return None when evaluation requires the original registry."""
    if not _plain_name(name):
        # The original root validator uses a JSON Pointer built from this
        # name. Preserve its RFC 6901 and URI fragment decoding rules.
        return None
    resolved = {}

    def definition(uri, key, seen):
        target = (uri, key)
        document = _DOCUMENTS.get(uri)
        if (target in seen or not document or document.get("$schema") != _DIALECT
                or document.keys() - _DOCUMENT_KEYS or key not in document.get("$defs", {})):
            raise _UseRegistry
        if target not in resolved:
            resolved[target] = link(document["$defs"][key], uri, seen | {target})
        return resolved[target]

    def link(node, uri, seen):
        if isinstance(node, bool):
            return node
        if node.keys() - _SUPPORTED:
            raise _UseRegistry
        if "$ref" in node:
            dest, fragment = urldefrag(urljoin(uri, node["$ref"]))
            parts = fragment.split("/")
            if len(node) != 1 or len(parts) != 3 or parts[:2] != ["", "$defs"]:
                raise _UseRegistry
            # Escaped or percent-encoded JSON Pointer segments must be resolved
            # by the registry, even if the literal escaped name also exists.
            key = parts[2]
            if not _plain_name(key):
                raise _UseRegistry
            return definition(dest, key, seen)
        result = dict(node)
        for key in _SCHEMA_MAPS:
            if key in result:
                result[key] = {prop: link(child, uri, seen) for prop, child in result[key].items()}
        for key in _SCHEMA_LISTS:
            if key in result:
                result[key] = [link(child, uri, seen) for child in result[key]]
        for key in _SINGLE_SCHEMAS:
            if key in result:
                result[key] = link(result[key], uri, seen)
        return result

    try:
        schema = definition(document_uri, name, frozenset())
    except _UseRegistry:
        return None
    return Draft202012Validator(schema, format_checker=FormatChecker())
