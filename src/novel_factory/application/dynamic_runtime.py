"""Apply a provisioned assignment to one NF task and one live Workshop delegation."""
from hashlib import sha256
from uuid import UUID, uuid5

from novel_factory.application.kernel import require


def assignment_for(repo, task, spec, *, selected_input=None):
    """One runtime generation cannot mix a static input and a dynamic assignment."""
    inputs = repo.many("artifacts", {"work_id": task["work_id"], "kind": "AGENT_INPUT",
                       "artifact_revision_id": [r["artifact_revision_id"] for r in spec["input_artifacts"]]})
    compatible = [row["payload"] for row in inputs if
                  (row["payload"]["role_id"], row["payload"]["task_kind"], row["payload"]["runtime_sha256"])
                  == (task["role_id"], task["task_kind"], spec["runtime_contract_version"])]
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


def protect_candidate(kernel, repo, task, world_ref, ref, assignment_ref, *, adaptive_ref=None):
    """Preserve the validated output schema, registering its access scope in the same transaction."""
    scope = {"world_ref": world_ref, "task_id": task["task_id"], "generation": task["generation"]}
    payload = {"scope": scope, "candidate_ref": ref, "dynamic_assignment_ref": assignment_ref}
    if adaptive_ref is not None:
        payload["adaptive_instruction_ref"] = adaptive_ref
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
                                ("adaptive_instruction_ref", "CREATIVE_INSTRUCTION_SELECTION")):
        if payload.get(name):
            return expected_kind, payload[name]
    return None
