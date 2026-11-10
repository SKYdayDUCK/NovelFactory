"""Dedicated strict evaluation contracts; the runtime schema bundle is unchanged."""
from copy import deepcopy
from functools import lru_cache
from importlib.resources import files
import json

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from novel_factory.contracts._schema_bundle import SCHEMAS
from novel_factory.domain.canonical import canonical_bytes
from novel_factory.domain.errors import DomainError


@lru_cache(maxsize=1)
def schema():
    return json.loads(files("novel_factory.evaluation").joinpath("schema.json").read_text(encoding="utf-8"))


@lru_cache(maxsize=32)
def _validator(name):
    document = schema()
    sources = [*SCHEMAS.values(), document]
    registry = Registry().with_resources((source["$id"], Resource.from_contents(source)) for source in sources)
    if name not in document["$defs"]:
        raise DomainError("UNSUPPORTED_EVALUATION_SCHEMA", "Unknown evaluation contract")
    return Draft202012Validator({"$schema": document["$schema"], "$ref": document["$id"] + "#/$defs/" + name},
                                registry=registry, format_checker=FormatChecker())


def validate(name, value):
    canonical_bytes(value)
    if not _validator(name).is_valid(value):
        raise DomainError("INVALID_EVALUATION_INPUT", "Evaluation contract constraints failed: " + name)
    return deepcopy(value)
