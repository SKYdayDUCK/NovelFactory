"""Structured editorial judgments and a separately authorized, immutable actor audit."""
from sqlalchemy import text

from novel_factory.application.editorial_effects import review_effects
from novel_factory.application.kernel import require
from novel_factory.contracts.registry import validate_contract


def unknown_assessment():
    return {"reference_node_ids": [], "affected_node_ids": [], "characters": [],
            "trajectory": {"disposition": "UNDETERMINED", "return_direction": None,
                           "alternative_direction": None, "rationale": "本次审核尚无足够材料判断回归方向。"}}


def validate_assessment(kernel, repo, spec, binding, original_ref, prior, window, events, assessment):
    review = validate_contract("DeviationAssessment", assessment, "agent")
    work = spec["world_ref"]["work_id"]
    original = kernel.artifact(repo, original_ref, work, kind="PLAN_REVISION")["payload"]
    reference_ids = {node["node_id"] for node in original["nodes"]}
    # Only expose/check original nodes that are also visible in the pinned plan.
    current_ids = {node["node_id"] for node in prior["nodes"]}
    require(set(review["reference_node_ids"]) <= reference_ids & current_ids
            and set(review["affected_node_ids"]) <= set(window), "INVALID_EVIDENCE",
            "Review nodes exceed the authorized plan materials or window")
    context = repo.one("contexts", {"task_id": binding["task_id"], "generation": binding["generation"]})
    compiled = kernel.artifact(repo, context["artifact_ref"], work, kind="COMPILED_CONTEXT")["payload"]
    manifest_ref = compiled["input_manifest_ref"]
    require(manifest_ref in spec["input_artifacts"] and manifest_ref in context["read_cut"]["source_manifest_refs"],
            "VIEW_DENIED", "The review requires its task-bound input manifest")
    subjects = kernel.artifact(repo, manifest_ref, work, kind="INPUT_MANIFEST")["payload"]["subject_ids"]
    entities = kernel.revision(repo, spec["world_ref"])["snapshot"]["entities"]
    public_events = []
    if any(c["plausibility"] != "INSUFFICIENT_EVIDENCE" for c in review["characters"]):
        task = {**binding, "work_id": work, "branch_id": spec["world_ref"]["branch_id"]}
        applied = review_effects(kernel, repo, task, spec, manifest_ref, context["read_cut"])
        public_events = [entry["event_ref"] for entry in applied["accepted_effects"] if entry["effects"]]
    actors = set()
    for character in review["characters"]:
        actor_id = character["actor_id"]
        require(actor_id in subjects and actor_id not in actors
                and entities.get(actor_id, {}).get("entity_kind") in {"CHARACTER", "ORGANIZATION"}
                and all(ref in events for ref in character["basis_event_refs"]),
                "INVALID_EVIDENCE", "Character assessment exceeds this review's authorized material")
        require(character["plausibility"] == "INSUFFICIENT_EVIDENCE" or
                any(ref in public_events for ref in character["basis_event_refs"]),
                "INVALID_EVIDENCE", "An affirmative character judgment needs accepted evidence")
        actors.add(actor_id)
    trajectory = review["trajectory"]
    if trajectory["disposition"] != "UNDETERMINED":
        field = "return_direction" if trajectory["disposition"] == "POSSIBLE_RETURN" else "alternative_direction"
        require(review["reference_node_ids"] and review["affected_node_ids"] and trajectory[field]
                and trajectory[field].strip(), "INVALID_EVIDENCE", "A trajectory decision needs nodes and a direction")
    if trajectory["disposition"] == "UNDETERMINED":
        require(trajectory["return_direction"] is None and trajectory["alternative_direction"] is None,
                "INVALID_EVIDENCE", "An undetermined trajectory must leave directions open")
    require(trajectory["disposition"] != "DIVERGENCE" or trajectory["return_direction"] is None,
            "INVALID_EVIDENCE", "A divergent trajectory cannot designate a return path")
    return review


def action_time_audit(kernel, principal, world_ref, change_point_ref):
    """Resolve the accepted interaction's original sealed inputs for a branch ADMIN.

    This control-plane query is deliberately absent from role MCP/Host packets.
    Only immutable actor-time references are returned. Roles cannot enumerate
    other participants or their context/intent IDs through the ChangePoint.
    """
    work, branch = world_ref["work_id"], world_ref["branch_id"]
    with kernel.db.uow(write=False) as repo:
        kernel.authorize(repo, principal, work, "ADMIN", branch)
        cp = kernel.artifact(repo, change_point_ref, work, kind="CHANGE_POINT")["payload"]
        require(cp["input_world_ref"] == world_ref, "VIEW_DENIED", "ChangePoint outside the bound branch/cut")
        joint = repo.conn.execute(text("""
            SELECT p.template_args->>'interaction_id' AS interaction_id, r.payload AS receipt
            FROM nf_receipts r JOIN nf_proposals p USING(proposal_id, work_id, branch_id)
            WHERE r.work_id=:work AND r.branch_id=:branch AND r.command_type='ACCEPT'
              AND p.template_id='JOINT_INTERACTION' AND r.payload->'world_ref'->>'world_revision_id'=:rev
        """), {"work": work, "branch": branch, "rev": world_ref["world_revision_id"]}).mappings().one_or_none()
        require(joint and all(ref in joint["receipt"]["accepted_event_refs"] for ref in cp["event_refs"])
                and cp.get("acceptance_command_id", joint["receipt"]["command_id"]) == joint["receipt"]["command_id"],
                "INVALID_EVIDENCE", "ChangePoint lacks the exact joint acceptance")
        actors = []
        for sealed in repo.many("simulation_intents", {"interaction_id": joint["interaction_id"]}, order=["actor_id"]):
            context = repo.one("contexts", {"work_id": work, "task_id": sealed["task_id"],
                "generation": sealed["generation"], "grant_id": sealed["grant_id"],
                "context_bundle_id": sealed["payload"]["context_bundle_id"]})
            require(context, "INVALID_EVIDENCE", "Missing historical actor evidence")
            cut = context["read_cut"]["knowledge_cut"]
            require(cut["plane"] == "BELIEF" and cut["actor_id"] == sealed["actor_id"]
                    and context["payload"]["world_ref"] == sealed["payload"]["world_ref"],
                    "INVALID_EVIDENCE", "Historical actor cut does not match the sealed action")
            actors.append({"actor_id": sealed["actor_id"], "story_time": context["payload"]["story_time"],
                           "known_at": cut["known_at"], "world_ref": sealed["payload"]["world_ref"],
                           "context_ref": context["artifact_ref"], "intent_ref": sealed["artifact_ref"]})
        return {"acceptance_command_id": joint["receipt"]["command_id"], "actors": actors}
