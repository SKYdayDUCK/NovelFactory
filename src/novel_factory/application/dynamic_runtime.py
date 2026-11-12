"""Apply a provisioned assignment to one NF task and one live Workshop delegation."""
from hashlib import sha256
import json
from uuid import UUID, uuid5

from sqlalchemy import select, text

from novel_factory.application.kernel import require


def matching_inputs(repo, task, spec):
    inputs = repo.many("artifacts", {"work_id": task["work_id"], "kind": "AGENT_INPUT",
                       "artifact_revision_id": [r["artifact_revision_id"] for r in spec["input_artifacts"]]})
    return [row["payload"] for row in inputs if
            (row["payload"]["role_id"], row["payload"]["task_kind"], row["payload"]["runtime_sha256"])
            == (task["role_id"], task["task_kind"], spec["runtime_contract_version"])]


def assignment_for(repo, task, spec, *, selected_input=None):
    return assignment_from_inputs(matching_inputs(repo, task, spec), selected_input=selected_input)


def assignment_from_inputs(compatible, *, selected_input=None):
    """One runtime generation cannot mix a static input and a dynamic assignment."""
    refs = [data["dynamic_assignment_ref"] for data in compatible if "dynamic_assignment_ref" in data]
    if not refs:
        return None
    require(all(data.get("dynamic_assignment_ref") == refs[0]
                and data.get("creative_harness", {}).get("creative_harness", {}).get("dynamic_agents", {}).get("enabled")
                for data in compatible)
            and (selected_input is None or selected_input.get("dynamic_assignment_ref") == refs[0]),
            "VIEW_DENIED", "Conflicting runtime assignments for this task")
    return refs[0]


def refresh(kernel, principal, task_id, generation, frozen_ref):
    """Refresh remote authority outside NF transactions, using only the local immutable binding."""
    if frozen_ref is None:
        return None
    from .dynamic_agents import DynamicAgentService
    from .workshop_skills import DynamicSkillAuthority

    with kernel.db.uow(write=False) as repo:
        registry = DynamicAgentService(kernel)
        record = registry.load(repo, principal, task_id, generation, frozen_ref=frozen_ref)
    live = DynamicSkillAuthority(kernel).refresh(principal, task_id, generation,
                                                  workshop_binding=record["workshop_binding"])
    if record.get("recovered_from_generation") is not None:
        registry.record_recovery(principal, task_id, generation, frozen_ref=frozen_ref, live=live)
    return live


def verified(kernel, repo, principal, task, grant, frozen_ref, live, *, plane):
    """Check current NF authority and the short-lived W4 witness at the local operation boundary."""
    from .dynamic_agents import DynamicAgentService
    from .workshop_skills import DynamicSkillAuthority

    require(live is not None, "VIEW_DENIED", "Active Workshop task authority required")
    task_id, generation = task["task_id"], task["generation"]
    record = DynamicAgentService(kernel).load(repo, principal, task_id, generation, frozen_ref=frozen_ref)
    witness = DynamicSkillAuthority(kernel).verify(repo, principal, task_id, generation,
                                                   workshop_binding=record["workshop_binding"], live=live)
    require(witness["binding"] == record["workshop_binding"] and witness["skill_epoch"] == record["skill_epoch"]
            and witness["binding"]["content_sha"] == record["skill_sha256"]
            and sha256(record["skill_instructions"].encode()).hexdigest() == record["skill_sha256"]
            and sha256(record["role_instructions"].encode()).hexdigest() == record["profile_sha256"]
            and witness["role_id"] == record["role_id"] == grant["role_id"] == task["role_id"]
            and witness["task_kind"] == record["task_kind"] == task["task_kind"]
            and witness["grant_id"] == principal.grant_id and witness["principal_id"] == principal.principal_id
            and witness["permission_epoch"] == grant["epoch"] and witness["task_id"] == task_id
            and witness["generation"] == generation and plane in witness["planes"]
            and plane in grant["planes"] and "READ" in witness["capabilities"], "VIEW_DENIED",
            "Dynamic instructions no longer match the authorized task and scope")
    caps = set(grant["capabilities"]).intersection(witness["capabilities"], record["requested_capabilities"])
    require("READ" in caps, "VIEW_DENIED", "The source assignment did not authorize reading this task")
    return record, caps


def refresh_task(kernel, principal, task_id, generation):
    with kernel.db.uow(write=False) as repo:
        task, _ = kernel._task_owner(repo, principal, task_id)
        inputs = repo.one("task_inputs", {"task_id": task_id, "generation": generation})
        require(inputs is not None, "STALE_GENERATION", "Original task input unavailable")
        ref = assignment_for(repo, task, inputs["spec"])
    if ref is not None:
        return ref, refresh(kernel, principal, task_id, generation, ref)
    return None


def verify_task(kernel, repo, principal, task, proof, *, required_tools=(), required_capabilities=()):
    from .queries import WorldQueryService

    inputs = repo.one("task_inputs", {"task_id": task["task_id"], "generation": task["generation"]})
    ref = assignment_for(repo, task, inputs["spec"])
    if ref is None:
        require(proof is None, "TASK_REBIND_REQUIRED", "Runtime assignment changed")
        return
    _, grant, _ = WorldQueryService(kernel)._task(repo, principal, task["task_id"], task["generation"])
    require(proof and ref == proof[0], "VIEW_DENIED", "Workshop assignment must be verified for delivery")
    context = repo.one("contexts", {"task_id": task["task_id"], "generation": task["generation"],
                                    "context_bundle_id": task["context_bundle_id"]})
    require(context, "CONTEXT_REQUIRED", "Task context required for delegated delivery")
    assignment, capabilities = verified(kernel, repo, principal, task, grant, ref, proof[1],
                                        plane=context["read_cut"]["knowledge_cut"]["plane"])
    require(set(required_tools).issubset(assignment["requested_tools"])
            and set(required_capabilities).issubset(capabilities), "VIEW_DENIED",
            "Dynamic task does not authorize this operation")


def _candidate_scope_id(ref):
    return str(uuid5(UUID(ref["artifact_revision_id"]), "dynamic-candidate-scope"))


def protect_candidate(kernel, repo, task, world_ref, ref, assignment_ref, *, adaptive_ref=None, context_ref=None):
    """Preserve the validated output schema, registering its access scope in the same transaction."""
    scope = {"world_ref": world_ref, "task_id": task["task_id"], "generation": task["generation"]}
    payload = {"scope": scope, "candidate_ref": ref, "dynamic_assignment_ref": assignment_ref}
    if adaptive_ref is not None:
        payload["adaptive_instruction_ref"] = adaptive_ref
    if context_ref is not None:
        payload["creative_context_ref"] = context_ref
    marker = kernel._blob(payload)
    marker["artifact_revision_id"] = _candidate_scope_id(ref)
    kernel._register_artifact(repo, marker, task["work_id"], payload, "CREATIVE_DYNAMIC_CANDIDATE_SCOPE",
                              task["principal_id"])


def audit_assignment(repo, work, artifact, ref):
    kind = artifact["kind"]
    if kind == "AGENT_CANDIDATE":
        marker = repo.one("artifacts", {"work_id": work, "kind": "CREATIVE_DYNAMIC_CANDIDATE_SCOPE",
                                        "artifact_revision_id": _candidate_scope_id(ref)})
        if not marker:
            return None
        require(marker["payload"]["candidate_ref"] == ref, "VIEW_DENIED", "Candidate scope mismatch")
        payload = marker["payload"]
    elif kind.startswith("AGENT_ROLE_TOOL_") or kind in {
            "AGENT_INPUT", "AGENT_PROMPT", "AGENT_RESPONSE", "AGENT_BLOCKED", "AGENT_CALL_EVIDENCE"}:
        payload = artifact["payload"]
    else:
        return None
    for name, expected_kind in (("dynamic_assignment_ref", "CREATIVE_DYNAMIC_FREEZE"),
                                ("adaptive_instruction_ref", "CREATIVE_INSTRUCTION_SELECTION"),
                                ("creative_context_ref", "CREATIVE_CONTEXT_SNAPSHOT")):
        if payload.get(name):
            return expected_kind, payload[name]
    return None

def audit_branches(kernel, repo, work, artifact, ref):
    """Return protected branches, None for static content, or () for an unbound legacy input."""
    kind = artifact["kind"]
    if kind != "AGENT_CANDIDATE" and not kind.startswith("AGENT_ROLE_TOOL_") and kind not in {
        "AGENT_INPUT", "AGENT_PROMPT", "AGENT_RESPONSE", "AGENT_BLOCKED", "AGENT_CALL_EVIDENCE",
    }:
        return None
    if kind == "AGENT_INPUT" and "routing_decision" in artifact["payload"]:
        policy = kernel.artifact(repo, artifact["payload"]["routing_decision"]["policy_ref"], work,
                                 kind="RUNTIME_ROUTING_POLICY")["payload"]
        return (policy["branch_id"],)
    assignment = audit_assignment(repo, work, artifact, ref)
    if assignment:
        scope_kind, scope_ref = assignment
        record = kernel.artifact(repo, scope_ref, work, kind=scope_kind)["payload"]
        scope = record["packet"]["scope"] if scope_kind == "CREATIVE_CONTEXT_SNAPSHOT" else record["scope"]
        return (scope["world_ref"]["branch_id"],)
    if kind == "AGENT_INPUT":
        if not artifact["payload"].get("creative_harness", {}).get("creative_harness", {}).get("enabled"):
            return None
        # A prepared input can outlive (or precede) its first task. New inputs have
        # an origin in the same transaction as preparation. Older bound inputs
        # retain their original TaskSpecs, including after cancellation or recovery.
        branch = repo.conn.scalar(text("""
            SELECT branch_id::text FROM nf_audit_events WHERE event_id=:id AND work_id=:work
            AND evidence_id=:id AND event_type='artifact.agent_input'
        """), {"id": ref["artifact_revision_id"], "work": work})
        if branch:
            return (branch,)
        return tuple(repo.conn.scalars(text("""
            SELECT DISTINCT i.spec->'world_ref'->>'branch_id'
            FROM nf_task_inputs i JOIN nf_tasks t ON t.task_id=i.task_id
            WHERE t.work_id=:work AND i.spec->'input_artifacts' @> CAST(:refs AS jsonb)
        """), {"work": work, "refs": json.dumps([ref])}))

    # Older responses, candidates and role-tool records have no scope field.
    # Every gateway stage registered its audit event in the artifact transaction.
    # Use the original generation, even when its task has since been reclaimed.
    event = repo.conn.execute(text("""
        SELECT c.context_bundle_id::text, e.branch_id::text
        FROM nf_audit_events e JOIN nf_contexts c ON c.work_id=e.work_id
             AND c.task_id=e.task_id AND c.generation=e.generation
        WHERE e.event_id=:id AND e.evidence_id=:id AND e.work_id=:work AND e.event_type=:event
    """), {"id": ref["artifact_revision_id"], "work": work,
           "event": "model.completed" if kind == "AGENT_CALL_EVIDENCE" else "artifact." + kind.lower()})
    from .creative_context_store import _revision

    artifacts = repo.tables["artifacts"]
    for context_id, branch in event:
        exists = repo.conn.scalar(select(artifacts.c.artifact_revision_id).where(
            artifacts.c.work_id == work, artifacts.c.kind == "CREATIVE_CONTEXT_SNAPSHOT",
            artifacts.c.artifact_revision_id == _revision(context_id)))
        if exists:
            return (branch,)
    return None


def check_input_branch(kernel, repo, world_ref, artifact, ref):
    """A new binding cannot establish provenance for a previously unbound legacy input."""
    branches = audit_branches(kernel, repo, world_ref["work_id"], artifact, ref)
    require(branches is None or branches == (world_ref["branch_id"],),
            "VIEW_DENIED", "Agent input is unavailable on this branch")
