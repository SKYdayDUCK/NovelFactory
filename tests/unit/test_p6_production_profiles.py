from copy import deepcopy

import pytest

from novel_factory.application.production_profiles import compile_dag, project_dag, resolve_profile
from novel_factory.contracts.registry import validate_contract
from novel_factory.domain.canonical import fingerprint
from novel_factory.domain.errors import DomainError
from novel_factory.infrastructure.runtime_catalog import RuntimeCatalog

PRESETS = ("short", "medium", "medium_long", "long", "ultra_long")


def test_scene_budgets_preserve_requested_book_bounds_after_replanning():
    from novel_factory.application.rolling_production import _scene_extents

    length = {"unit": "han_characters", "min": 11, "max": 17}
    def chapter(*ids):
        return {"scenes": [{"scene_ref": {"artifact_revision_id": ref}} for ref in ids]}

    first = _scene_extents(length, [chapter("a", "b")], 0, 2)
    assert first == {"a": {"min": 3, "max": 5}, "b": {"min": 3, "max": 4}}
    for replacement in (chapter("c"), chapter("x", "y", "z")):
        ranges = {**first, **_scene_extents(length, [replacement], 1, 2)}
        assert sum(scene["min"] for scene in ranges.values()) == length["min"]
        assert sum(scene["max"] for scene in ranges.values()) == length["max"]
    assert _scene_extents({"unit": "chapters"}, [chapter("c")], 1, 2) == {}


@pytest.mark.parametrize("preset", PRESETS)
def test_resolved_profile_has_closed_graph_and_registered_role_bindings(preset):
    profile = resolve_profile(preset, "现实主义")
    graph = compile_dag(profile)
    ids = set()
    catalog = RuntimeCatalog()
    for node in graph:
        assert node["id"] not in ids
        assert set(node["dependencies"] + node["conditional_dependencies"]) <= ids
        ids.add(node["id"])
        assert node["state"] == ("SKIPPED_BY_PROFILE" if node["decision"] == "SKIPPED_BY_PROFILE" else "WAITING")
        if node["role_id"]:
            snap = catalog.snapshot(node["role_id"], node["task_kind"], node["skill_name"])["snapshot"]
            assert snap["effective_model_tools"] == []
    assert {n["stage"] for n in graph} == {1, 2, 3}
    assert graph[-1]["dependencies"] == ["accept_text", "blind_review_a", "blind_review_b"]
    assert profile["independent_reviewers"] == 2
    assert validate_contract("ProductionProfile", profile, "production_profile") == profile

    # Reconstruct using only durable state on the same immutable profile.
    states = {n["id"]: "COMPLETED" for n in graph if n["decision"] in {"REQUIRED", "ENABLED"}}
    assert project_dag(deepcopy(profile), states)[-1]["state"] == "COMPLETED"
    assert compile_dag(profile) == graph


@pytest.mark.parametrize(("chapters", "expected"), [(99, "medium"), (100, "medium_long"), (500, "medium_long"),
                                                    (501, "long"), (2999, "long"), (3000, "ultra_long")])
def test_automatic_selection_and_explicit_length_precedence(chapters, expected):
    assert resolve_profile(None, "玄幻", complexity={"target_chapters": chapters})["preset"] == expected
    assert resolve_profile("short", "玄幻", complexity={"target_chapters": chapters})["preset"] == "short"


def test_complexity_and_explicit_overrides_change_planning_without_weakening_acceptance():
    p = resolve_profile("short", "现实主义")
    assert p["world_depth"] == "minimal"
    assert resolve_profile("auto", "灵异", complexity={"target_hanzi": 10000})["preset"] == "short"
    assert resolve_profile("auto", "灵异", complexity={"target_hanzi": 10001})["preset"] == "medium"
    richer = resolve_profile("short", "现实主义", complexity={"storylines": 3})
    assert richer["planning_depth"] == "chapters" and richer["world_depth"] == "detailed"
    heavy = resolve_profile("short", "奇幻")
    assert heavy["world_depth"] == "core"
    simplified = resolve_profile("short", "奇幻", {"world_depth": "minimal", "character_simulation": False})
    nodes = {n["id"]: n for n in compile_dag(simplified)}
    assert nodes["world_detail"]["decision"] == nodes["character_actions"]["decision"] == "SKIPPED_BY_PROFILE"
    assert set(nodes["world_acceptance"]["dependencies"]) == {"bind_plan"}
    for essential in ("world", "bind_plan", "scene_inputs", "reconcile", "knowledge", "accept_text", "release"):
        assert nodes[essential]["decision"] == "REQUIRED"


@pytest.mark.parametrize("invalid", [
    {"max_reworks": True}, {"window_chapters": 33}, {"writing_concurrency": 1.0}, {"future_volume_lookahead": 1},
    {"independent_reviewers": 1}, {"release_conditions": []}, {"role_id": "chief_editor"},
    {"tools": ["approve"]}, {"volume_planning": False, "cross_volume_memory": True}, {"review_concurrency": 0},
])
def test_profile_refuses_authority_changes_and_invalid_policy(invalid):
    with pytest.raises(DomainError):
        resolve_profile("short", "灵异", invalid)


def test_validation_on_load_refuses_missing_or_downgraded_immutable_fields():
    profile = resolve_profile("medium", "玄幻")
    before = fingerprint(profile)
    graph = compile_dag(profile)
    graph[0]["dependencies"].append("changed")
    assert fingerprint(profile) == before and compile_dag(profile)[0]["dependencies"] == []
    for key, value in (("release_conditions", []), ("independent_reviewers", 1), ("version", "unknown")):
        with pytest.raises(DomainError):
            compile_dag({**profile, key: value})


def test_failed_and_triggered_nodes_cannot_be_replayed_as_complete_or_skipped():
    p = resolve_profile("short", "灵异")
    graph = compile_dag(p)
    done = {n["id"]: "COMPLETED" for n in graph if n["decision"] in {"REQUIRED", "ENABLED"}}
    with pytest.raises(DomainError, match="predecessors"):
        project_dag(p, done, ["adjudication"])
    assert project_dag(p, {**done, "adjudication": "COMPLETED"}, ["adjudication"])[-1]["state"] == "COMPLETED"
    failed = {n["id"]: n["state"] for n in project_dag(p, {"world": "FAILED"})}
    assert failed["release"] == "BLOCKED" and failed["volume_plan"] == "SKIPPED_BY_PROFILE"
    with pytest.raises(DomainError, match="Inactive"):
        project_dag(p, {"volume_plan": "COMPLETED"})
    with pytest.raises(DomainError, match="optional"):
        project_dag(p, {}, ["volume_plan"])


def test_first_world_design_admissible_for_new_bound_output_and_legacy_type_still_valid():
    output = {"title": "例", "protagonist": "甲", "place": "乙地", "storyline": "甲作出选择",
              "world_rules": ["一项本地规则"], "chapter_goals": ["解决当章的问题"]}
    validate_contract("FirstWorldDesign", output, "production_profile")
    validate_contract("OutputContract", {"name": "FirstWorldDesign", "version": "production_profile"}, "agent")
    validate_contract("OutputContract", {"name": "AgentTextOutput", "version": "agent"}, "agent")
    for bad in ({"title": " "}, {"world_rules": []}, {"chapter_goals": ["一"] * 33}, {"provider": "surplus"}):
        with pytest.raises(DomainError):
            validate_contract("FirstWorldDesign", {**output, **bad}, "production_profile")


def test_open_ended_chapter_goal_accepts_larger_manuscripts():
    from novel_factory.application.idea_intake import _first_window_limit, parse_inspiration
    from novel_factory.application.production_extent import check_length

    length = {"preset": "medium_long", "unit": "chapters", "min": 100}
    idea = {"idea": "测试", "genre": "无限流", "length": length}
    profile = parse_inspiration(idea, "quality")
    assert _first_window_limit(profile, idea) == profile["window_chapters"]
    for count in (100, 101, 501):
        check_length(length, 999999, count)
    check_length(length, 999999, 99)
    with pytest.raises(DomainError):
        parse_inspiration({**idea, "length": {**length, "unit": "han_characters"}})


def test_review_diagnostic_only_transfers_known_misplaced_payload_keys():
    import json

    from novel_factory.application.idea_repair import structured_diagnostic
    raw = {"text": json.dumps({"summary": "", "issues": [], "untrusted_key": "private"})}
    diagnosis = structured_diagnostic(raw, {"reason": "INVALID_ARGUMENT"}, {"role_id": "reviewer"})
    assert diagnosis["move_inside_payload"] == ["issues", "summary"]
    assert "untrusted_key" not in str(diagnosis) and "private" not in str(diagnosis)
