"""Resolve finite production policy and compile its three-stage dependency graph.

The returned JSON is suitable for an immutable artifact. These functions do
not grant capabilities, select a provider, issue tasks or certify completion.
"""
from __future__ import annotations

from novel_factory.contracts.registry import schema_for, validate_contract
from novel_factory.domain.canonical import fingerprint
from novel_factory.domain.errors import DomainError

_COMMON = {
    "world_depth": "core",
    "planning_depth": "chapters",
    "volume_planning": False,
    "character_simulation": True,
    "environment_processes": False,
    "local_replanning": True,
    "future_volume_lookahead": 0,
    "cross_volume_memory": False,
    "writing_concurrency": 2,
    "review_concurrency": 2,
    "window_chapters": 8,
    "chapter_granularity": "chapter",
    "scene_granularity": "standard",
    "memory_scope": "chapter",
    "review_intensity": "chapter_and_work",
    "max_reworks": 2,
}

_VOLUME = {
    "world_depth": "detailed",
    "planning_depth": "volumes",
    "volume_planning": True,
    "environment_processes": True,
    "future_volume_lookahead": 1,
    "cross_volume_memory": True,
    "writing_concurrency": 4,
    "review_concurrency": 2,
    "window_chapters": 12,
    "chapter_granularity": "volume",
    "scene_granularity": "detailed",
    "memory_scope": "cross_volume",
    "review_intensity": "chapter_volume_and_work",
}
_PRESETS = {
    "short": {
        "world_depth": "minimal", "planning_depth": "storyline", "local_replanning": False,
        "writing_concurrency": 1, "review_concurrency": 1, "window_chapters": 4,
        "chapter_granularity": "story", "scene_granularity": "compact", "memory_scope": "work",
        "review_intensity": "whole_work", "max_reworks": 1,
    },
    "medium": {},
    "medium_long": _VOLUME,
    "long": {**_VOLUME, "world_depth": "layered", "planning_depth": "layered",
             "future_volume_lookahead": 2, "window_chapters": 16, "review_concurrency": 3},
    "ultra_long": {**_VOLUME, "world_depth": "layered", "planning_depth": "layered",
                   "future_volume_lookahead": 2, "window_chapters": 24, "review_concurrency": 4},
}
_GENRE_COMPLEXITY = {"玄幻", "奇幻", "无限流", "科幻", "fantasy", "xuanhuan", "science fiction", "infinite flow"}
_SCHEMA = "production_profile"


def _recommend(factors):
    chapters = factors.get("target_chapters", 0)
    if chapters >= 3000:
        return "ultra_long"
    if chapters >= 501:
        return "long"
    if chapters >= 100:
        return "medium_long"
    if "target_hanzi" in factors:
        return "short" if factors["target_hanzi"] <= 10000 else "medium"
    return "medium" if chapters > 1 else "short"


def _validate_settings(profile):
    checked = validate_contract("ProductionProfile", profile, _SCHEMA)
    fingerprint(checked)  # Reject noncanonical numbers before a profile becomes an artifact.
    if not checked["volume_planning"] and (
        checked["future_volume_lookahead"] or checked["cross_volume_memory"]
        or checked["planning_depth"] in {"volumes", "layered"}
        or checked["chapter_granularity"] == "volume"
        or checked["review_intensity"] == "chapter_volume_and_work"
    ):
        raise DomainError("INVALID_ARGUMENT", "Volume-level settings require volume planning")
    if checked["cross_volume_memory"] != (checked["memory_scope"] == "cross_volume"):
        raise DomainError("INVALID_ARGUMENT", "Memory scope must agree with cross-volume memory")
    return checked


def resolve_profile(preset, genre, overrides=None, complexity=None):
    """Resolve explicit settings over genre/complexity defaults; never accept role or tool overrides."""
    factors = validate_contract("ProductionComplexity", {} if complexity is None else complexity, _SCHEMA)
    settings = validate_contract("ProductionProfileOverrides", {} if overrides is None else overrides, _SCHEMA)
    if preset is None or preset == "auto":
        preset = _recommend(factors)
    if not isinstance(preset, str) or preset not in _PRESETS:
        raise DomainError("INVALID_ARGUMENT", "Unknown production preset")
    if not isinstance(genre, str) or not genre.strip() or len(genre) > 120:
        raise DomainError("INVALID_ARGUMENT", "A genre of at most 120 characters is required")
    genre = genre.strip()
    resolved = {**_COMMON, **_PRESETS[preset]}
    multi_line = factors.get("storylines", 1) >= 3 or factors.get("factions", 0) >= 3
    complex_world = (factors.get("characters", 1) >= 12 or factors.get("world_rules", 0) >= 6
                     or multi_line or genre.casefold() in _GENRE_COMPLEXITY)
    if complex_world and resolved["world_depth"] in {"minimal", "core"}:
        resolved["world_depth"] = "detailed" if multi_line else "core"
    if multi_line and resolved["planning_depth"] == "storyline":
        resolved.update(planning_depth="chapters", chapter_granularity="chapter", local_replanning=True)
    resolved.update(settings)
    # These settings describe the same policy from two callers' perspectives.
    if "cross_volume_memory" in settings and "memory_scope" not in settings:
        if settings["cross_volume_memory"]:
            resolved["memory_scope"] = "cross_volume"
        elif resolved["memory_scope"] == "cross_volume":
            resolved["memory_scope"] = "volume" if resolved["volume_planning"] else "chapter"
    if "memory_scope" in settings and "cross_volume_memory" not in settings:
        resolved["cross_volume_memory"] = settings["memory_scope"] == "cross_volume"
    required = schema_for("ProductionProfile", _SCHEMA)["$defs"]["ProductionProfile"]["properties"]
    profile = {"version": required["version"]["const"], "preset": preset, "genre": genre,
               "complexity": factors, **resolved, "independent_reviewers": required["independent_reviewers"]["const"],
               "release_conditions": required["release_conditions"]["const"]}
    return _validate_settings(profile)


def compile_dag(profile):
    """Compile a topologically ordered graph from validated settings, including explicit skips.

    dependencies contain active predecessor IDs only. OPTIONAL nodes carry the
    required trigger and are side branches; the native accept/release services
    independently fence unresolved reviews, world effects and pending repairs.
    """
    p = _validate_settings(profile)
    nodes = []
    active = set()
    conditional = set()

    def add(name, stage, parents=(), role=None, *, when=None, trigger=None, settings=()):
        if when is False:
            decision = "SKIPPED_BY_PROFILE"
        elif trigger:
            decision = "OPTIONAL"
        elif when is True:
            decision = "ENABLED"
        else:
            decision = "REQUIRED"
        role_id, task_kind, skill_name = role or (None, None, None)
        nodes.append({"id": name, "stage": stage, "decision": decision,
                      "state": "SKIPPED_BY_PROFILE" if when is False else "WAITING",
                      "dependencies": [parent for parent in parents if parent in active],
                      "conditional_dependencies": [parent for parent in parents if parent in conditional],
                      "role_id": role_id, "task_kind": task_kind, "skill_name": skill_name, "trigger": trigger,
                      "settings": {key: p[key] for key in settings}})
        if when is not False:
            (conditional if trigger else active).add(name)

    add("world", 1, role=("world_designer", "initial_world", "prepare-world"), settings=("world_depth",))
    add("world_detail", 1, ("world",), ("world_designer", "world_extension", "prepare-world"),
        when=p["world_depth"] != "minimal")
    add("world_rules_review", 1, ("world_detail",), ("world_designer", "rule_review", "prepare-world"),
        when=p["world_depth"] in {"detailed", "layered"})
    add("world_process_plan", 1, ("world_rules_review",), ("environment_director", "process_semantics", "prepare-world"),
        when=p["world_depth"] == "layered" and p["environment_processes"])
    add("characters", 1, ("world",), ("world_designer", "initial_world", "prepare-world"))
    add("storyline", 1, ("world", "world_detail", "world_rules_review", "world_process_plan", "characters"), ("chief_editor", "book_direction", "plan-volume"))
    add("volume_plan", 1, ("storyline",), ("volume_director", "volume_contract", "plan-volume"),
        when=p["volume_planning"])
    add("chapter_plan", 1, ("storyline", "volume_plan"), ("volume_director", "window_plan", "plan-volume"),
        when=p["planning_depth"] != "storyline", settings=("chapter_granularity", "planning_depth"))
    add("series_plan", 1, ("volume_plan",), ("chief_editor", "book_direction", "plan-volume"),
        when=p["planning_depth"] == "layered")
    add("bind_plan", 1, ("storyline", "volume_plan", "chapter_plan", "series_plan"))
    add("future_volumes", 1, ("bind_plan",), ("volume_director", "window_plan", "roll-window"),
        when=p["future_volume_lookahead"] > 0, settings=("future_volume_lookahead",))
    add("character_actions", 2, ("characters", "bind_plan"), ("character_actor", "character_action", "simulate-character"),
        when=p["character_simulation"])
    add("environment", 2, ("world", "bind_plan"), ("environment_director", "environment_change", "prepare-world"),
        when=p["environment_processes"])
    add("adjudication", 2, ("character_actions", "environment", "bind_plan"),
        ("adjudication_advisor", "joint_outcome", "adjudicate-actions"), trigger="unresolved_action_outcome")
    add("world_acceptance", 2, ("bind_plan", "character_actions", "environment", "adjudication"))
    add("replan", 2, ("world_acceptance",), ("volume_director", "replan", "roll-window"),
        when=p["local_replanning"], trigger="accepted_deviation")
    add("cross_volume_memory", 2, ("bind_plan",), ("evidence_maintainer", "literary_memory", "maintain-memory"),
        when=p["cross_volume_memory"])
    add("scene_inputs", 2, ("world_acceptance", "bind_plan", "cross_volume_memory", "replan"),
        ("volume_director", "scene_plan", "plan-volume"), settings=("chapter_granularity", "scene_granularity"))
    add("draft", 2, ("scene_inputs",), ("writer", "scene_draft", "write-scene"),
        settings=("writing_concurrency", "window_chapters", "scene_granularity"))
    add("handoff", 2, ("draft",), ("evidence_maintainer", "summary", "maintain-memory"),
        settings=("memory_scope",))
    add("volume_handoff", 2, ("handoff", "future_volumes"),
        ("literary_editor", "cross_volume", "maintain-memory"), when=p["cross_volume_memory"])
    add("reconcile", 3, ("world_acceptance", "draft"), ("reviewer", "fact", "review-consistency"))
    add("knowledge", 3, ("draft",), ("reviewer", "epistemic", "review-consistency"))
    add("continuity", 3, ("handoff", "volume_handoff", "reconcile", "knowledge"), ("reviewer", "continuity", "review-consistency"))
    add("chapter_review", 3, ("draft",), ("reviewer", "literary", "review-consistency"),
        when=p["review_intensity"] != "whole_work", settings=("review_intensity", "review_concurrency"))
    add("volume_review", 3, ("chapter_review", "continuity"), ("volume_director", "volume_review", "volume-director-review"),
        when=p["review_intensity"] == "chapter_volume_and_work")
    add("literary_edit", 3, ("continuity", "chapter_review", "volume_review"), ("literary_editor", "structure", "edit-volume"))
    add("revise", 3, ("literary_edit",), ("writer", "targeted_revision", "write-scene"),
        when=p["max_reworks"] > 0, trigger="revision_requested", settings=("max_reworks",))
    add("accept_text", 3, ("reconcile", "knowledge", "literary_edit", "revise"))
    for judge in ("blind_review_a", "blind_review_b"):
        add(judge, 3, ("accept_text",), ("reviewer", "literary", "review-consistency"))
    add("release", 3, ("accept_text", "blind_review_a", "blind_review_b"), settings=("release_conditions",))
    return nodes


def project_dag(profile, execution_states, triggered=()):
    """Read-only replay of states observed by the trusted controller on one input version.

    A triggered optional step blocks its consumers until completed. Revised
    texts use a fresh version's receipts, never completion of the old graph.
    No state in this projection can be used as a release/acceptance receipt.
    """
    nodes = compile_dag(profile)
    by_id = {node["id"]: node for node in nodes}
    if not isinstance(execution_states, dict) or not execution_states.keys() <= by_id.keys():
        raise DomainError("INVALID_ARGUMENT", "Unknown production node in execution state")
    if not isinstance(triggered, (tuple, list, set)) or not all(
        isinstance(key, str) and key in by_id and by_id[key]["decision"] == "OPTIONAL" for key in triggered
    ):
        raise DomainError("INVALID_ARGUMENT", "Only configured optional nodes can be triggered")
    triggers = set(triggered)
    for node in nodes:
        key = node["id"]
        observed = execution_states.get(key)
        if key in execution_states and observed not in ("WAITING", "BLOCKED", "COMPLETED", "FAILED"):
            raise DomainError("INVALID_ARGUMENT", "Unknown observed production state")
        if node["decision"] == "SKIPPED_BY_PROFILE" or (node["decision"] == "OPTIONAL" and key not in triggers):
            if key in execution_states:
                raise DomainError("INVALID_ARGUMENT", "Inactive node has an execution state")
            continue
        prerequisites = node["dependencies"] + [parent for parent in node["conditional_dependencies"] if parent in triggers]
        states = [by_id[parent]["state"] for parent in prerequisites]
        if observed == "COMPLETED" and any(state != "COMPLETED" for state in states):
            raise DomainError("INVALID_ARGUMENT", "Node completed without required predecessors")
        if any(state in {"BLOCKED", "FAILED"} for state in states) and observed in (None, "WAITING"):
            node["state"] = "BLOCKED"
        elif observed is not None:
            node["state"] = observed
    return nodes


def load_profile(kernel, repo, owner, work, branch):
    """Read the two pinned artifacts under the current branch administrator's grant."""
    from .kernel import require
    from .volume_successor import _selected

    kernel.authorize(repo, owner, work, "ADMIN", branch)
    kernel.head(repo, work, branch)
    profile_ref = _selected(repo, work, "P6_PROFILE:" + branch)
    dag_ref = _selected(repo, work, "P6_DAG:" + branch)
    if profile_ref is None and dag_ref is None:
        return None
    require(profile_ref and dag_ref, "PROJECTION_CORRUPT", "Incomplete production profile binding")
    profile = kernel.artifact(repo, profile_ref, work, kind="PRODUCTION_PROFILE")["payload"]
    graph = kernel.artifact(repo, dag_ref, work, kind="PRODUCTION_DAG")["payload"]
    require(fingerprint(profile) == profile_ref["sha256"] and fingerprint(graph) == dag_ref["sha256"]
            and graph["profile_ref"] == profile_ref, "PROJECTION_CORRUPT", "Inconsistent production profile binding")
    return {"profile_ref": profile_ref, "dag_ref": dag_ref, "profile": profile, "nodes": graph["nodes"]}


def bind_profile(kernel, repo, owner, world_ref, profile):
    """Pin both references atomically in the caller's transaction; exact retries return the same IDs."""
    from .kernel import require
    from .planning import PlanningService
    from .production_pipeline import _artifact

    validate_contract("WorldRef", world_ref)
    profile = _validate_settings(profile)
    work, branch = world_ref["work_id"], world_ref["branch_id"]
    repo.one("works", {"work_id": work}, lock="update")
    previous = load_profile(kernel, repo, owner, work, branch)
    require(kernel.head(repo, work, branch) == world_ref, "HEAD_CONFLICT", "Production profile requires the current world")
    if previous:
        require(previous["profile"] == profile, "TASK_REBIND_REQUIRED", "Branch already has a different production profile")
        return {"profile_ref": previous["profile_ref"], "dag_ref": previous["dag_ref"]}
    profile_ref = _artifact(kernel, repo, work, owner, "PRODUCTION_PROFILE", profile)
    graph = {"profile_ref": profile_ref, "nodes": compile_dag(profile)}
    dag_ref = _artifact(kernel, repo, work, owner, "PRODUCTION_DAG", graph)
    plans = PlanningService(kernel)
    plans._select(repo, world_ref, "P6_PROFILE:" + branch, profile_ref)
    plans._select(repo, world_ref, "P6_DAG:" + branch, dag_ref)
    return {"profile_ref": profile_ref, "dag_ref": dag_ref}
