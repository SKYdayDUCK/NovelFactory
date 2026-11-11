"""Conditional planning over an explicit immutable world projection.

The functions here never apply effects. VIABLE means that the stated
prerequisites hold at the input cut, not that a proposed future has occurred.
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal

from novel_factory.domain.errors import DomainError
from novel_factory.domain.reducer import lifecycle_at, resource_at, state_at


def _require(condition, code, message):
    if not condition:
        raise DomainError(code, message)


def validate_graph(plan):
    nodes = {node["node_id"]: node for node in plan["nodes"]}
    _require(len(nodes) == len(plan["nodes"]), "INVALID_ARGUMENT", "Plan node identities repeat")
    _require(set(plan["published_prefix_node_ids"]) <= nodes.keys(), "INVALID_ARGUMENT",
             "Published prefix references an unknown plan node")
    successors = {key: [] for key in nodes}
    seen = set()
    for edge in plan["dependencies"]:
        source, target = edge["source_node_id"], edge["target_node_id"]
        _require(source in nodes and target in nodes and source != target, "INVALID_ARGUMENT",
                 "Plan dependency has an invalid endpoint")
        identity = (source, target, edge["kind"])
        _require(identity not in seen, "INVALID_ARGUMENT", "Plan dependency repeats")
        seen.add(identity)
        if edge["kind"] == "HARD_PREREQUISITE":
            successors[source].append(target)
    visiting, visited = set(), set()

    def visit(node):
        _require(node not in visiting, "PLAN_CYCLE", "Hard plan prerequisites contain a cycle")
        if node in visited:
            return
        visiting.add(node)
        for target in successors[node]:
            visit(target)
        visiting.remove(node)
        visited.add(node)

    for node in nodes:
        visit(node)
    selected = [o for o in plan["decision"]["considered_options"] if o["disposition"] == "SELECTED"]
    _require(len(selected) == 1, "INVALID_ARGUMENT", "A planning decision selects exactly one option")
    option_ids = [o["option_id"] for o in plan["decision"]["considered_options"]]
    _require(len(option_ids) == len(set(option_ids)), "INVALID_ARGUMENT", "Decision option identities repeat")


def condition_result(condition, snapshot, selection):
    """SATISFIED / BROKEN / UNRESOLVED at the supplied accepted cut.

    No absence of a value is interpreted as false. Resource/relationship
    conditions describe current feasibility, never guaranteed future outcomes.
    """
    kind = condition["kind"]
    if kind == "ENTITY_ACTIVE":
        state = lifecycle_at(snapshot, condition["subject_id"])
        if state is None:
            return "UNRESOLVED"
        return "SATISFIED" if state == "ACTIVE" else "BROKEN"
    if kind == "STATE_EQUALS":
        answer = state_at(snapshot, condition["subject_id"], condition["predicate"])
        if answer["value_status"] == "UNRESOLVED":
            return "UNRESOLVED"
        return "SATISFIED" if answer["value_status"] == "VALUE" and answer["data"] == condition["expected_value"] else "BROKEN"
    if kind == "RESOURCE_AT_LEAST":
        if condition["account_id"] not in snapshot["entities"] or condition["resource_id"] not in snapshot["entities"]:
            return "UNRESOLVED"
        balance = resource_at(snapshot, condition["account_id"], condition["resource_id"], condition["unit"])
        return "SATISFIED" if Decimal(balance["quantity"]) >= Decimal(condition["quantity"]) else "BROKEN"
    if kind == "EVENT_SELECTED":
        event = condition["event_ref"]
        return "SATISFIED" if selection.get(event["occurrence_id"]) == event["event_version_id"] else "BROKEN"
    raise DomainError("INVALID_ARGUMENT", "Unknown planning precondition")


def affected_nodes(plan, snapshot, selection):
    """Propagate hard failures, while soft/alternative edges request review."""
    validate_graph(plan)
    broken, review = set(), set()
    for node in plan["nodes"]:
        if node["status"] in {"REALIZED", "ABANDONED"}:
            continue
        results = [condition_result(c, snapshot, selection) for c in node["hard_preconditions"]]
        if "BROKEN" in results:
            broken.add(node["node_id"])
        elif "UNRESOLVED" in results:
            review.add(node["node_id"])
    direct = set(broken)
    changed = True
    while changed:
        before = (len(broken), len(review))
        for edge in plan["dependencies"]:
            source, target = edge["source_node_id"], edge["target_node_id"]
            if source in broken:
                (broken if edge["kind"] == "HARD_PREREQUISITE" else review).add(target)
            elif source in review:
                review.add(target)
        changed = before != (len(broken), len(review))
    review -= broken
    return {"direct": sorted(direct), "blocked": sorted(broken), "review": sorted(review),
            "affected": sorted(broken | review)}


def replan_nodes(plan, impact, replacements, additional_nodes=(), additional_dependencies=()):
    """Preserve unaffected nodes and all recorded outcomes; only future changes."""
    nodes = {node["node_id"]: deepcopy(node) for node in plan["nodes"]}
    replacements = list(replacements)
    ids = [node["node_id"] for node in replacements]
    _require(len(ids) == len(set(ids)), "INVALID_ARGUMENT", "Replan replacement identities repeat")
    allowed = set(impact["affected"])
    protected = set(plan["published_prefix_node_ids"]) | {
        node["node_id"] for node in plan["nodes"] if node["status"] == "REALIZED"
    }
    _require(not allowed.intersection(protected), "PUBLISHED_PREFIX_CONFLICT",
             "Replanning cannot rewrite accepted outcomes or the published prefix")
    _require(set(ids) <= allowed, "UNRELATED_REPLAN", "Replacement is outside the affected dependency closure")
    for key in impact["blocked"]:
        nodes[key]["status"] = "BLOCKED"
    for key in impact["review"]:
        nodes[key]["status"] = "REVIEW_REQUIRED"
    for node in replacements:
        _require(node["status"] != "REALIZED", "INVALID_ARGUMENT",
                 "Replanning alternatives cannot manufacture realized outcomes")
        nodes[node["node_id"]] = deepcopy(node)
    for node in additional_nodes:
        _require(node["node_id"] not in nodes and node["status"] != "REALIZED", "INVALID_ARGUMENT",
                 "An alternative must have a fresh identity and remain a future plan")
        nodes[node["node_id"]] = deepcopy(node)
    result = deepcopy(plan)
    result["nodes"] = list(nodes.values())
    result["dependencies"] += deepcopy(list(additional_dependencies))
    # Alternatives must connect to this impact, not silently extend the whole book.
    new_ids = {node["node_id"] for node in additional_nodes}
    reachable = set(allowed)
    while True:
        old = len(reachable)
        for edge in additional_dependencies:
            if edge["source_node_id"] in reachable:
                reachable.add(edge["target_node_id"])
        if old == len(reachable):
            break
    _require(new_ids <= reachable, "UNRELATED_REPLAN", "New alternatives must depend on the affected plan region")
    validate_graph(result)
    return result


def future_manifest(plan, plan_ref):
    """Adapt P2 plans to the existing P1 explicit PLAN query cut.

    Realized nodes are stored with accepted EventRefs in PlanRevision; they are
    absent from this *future* manifest, whose old schema intentionally forbids
    certifying fulfillment. This retains the unchanged 2.0/2.1 read contracts.
    """
    statuses = {"PROPOSED": "DRAFT", "VIABLE": "READY", "BLOCKED": "INVALIDATED",
                "REVIEW_REQUIRED": "BLOCKED", "ABANDONED": "CANCELLED"}
    return {"schema_version": "p1", "plans": [
        {"plan_id": node["node_id"], "plan_ref": plan_ref, "status": statuses[node["status"]],
         "target_time": node["target_time"], "subject_ids": node["subject_ids"], "summary": node["goal"],
         "requires_active": [c["subject_id"] for c in node["hard_preconditions"] if c["kind"] == "ENTITY_ACTIVE"],
         "fulfillment_event_refs": []}
        for node in plan["nodes"] if node["status"] != "REALIZED"
    ]}
