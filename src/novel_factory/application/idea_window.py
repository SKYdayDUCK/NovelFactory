"""Frozen intake-window constraints used by role prompts, structural diagnostics and replay."""

import json

from novel_factory.domain.canonical import canonical_bytes
from .kernel import require
from .prompt_schema import prompt_schema


_DESIGN_CONTRACTS = {"FirstWorldDesign", "OpeningWorldDesign", "EnvironmentalOpeningWorldDesign", "ShortEndingPlan"}


# Each chapter in a native intake plan currently materializes one writer scene.
# This planning target leaves headroom for a Chinese scene in the production
# writer's 8192-token structured response. It is not a tokenizer conversion.
_HAN_PER_PLANNED_SCENE = 5000


def initial_window_minimum(profile, length, maximum):
    if (profile.get("production_mode") != "quality" or not isinstance(length, dict)
            or length["unit"] != "han_characters"):
        return 1
    needed = (length["min"] + _HAN_PER_PLANNED_SCENE - 1) // _HAN_PER_PLANNED_SCENE
    return min(needed, maximum)


def frozen_window_minimum(assignment, maximum):
    if assignment.get("window_constraints_version") != 2:
        return 1
    minimum = assignment.get("first_window_minimum")
    require(type(minimum) is int and 1 <= minimum <= maximum <= 32,
            "INVALID_EVIDENCE", "Invalid frozen intake chapter minimum")
    return minimum


def window_diagnostics(goals, maximum, minimum=1):
    findings = []
    if len(goals) < minimum:
        findings.append({"path": "/chapter_goals", "keyword": "minItems", "limit": minimum, "actual": len(goals)})
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
    if not isinstance(assignment, dict) or assignment.get("window_constraints_version") not in (1, 2):
        return schema  # Older frozen inputs retain their original prompt and request hash.
    maximum = assignment.get("first_window_limit")
    require(type(maximum) is int and 1 <= maximum <= 32, "INVALID_ARGUMENT", "Invalid frozen intake window")
    minimum = frozen_window_minimum(assignment, maximum)
    document = json.loads(schema)

    def specialize(value):
        if isinstance(value, list):
            for child in value:
                specialize(child)
        elif isinstance(value, dict):
            field = value.get("properties", {}).get("chapter_goals")
            if field is not None:
                if minimum > 1:
                    field["minItems"] = max(minimum, field.get("minItems", 1))
                field["maxItems"] = min(maximum, field.get("maxItems", maximum))
                field["uniqueItems"] = True
            for child in value.values():
                specialize(child)

    specialize(document)
    return canonical_bytes(document).decode()
