"""Schema is the only boundary authority; generated adapters validate it in full.

The bundle is generated from checked-in JSON Schema, not loaded from a mutable
working directory. Generated Pydantic adapters retain source conditional rules
through the Draft 2020-12 validator. No schema retrieval uses the network.
"""
from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
from typing import Any, Annotated, ClassVar, Literal, Union
from urllib.parse import urljoin

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from pydantic import BaseModel, ConfigDict, Field, RootModel, create_model, model_validator

from ._schema_bundle import SCHEMAS
from novel_factory.domain.errors import DomainError

_BY_ID = {schema["$id"]: schema for schema in SCHEMAS.values()}
_REGISTRY = Registry().with_resources((key, Resource.from_contents(value)) for key, value in _BY_ID.items())
_FILES = {'2.0': 'core.schema.json', '2.1': 'temporal_concurrency.schema.json', 'literary': 'literary.schema.json', 'kernel': 'kernel.schema.json', 'commands': 'commands.schema.json', 'http': 'http.schema.json', 'planning': 'planning.schema.json', 'simulation': 'simulation.schema.json', 'agent': 'agent.schema.json', 'narrative': 'narrative.schema.json', 'editorial': 'editorial.schema.json', 'release': 'release.schema.json', 'refinement': 'refinement.schema.json', 'continuity': 'continuity.schema.json'}


def _source(name: str, version: str):
    filename = _FILES.get(version)
    if filename not in SCHEMAS:
        raise DomainError("UNSUPPORTED_SCHEMA", "Unsupported schema version")
    schema = SCHEMAS[filename]
    if name not in schema["$defs"]:
        # R1 reuses core definitions rather than redefining them.
        if version == "2.1" and name in SCHEMAS["core.schema.json"]["$defs"]:
            return SCHEMAS["core.schema.json"]
        raise DomainError("UNSUPPORTED_SCHEMA", "Unknown contract definition")
    return schema


@lru_cache(maxsize=256)
def _validator(name: str, version: str):
    source = _source(name, version)
    schema = {"$schema": source["$schema"], "$ref": source["$id"] + "#/$defs/" + name}
    return Draft202012Validator(schema, registry=_REGISTRY, format_checker=FormatChecker())


def plain(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", exclude_unset=True)
    if isinstance(value, dict):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    return value


def validate_contract(name: str, payload: Any, version: str = "2.0") -> Any:
    value = plain(payload)
    errors = sorted(_validator(name, version).iter_errors(value), key=lambda e: str(list(e.path)))
    if errors:
        # Do not echo instance values: they can be secret or attacker-controlled.
        raise DomainError("INVALID_ARGUMENT", f"Invalid {name}: contract constraints failed")
    return deepcopy(value)


def schema_for(name: str, version: str = "2.0") -> dict:
    source = deepcopy(_source(name, version))
    source["$ref"] = source["$id"] + "#/$defs/" + name
    return source


class _ContractMixin:
    contract_name: ClassVar[str]
    contract_version: ClassVar[str]

    @model_validator(mode="before")
    @classmethod
    def _enforce_schema(cls, value):
        if isinstance(value, cls):
            value = value.model_dump(mode="json")
        try:
            return validate_contract(cls.contract_name, value, cls.contract_version)
        except DomainError as exc:
            raise ValueError(exc.safe_message) from exc

    @classmethod
    def model_json_schema(cls, *args, **kwargs):
        """Return the authoritative schema, including conditionals and bounds."""
        return schema_for(cls.contract_name, cls.contract_version)

    def model_dump(self, *args, **kwargs):
        kwargs.setdefault("exclude_unset", True)
        return super().model_dump(*args, **kwargs)

    def model_dump_json(self, *args, **kwargs):
        kwargs.setdefault("exclude_unset", True)
        return super().model_dump_json(*args, **kwargs)


class ContractModel(_ContractMixin, BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ContractValue(_ContractMixin, RootModel[Any]):
    model_config = ConfigDict(frozen=True, strict=True)


def _annotation(node: dict, source: dict) -> Any:
    if "$ref" in node:
        uri = urljoin(source["$id"], node["$ref"])
        root, fragment = uri.split("#/$defs/")
        target = _BY_ID[root]
        version = next((v for v, fn in _FILES.items() if SCHEMAS.get(fn, {}).get("$id") == root), None)
        definition = target["$defs"][fragment]
        if definition.get("type") == "object":
            return model_for(fragment, version)
        return _annotation(definition, target)
    if "const" in node:
        return Literal[node["const"]]
    if "enum" in node:
        return Literal[tuple(node["enum"])]
    if "oneOf" in node or "anyOf" in node:
        variants = node.get("oneOf", node.get("anyOf"))
        return Union[tuple(_annotation(v, source) for v in variants)]
    kind = node.get("type")
    if kind == "integer":
        constraints = {key: node[src] for key, src in [("ge", "minimum"), ("le", "maximum")] if src in node}
        return Annotated[int, Field(**constraints)] if constraints else int
    if kind == "string":
        constraints = {key: node[src] for key, src in [("min_length", "minLength"), ("max_length", "maxLength"), ("pattern", "pattern")] if src in node}
        return Annotated[str, Field(**constraints)] if constraints else str
    if kind == "boolean":
        return bool
    if kind == "null":
        return type(None)
    if kind == "array":
        return list[_annotation(node.get("items", {}), source)]
    if kind == "object":
        # Inline objects stay JSON dictionaries; full schema validation above
        # enforces every property, conditional, and extra-field prohibition.
        return dict[str, Any]
    return Any


@lru_cache(maxsize=256)
def model_for(name: str, version: str = "2.0"):
    source = _source(name, version)
    schema = source["$defs"][name]
    if schema.get("type") == "object":
        required = set(schema.get("required", ()))
        fields = {}
        for key, prop in schema.get("properties", {}).items():
            annotation = _annotation(prop, source)
            fields[key] = (annotation, ... if key in required else None)
        model = create_model(name, __base__=ContractModel, __module__="novel_factory.contracts.models", **fields)
    else:
        model = type(name, (ContractValue,), {"__module__": "novel_factory.contracts.models"})
    model.contract_name = name
    model.contract_version = version
    return model
