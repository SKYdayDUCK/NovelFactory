"""Frozen intake-window constraints used by role prompts, structural diagnostics and replay."""

import json

from novel_factory.domain.canonical import canonical_bytes
from .kernel import require
from .prompt_schema import prompt_schema


_DESIGN_CONTRACTS = {"FirstWorldDesign", "OpeningWorldDesign", "EnvironmentalOpeningWorldDesign", "ShortEndingPlan"}


def window_diagnostics(goals, maximum):
    findings = []
    if len(goals) > maximum:
        findings.append({"path": "/chapter_goals", "keyword": "maxItems", "limit": maximum, "actual": len(goals)})
    seen = {}
    duplicates = []
    for index, goal in enumerate(goals):
        if goal in seen:
            duplicates.append([seen[goal], index])
        else:
            seen[goal] = index
    if duplicates:
        findings.append({"path": "/chapter_goals", "keyword": "uniqueItems", "duplicates": duplicates})
    return findings


def design_prompt_schema(data):
    output = data["output_contract"]
    schema = prompt_schema(output["name"], output["version"])
    if (output["version"] != "production_profile" or output["name"] not in _DESIGN_CONTRACTS
            or (data["role_id"], data["task_kind"]) != ("world_designer", "initial_world")):
        return schema
    try:
        assignment = json.loads(data["assignment"])
    except (TypeError, ValueError):
        return schema
    if not isinstance(assignment, dict) or assignment.get("window_constraints_version") != 1:
        return schema  # Older frozen inputs retain their original prompt and request hash.
    maximum = assignment.get("first_window_limit")
    require(type(maximum) is int and 1 <= maximum <= 32, "INVALID_ARGUMENT", "Invalid frozen intake window")
    document = json.loads(schema)

    def specialize(value):
        if isinstance(value, list):
            for child in value:
                specialize(child)
        elif isinstance(value, dict):
            field = value.get("properties", {}).get("chapter_goals")
            if field is not None:
                field["maxItems"] = min(maximum, field.get("maxItems", maximum))
                field["uniqueItems"] = True
            for child in value.values():
                specialize(child)

    specialize(document)
    return canonical_bytes(document).decode()
