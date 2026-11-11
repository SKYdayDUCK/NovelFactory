"""A finite, controller-authorized continuation of the same scene allowance."""

from uuid import uuid4

from novel_factory.application.kernel import require
from novel_factory.application.planning import PlanningService
from novel_factory.contracts.registry import validate_contract
from novel_factory.domain.canonical import fingerprint
from novel_factory.domain.errors import DomainError
from novel_factory.domain.planning import affected_nodes
from novel_factory.domain.scheduling import closure_at, state_from_revision
from novel_factory.domain.temporal import point_key


def validate_slots(slots, limit):
    require(
        isinstance(slots, list) and 1 <= len(slots) <= limit,
        "INVALID_ARGUMENT",
        "Future slots must fit the authorized rolling window",
    )
    for slot in slots:
        require(
            isinstance(slot, dict) and set(slot) == {"node_id", "process_id"},
            "INVALID_ARGUMENT",
            "Each future slot binds a plan node and a world process",
        )
        for field in ("node_id", "process_id"):
            validate_contract("UUID", slot[field])
    for field in ("node_id", "process_id"):
        require(
            len({item[field] for item in slots}) == len(slots),
            "INVALID_ARGUMENT",
            "A future slot cannot repeat a node or process",
        )


def _pointer(work, task_id):
    return {"work_id": work, "selection_kind": "SCENE_CONTINUATION:" + task_id}


def authorize_slots(kernel, repo, owner, director_id, request, plan_ref):
    work = request["world_ref"]["work_id"]
    plan = kernel.artifact(repo, plan_ref, work, kind="PLAN_REVISION")["payload"]
    ids = {node["node_id"] for node in plan["nodes"]}
    require(
        all(item["node_id"] in ids for item in request["following_scenes"]),
        "INVALID_ARGUMENT",
        "Future slots must name adopted plan nodes",
    )
    # The selected model route is immutable across the entire allowance.
    director_job = repo.one("supervisor_jobs", {"task_id": director_id})["config"]
    agent_input = kernel.artifact(
        repo, director_job["dispatch"]["input_ref"], work, kind="AGENT_INPUT"
    )["payload"]
    allowance = {
        k: v for k, v in request.items() if k not in {"world_ref", "story_time", "deadline_seconds", "allowed_item_gift"}
    }
    allowance["model_config_sha256"] = agent_input["model_config_sha256"]
    # The private authorization never becomes a role's task input.
    ref = kernel._blob(allowance)
    kernel._register_artifact(repo, ref, work, allowance, "SCENE_CONTINUATION", owner.principal_id)
    repo.add("selections", {**_pointer(work, director_id), "artifact_ref": ref, "epoch": 1})


def _start_next(kernel, repo, gateway, owner, task, spec, allowance):
    from novel_factory.application.scene_runtime import register_scene

    wr = spec["world_ref"]
    work, branch = wr["work_id"], wr["branch_id"]
    require(
        kernel.head(repo, work, branch) == wr,
        "HEAD_CONFLICT",
        "The next scene requires the accepted editorial world cut",
    )
    remaining = int((task["deadline"] - repo.now()).total_seconds())
    require(remaining > 0, "TASK_EXPIRED", "The scene allowance expired")
    adapter = gateway.adapters.get(allowance["adapter_id"])
    require(
        adapter and fingerprint(adapter.configuration) == allowance["model_config_sha256"],
        "MODEL_CONFIG_CHANGED",
        "The authorized model route changed",
    )
    selected = repo.one(
        "selections",
        {"work_id": work, "selection_kind": PlanningService.selection_key(allowance["volume_id"])},
    )
    plan = kernel.artifact(repo, selected["artifact_ref"], work, kind="PLAN_REVISION")["payload"]
    require(repo.ancestor(plan["input_world_ref"], wr), "STALE_PLAN", "Plan is on another history")
    slot, *rest = allowance["following_scenes"]
    node_id = slot["node_id"]
    nodes = {node["node_id"]: node for node in plan["nodes"]}
    node = nodes.get(node_id)
    require(
        node
        and node["status"] in {"PROPOSED", "VIABLE"}
        and node_id not in plan["published_prefix_node_ids"]
        and node["target_time"]["kind"] == "EXACT",
        "STALE_PLAN",
        "Future node is no longer executable",
    )
    at = node["target_time"]["at"]
    require(
        point_key(at) > point_key(spec["story_time"]),
        "STALE_PLAN",
        "Scene must lie after its review",
    )
    revision = kernel.revision(repo, wr)
    impact = affected_nodes(plan, revision["snapshot"], revision["selection"])
    require(node_id not in impact["affected"], "STALE_PLAN", "Scene preconditions require review")
    hard = {}
    for edge in plan["dependencies"]:
        if edge["kind"] == "HARD_PREREQUISITE":
            hard.setdefault(edge["target_node_id"], set()).add(edge["source_node_id"])
    predecessors = set()
    pending = [node_id]
    while pending:
        more = hard.get(pending.pop(), set()) - predecessors
        predecessors.update(more)
        pending.extend(more)
    for predecessor in predecessors:
        upstream = nodes[predecessor]
        evidence = upstream["fulfillment_event_refs"]
        require(
            upstream["status"] == "REALIZED"
            and evidence
            and all(
                revision["selection"].get(ref["occurrence_id"]) == ref["event_version_id"]
                for ref in evidence
            ),
            "STALE_PLAN",
            "Hard prerequisite lacks selected fulfillment evidence",
        )
    closure = closure_at(state_from_revision(revision), allowance["scope_ids"], at)
    require(
        closure["process_ids"] == [slot["process_id"]],
        "ADVANCE_BLOCKED",
        "Selected world trigger no longer matches this scene",
    )
    settings = {
        k: v
        for k, v in allowance.items()
        if k not in {"following_scenes", "idempotency_key", "model_config_sha256"}
    }
    result = register_scene(
        kernel,
        gateway,
        owner,
        **settings,
        world_ref=wr,
        story_time=at,
        following_scenes=rest or None,
        deadline_seconds=min(86400, remaining),
        idempotency_key=fingerprint(
            {"parent_task": task["task_id"], "generation": task["generation"], "slot": slot}
        ),
    )
    # No plan ref is put into a BELIEF input. Plan adoption fences these server-side
    # registrations across queue/lease generation changes, including a blocked actor.
    scopes = repo.scope_map(work, branch)
    task_ids = [
        *result["actor_tasks"].values(),
        result["control_task_id"],
        result["adjudicator_task_id"],
        result["director_task_id"],
    ]
    for affected in {node_id, *predecessors}:
        scope = f"ARTIFACT_SELECTION:PLAN_NODE:{plan['volume_id']}:{affected}"
        for task_id in task_ids:
            repo.add(
                "dependencies",
                {
                    "dependency_id": str(uuid4()),
                    "work_id": work,
                    "branch_id": branch,
                    "owner_type": "scene_task",
                    "owner_id": task_id,
                    "scope_key": scope,
                    "observed_version": scopes.get(scope, 0),
                    "dependency_kind": "ARTIFACT_SELECTION",
                },
            )
    return {
        "status": "STARTED",
        **slot,
        "plan_ref": selected["artifact_ref"],
        "world_ref": wr,
        "remaining_slots": len(rest),
        **result,
    }


def continue_after_review(kernel, repo, gateway, owner, task, spec, outcome):
    """Called under the editorial delivery lock after its native decision."""
    pointer = repo.one("selections", _pointer(task["work_id"], task["task_id"]))
    if pointer is None:
        return
    if child := outcome.get("successor_task_id"):
        repo.add(
            "selections",
            {
                **_pointer(task["work_id"], child),
                "artifact_ref": pointer["artifact_ref"],
                "epoch": 1,
            },
        )
        return
    if outcome.get("decision") not in {"NO_MAJOR_CHANGE", "APPROVED"}:
        outcome["continuation"] = {"status": "STOPPED", "reason": "EDITORIAL_DECISION"}
        return
    allowance = kernel.artifact(
        repo, pointer["artifact_ref"], task["work_id"], kind="SCENE_CONTINUATION"
    )["payload"]
    try:
        with repo.conn.begin_nested():
            outcome["continuation"] = _start_next(
                kernel, repo, gateway, owner, task, spec, allowance
            )
    except DomainError as error:
        # Keep the valid editorial decision. An invalidated allowance ends explicitly
        # without partial child grants, world changes, model calls, or a retry loop.
        outcome["continuation"] = {
            "status": "STOPPED",
            "reason": error.code,
            "node_id": allowance["following_scenes"][0]["node_id"],
        }
