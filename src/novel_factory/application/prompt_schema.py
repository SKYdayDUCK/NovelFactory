"""Self-contained, bounded JSON Schemas for role prompts."""

from functools import lru_cache
from urllib.parse import urljoin

from novel_factory.application.kernel import require
from novel_factory.contracts._schema_bundle import SCHEMAS
from novel_factory.contracts.registry import schema_for
from novel_factory.domain.canonical import canonical_bytes


def inline_schema(name, version):
    """Give the model the selected schema, not every unrelated contract."""
    documents = {s["$id"]: s for s in SCHEMAS.values()}
    source = schema_for(name, version)
    def expand(value, document, seen=()):
        if isinstance(value, list):
            return [expand(item, document, seen) for item in value]
        if not isinstance(value, dict):
            return value
        if "$ref" in value:
            ref = value["$ref"]
            root, definition = ref.split("#/$defs/")
            target = documents[urljoin(document["$id"], root)] if root else document
            identity = target["$id"] + "#/$defs/" + definition
            require(identity not in seen, "INVALID_MODEL_CONFIG", "Recursive model output schemas require a bounded adapter")
            return expand(target["$defs"][definition], target, seen + (identity,))
        return {k: expand(v, document, seen) for k, v in value.items()}
    return expand(source["$defs"][name], source)


@lru_cache(maxsize=64)
def prompt_schema(name, version):
    """Keep only reachable definitions. For small schemas use the shorter inline form."""
    source = schema_for(name, version)
    documents = {doc["$id"]: doc for doc in SCHEMAS.values()}
    names = {}
    definitions = {}

    def localize(value, document):
        if isinstance(value, list):
            return [localize(part, document) for part in value]
        if not isinstance(value, dict):
            return value
        result = {k: localize(v, document) for k, v in value.items() if k != "$ref"}
        if "$ref" in value:
            path, definition = value["$ref"].split("#/$defs/")
            origin = urljoin(document["$id"], path) if path else document["$id"]
            key = (origin, definition)
            if key not in names:
                # Contract documents may reuse definition names.
                alias = definition
                while alias in names.values():
                    alias += "_"
                names[key] = alias
                target = documents[origin]
                definitions[alias] = localize(target["$defs"][definition], target)
            result["$ref"] = "#/$defs/" + names[key]
        return result

    schema = localize(source["$defs"][name], source)
    if definitions:
        schema["$defs"] = definitions
    compact = canonical_bytes(schema)
    inline = canonical_bytes(inline_schema(name, version))
    return min((inline, compact), key=len).decode()
