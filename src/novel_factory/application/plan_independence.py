"""Prove and compose independent, unchanged-topology editorial node patches."""
from copy import deepcopy
from uuid import uuid4

from novel_factory.application.kernel import require


def _node_patch(base, candidate):
    def header(plan):
        ignored = {"nodes", "plan_revision_id", "decision", "prior_plan_ref", "input_world_ref"}
        return {k: v for k, v in plan.items() if k not in ignored}
    require(header(base) == header(candidate) and
            [n["node_id"] for n in base["nodes"]] == [n["node_id"] for n in candidate["nodes"]],
            "PLAN_SELECTION_CONFLICT", "Graph, contract or published prefix changed")
    patch = {}
    for old, new in zip(base["nodes"], candidate["nodes"], strict=True):
        if old == new:
            continue
        require(old["node_id"] not in base["published_prefix_node_ids"] and
                old["status"] not in {"REALIZED", "ABANDONED"} and
                {k: v for k, v in old.items() if k not in {"goal", "status"}} ==
                {k: v for k, v in new.items() if k not in {"goal", "status"}} and
                new["status"] in {"VIABLE", "PROPOSED"},
                "PLAN_SELECTION_CONFLICT", "Cannot commute structural, realized or resource changes")
        patch[old["node_id"]] = new
    return patch


def compose_independent(kernel, repo, world_ref, base_ref, candidate, selected_ref, *, window_ids=()):
    """Walk at most 32 revisions, including reverted edits. A new plan retains both parents in its receipt."""
    work = world_ref["work_id"]
    def plan(ref):
        return kernel.artifact(repo, ref, work, kind="PLAN_REVISION")["payload"]

    base = plan(base_ref)
    patch = _node_patch(base, candidate)
    require(bool(patch) and len(patch) <= 32, "PLAN_SELECTION_CONFLICT", "No bounded node patch")
    protected = set(patch) | set(window_ids)
    # A link, in either direction, is evidence of possible editorial influence.
    # Also protect common ancestors, subjects, declared conditions and shared event sources.
    for _ in range(32):
        adjacent = {n for e in base["dependencies"]
                    if protected.intersection({e["source_node_id"], e["target_node_id"]})
                    for n in (e["source_node_id"], e["target_node_id"])}
        if adjacent <= protected:
            break
        protected.update(adjacent)
    else:
        require(False, "PLAN_SELECTION_CONFLICT", "Dependency traversal budget exhausted")
    nodes = {n["node_id"]: n for n in base["nodes"]}
    def inputs(ids):
        result = set()
        for nid in ids:
            node = nodes[nid]
            result.update(node["subject_ids"])
            # Typed values may reference another entity, record or artifact
            # below expected_value. Include every nested provenance/resource ID.
            pending = [*node["hard_preconditions"], *node["fulfillment_event_refs"]]
            while pending:
                term = pending.pop()
                if isinstance(term, dict):
                    result.update(value for key, value in term.items()
                                  if (key.endswith("_id") or key == "sha256") and isinstance(value, str))
                    pending.extend(term.values())
                elif isinstance(term, list):
                    pending.extend(term)
        return result

    selected = plan(selected_ref)
    protected_inputs = inputs(protected)
    ref = selected_ref
    for _ in range(32):
        if ref == base_ref:
            break
        current = plan(ref)
        require(current["input_world_ref"] == world_ref and current["prior_plan_ref"],
                "PLAN_SELECTION_CONFLICT", "Selections do not share a frozen world and parent")
        parent = plan(current["prior_plan_ref"])
        changed = set(_node_patch(parent, current))
        require(not changed.intersection(protected) and not inputs(changed).intersection(protected_inputs),
                "PLAN_SELECTION_CONFLICT", "Window overlaps changed nodes or causal inputs")
        ref = current["prior_plan_ref"]
    else:
        require(False, "PLAN_SELECTION_CONFLICT", "Plan lineage traversal budget exhausted")
    result = deepcopy(selected)
    result.update(plan_revision_id=str(uuid4()), prior_plan_ref=selected_ref, input_world_ref=world_ref,
                  decision=deepcopy(candidate["decision"]),
                  nodes=[deepcopy(patch.get(n["node_id"], n)) for n in selected["nodes"]])
    return result
