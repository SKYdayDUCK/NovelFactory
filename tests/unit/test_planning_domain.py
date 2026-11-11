"""P2-01/P2-04 typed dependencies preserve facts and creative alternatives."""
from copy import deepcopy
from uuid import uuid4

import pytest

from novel_factory.contracts.registry import validate_contract
from novel_factory.domain.errors import DomainError
from novel_factory.domain.planning import affected_nodes, condition_result, future_manifest, replan_nodes, validate_graph
from novel_factory.domain.reducer import replay
from novel_factory.fixtures import fixture_definition, point


def node(subject, *, node_id=None, conditions=None):
    return {"node_id": node_id or str(uuid4()), "goal": "通过既有路径救济南岸", "status": "PROPOSED",
            "target_time": {"kind": "EXACT", "at": point(50)}, "subject_ids": [subject],
            "hard_preconditions": conditions or [], "risk_notes": [], "failure_exit": "保留真实损失，另找合法路径",
            "fulfillment_event_refs": []}


def test_hard_failure_propagates_but_soft_edges_only_request_review():
    fixture = fixture_definition("r1_sword")
    snapshot = replay({**fixture["genesis"], "relations": []}, [])
    sword = fixture["ids"]["sword"]
    nodes = [node(sword, conditions=[{"kind": "EVENT_SELECTED", "event_ref": {"occurrence_id": str(uuid4()), "event_version_id": str(uuid4())}}])]
    nodes += [node(sword) for _ in range(4)]
    plan = {"nodes": nodes, "published_prefix_node_ids": [], "decision": {"considered_options": [{"option_id": str(uuid4()), "disposition": "SELECTED"}]},
            "dependencies": [{"source_node_id": nodes[0]["node_id"], "target_node_id": nodes[i]["node_id"], "kind": kind}
                             for i, kind in [(1, "HARD_PREREQUISITE"), (2, "ALTERNATIVE"), (3, "LITERARY_PROMISE")]]}
    result = affected_nodes(plan, snapshot, {})
    assert set(result["blocked"]) == {n["node_id"] for n in nodes[:2]}
    assert set(result["review"]) == {n["node_id"] for n in nodes[2:4]}
    assert nodes[4]["node_id"] not in result["affected"]
    changed = replan_nodes(plan, result, [])
    assert changed["nodes"][4] == nodes[4]
    assert changed["nodes"][2]["status"] == "REVIEW_REQUIRED"
    assert plan["nodes"][0]["status"] == "PROPOSED"


def test_unknown_value_is_not_a_failed_prerequisite():
    fixture = fixture_definition("r1_sword")
    snapshot = replay({**fixture["genesis"], "relations": []}, [])
    condition = {"kind": "STATE_EQUALS", "subject_id": str(uuid4()), "predicate": "owner",
                 "expected_value": {"kind": "ENTITY", "entity_id": fixture["ids"]["smith"]}}
    assert condition_result(condition, snapshot, {}) == "UNRESOLVED"


def test_hard_cycles_and_unrelated_replacements_rejected():
    nodes = [node(str(uuid4())) for _ in range(2)]
    plan = {"nodes": nodes, "published_prefix_node_ids": [], "decision": {"considered_options": [{"option_id": str(uuid4()), "disposition": "SELECTED"}]},
            "dependencies": [{"source_node_id": nodes[i]["node_id"], "target_node_id": nodes[1-i]["node_id"], "kind": "HARD_PREREQUISITE"} for i in range(2)]}
    with pytest.raises(DomainError) as cycle:
        validate_graph(plan)
    assert cycle.value.code == "PLAN_CYCLE"
    plan["dependencies"] = []
    impact = {"affected": [nodes[0]["node_id"]], "blocked": [nodes[0]["node_id"]], "review": []}
    with pytest.raises(DomainError) as unrelated:
        replan_nodes(plan, impact, [nodes[1]])
    assert unrelated.value.code == "UNRELATED_REPLAN"
    plan["published_prefix_node_ids"] = [nodes[0]["node_id"]]
    with pytest.raises(DomainError) as prefix:
        replan_nodes(plan, impact, [])
    assert prefix.value.code == "PUBLISHED_PREFIX_CONFLICT"


def test_future_manifest_does_not_claim_realized_events_are_plans():
    ref = {"artifact_id": str(uuid4()), "artifact_revision_id": str(uuid4()), "sha256": "a" * 64}
    future = node(str(uuid4()))
    past = deepcopy(future)
    past.update(node_id=str(uuid4()), status="REALIZED", fulfillment_event_refs=[{"occurrence_id": str(uuid4()), "event_version_id": str(uuid4())}])
    result = future_manifest({"nodes": [future, past]}, ref)
    validate_contract("PlanManifest", result, "kernel")
    assert len(result["plans"]) == 1 and result["plans"][0]["plan_id"] == future["node_id"]
    assert result["plans"][0]["fulfillment_event_refs"] == []


def test_plan_schema_rejects_fake_realization_and_extra_effects():
    proposed = node(str(uuid4()))
    validate_contract("PlanNode", proposed, "planning")
    realized = {**proposed, "status": "REALIZED"}
    with pytest.raises(DomainError):
        validate_contract("PlanNode", realized, "planning")
    with pytest.raises(DomainError):
        validate_contract("PlanNode", {**proposed, "effects": []}, "planning")
