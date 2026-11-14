"""Content-free, scoped observations at the transactions that produce Harness evidence."""

import re
from collections import Counter
from hashlib import sha256
from time import monotonic
from uuid import UUID, uuid5

from sqlalchemy import text

from novel_factory.domain.canonical import canonical_bytes
from novel_factory.domain.creative_review import RUBRICS

from .audit_log import _task_scope, _uuid, record, supports_harness_details

EVENTS = {
    "CREATIVE_PLAN_CANDIDATE": ("S2", "plan.candidate"),
    "CREATIVE_PLAN_DECISION": ("S2", "plan.decision"),
    "CREATIVE_SIMULATION_RUN": ("S2", "simulation.completed"),
    "CREATIVE_REVIEW_DETAILS": ("S3", "review.completed"),
    "CREATIVE_REVIEW_DECISION": ("S3", "review.decision"),
    "CREATIVE_REVIEW_REVISION": ("S3", "review.revised"),
    "CREATIVE_DYNAMIC_REVISION": ("S4", "agent.revision"),
    "CREATIVE_DYNAMIC_EVENT": ("S4", "agent.selection"),
    "CREATIVE_DYNAMIC_FREEZE": ("S4", "agent.frozen"),
    "CREATIVE_DYNAMIC_GENERATION": ("S4", "agent.recovered"),
    "CREATIVE_INSTRUCTION_CANDIDATE": ("S5", "instruction.candidate"),
    "CREATIVE_ADAPTIVE_ENGINEERING_REPORT": ("S5", "instruction.evaluated"),
    "CREATIVE_INSTRUCTION_OPINION": ("S5", "instruction.reviewed"),
    "CREATIVE_INSTRUCTION_DECISION": ("S5", "instruction.decision"),
    "CREATIVE_INSTRUCTION_SELECTION": ("S5", "instruction.selection"),
    "CREATIVE_INSTRUCTION_MONITOR": ("S5", "instruction.monitored"),
}
STOPS = {
    "DEADLINE",
    "TOKEN_BUDGET",
    "STEP_LIMIT",
    "BRANCH_LIMIT",
    "INSUFFICIENT_EVIDENCE",
    "ATTEMPT_LIMIT",
    "COMPLETED",
}
REF_KEYS = (
    "candidate_ref",
    "report_ref",
    "instruction_ref",
    "decision_ref",
    "instance_ref",
    "revision_ref",
    "source_adaptive_ref",
    "run_manifest_ref",
    "review_ref",
    "plan_ref",
    "restored_selection_ref",
    "selected_ref",
    "rollback_ref",
    "previous_version_ref",
    "profile_ref",
    "skill_ref",
)
LABELS = {
    "status": {"ADOPTED", "DECLINED", "COMPLETED", "INCOMPLETE", "COMPLETE", "BLOCKED", "FAILED"},
    "action": {"SELECT", "EXIT", "ARCHIVE", "RETURN", "RETAIN", "APPROVE", "WITHDRAW", "REJECT", "REVISE"},
    "decision": {"APPROVE", "REJECT", "ROLLBACK"},
    "verdict": {"APPROVE", "REJECT"},
    "route": {"MANUAL", "POLICY_AUTOMATIC"},
    "layer": set(RUBRICS),
    "trigger": {"ORDINARY", "MOTIVE_CONFLICT", "IRREVERSIBLE"},
    "change_kind": {"ROLE_INSTRUCTIONS", "SKILL_INSTRUCTIONS", "CONTEXT_RECIPE", "REVIEW_RUBRIC"},
}


def _digest(value):
    return sha256(value.encode()).hexdigest()


def _identity(value):
    if value is None:
        return None
    try:
        return str(UUID(str(value)))
    except ValueError:
        return _digest(str(value))


def _version(value):
    if not isinstance(value, str):
        return None
    return value if re.fullmatch(r"v?\d{1,6}(\.\d{1,6}){0,3}", value) else _digest(value)


def _revision(ref):
    return _identity(ref.get("artifact_revision_id")) if isinstance(ref, dict) else None


def _binding(binding):
    if not binding:
        return {}
    return {
        "installation_id": _identity(binding.get("installation_id")),
        "employee_id": _identity(binding.get("employee_id")),
        "employment_revision": binding.get("employment_revision"),
        "binding_id": _identity(binding.get("binding_id")),
        "skill_source_revision": _version(str(binding["source_revision"])),
        "skill_content_sha256": binding.get("content_sha"),
    }


def _switches(flags):
    return {
        "enabled": flags.get("enabled") is True,
        "dynamic_agents": flags.get("dynamic_agents", {}).get("enabled") is True,
        "adaptive_instructions": flags.get("adaptive_instructions", {}).get("enabled") is True,
        "auto_promote": flags.get("adaptive_instructions", {}).get("auto_promote") is True,
    }


def _runtime(conn, task, generation):
    if not task or generation is None:
        return {}
    # Select individual immutable metadata fields. Never fetch the role prompt,
    # its assignment, the compiled context, the manuscript, or any role's beliefs.
    row = (
        conn.execute(
            text("""
        SELECT a.payload #> '{creative_harness,creative_harness}' AS flags,
          a.payload->>'runtime_sha256' AS runtime_sha256,
          ar.payload->>'role_contract_version' AS role_version,
          ar.payload->>'skill_version' AS skill_version,
          d.payload->'workshop_binding' AS binding, d.payload->>'profile_sha256' AS profile_sha256,
          d.payload->>'profile_version' AS profile_version, d.payload->>'skill_epoch' AS skill_epoch,
          a.payload->'adaptive_instruction_ref'->>'artifact_revision_id' AS selection_ref
        FROM nf_task_inputs i
        CROSS JOIN LATERAL jsonb_array_elements(i.spec->'input_artifacts') x
        JOIN nf_artifacts a ON a.artifact_revision_id=(x->>'artifact_revision_id')::uuid AND a.kind='AGENT_INPUT'
        LEFT JOIN nf_artifacts ar ON ar.artifact_revision_id=(a.payload #>> '{runtime_ref,artifact_revision_id}')::uuid
        LEFT JOIN nf_artifacts d ON d.artifact_revision_id=(a.payload #>> '{dynamic_assignment_ref,artifact_revision_id}')::uuid
        WHERE i.task_id=:task AND i.generation=:generation
          AND a.payload->>'role_id'=i.spec->>'role_id' AND a.payload->>'runtime_sha256'=i.spec->>'runtime_contract_version'
        LIMIT 1
    """),
            {"task": task, "generation": generation},
        )
        .mappings()
        .first()
    )
    if not row:
        return {}
    flags = row["flags"] or {}
    metadata = {
        "switches": _switches(flags),
        "runtime_sha256": row["runtime_sha256"],
        "role_version": _version(row["role_version"]),
        "skill_version": _version(row["skill_version"]),
        "profile_sha256": row["profile_sha256"],
        "profile_version": _version(row["profile_version"]),
        "skill_epoch": int(row["skill_epoch"]) if row["skill_epoch"] else None,
        "selection_ref": row["selection_ref"],
        **_binding(row["binding"]),
    }
    return {key: value for key, value in metadata.items() if value is not None}


def compiled_context(conn, event_id, compiled, started):
    if not supports_harness_details(conn):
        return
    bundle = compiled["bundle"]
    scope = _task_scope(conn, bundle["task_id"], bundle["generation"])
    info = _runtime(conn, bundle["task_id"], bundle["generation"])
    info.update(
        stage="CONTEXT",
        packet_bytes=len(canonical_bytes(compiled)),
        source_count=len(bundle["permitted_evidence_refs"]),
        included_count=len(compiled["sections"]),
        omitted_count=len(compiled["omitted_optional_scopes"]),
    )
    record(
        conn,
        "context.compiled",
        event_id=event_id,
        evidence_id=bundle["context_bundle_id"],
        duration_ms=round((monotonic() - started) * 1000, 3),
        harness_details=info,
        **scope,
    )


def creative_packet(conn, ref, packet, user, started):
    if not supports_harness_details(conn):
        return
    scope = _task_scope(conn, packet["scope"]["task_id"], packet["scope"]["generation"])
    info = _runtime(conn, scope["task_id"], scope["generation"])
    info.update(
        stage="S1",
        packet_bytes=len(canonical_bytes(user)),
        source_count=len(packet["sources"]),
        # Required-MCP reviews retain the frozen packet but initially send
        # source references only. No creative section is in that wire input.
        included_count=len(user.get("creative_materials", [])),
        omitted_count=len(packet["omissions"]),
    )
    record(
        conn,
        "harness.context.packet",
        event_id=ref["artifact_revision_id"],
        evidence_id=ref["artifact_revision_id"],
        duration_ms=round((monotonic() - started) * 1000, 3),
        harness_details=info,
        **scope,
    )


def model_metadata(conn, data):
    info = _runtime(conn, data["task_id"], data["generation"])
    usage = data["usage"]
    return {
        **info,
        "stage": "MODEL",
        "prompt_ref": _revision(data.get("prompt_ref")),
        "prompt_sha256": (data.get("prompt_ref") or {}).get("sha256"),
        "accounting_state": data.get("accounting_state"),
        **{
            k: usage.get(k)
            for k in (
                "input_tokens",
                "output_tokens",
                "cached_input_tokens",
                "actual_amount",
                "currency",
                "cost_status",
            )
        },
    }


def artifact_observation(conn, kind, ref, work, payload, principal):
    if kind not in EVENTS or not supports_harness_details(conn):
        return
    stage, name = EVENTS[kind]
    scope = payload.get("scope", {})
    wr = scope.get("world_ref")
    if not wr or wr.get("work_id") != work:
        return
    fields = _task_scope(conn, scope.get("task_id"), scope.get("generation"))
    fields.update(
        work_id=work,
        branch_id=wr["branch_id"],
        world_revision_id=wr["world_revision_id"],
        principal_id=_uuid(principal),
    )
    info = _runtime(conn, scope.get("task_id"), scope.get("generation"))
    binding = (
        payload.get("binding") if kind == "CREATIVE_DYNAMIC_GENERATION" else payload.get("workshop_binding")
    )
    info.update(stage=stage, **_binding(binding))
    if kind == "CREATIVE_INSTRUCTION_DECISION":
        info["decision_switches"] = _switches(payload["creative_harness"])
        info["engineering_report_ref"] = _revision(payload["evidence"]["engineering_report_ref"])
        info["run_manifest_ref"] = _revision(payload["evidence"]["run_manifest_ref"])
    if scope.get("task_id") and not fields.get("task_id"):
        # Dynamic freeze is created during provisioning, before the FK task exists.
        info.update(
            provisioning_task_id=_identity(scope["task_id"]), provisioning_generation=scope["generation"]
        )
    for key, allowed in LABELS.items():
        value = payload.get(key)
        if isinstance(value, str) and value in allowed:
            info[key] = value
    for key in REF_KEYS:
        value = _revision(payload.get(key))
        if value:
            info[key] = value
    for key in ("epoch", "round", "horizon_scenes", "selection_epoch", "skill_epoch"):
        if type(payload.get(key)) is int:
            info[key] = payload[key]
    if kind == "CREATIVE_PLAN_CANDIDATE":
        info["candidate_count"] = len(payload["alternatives"])
    if kind == "CREATIVE_PLAN_DECISION" and payload.get("option_id"):
        info["selected_option_id"] = _identity(payload["option_id"])
    if kind == "CREATIVE_SIMULATION_RUN":
        info.update(
            branch_count=len(payload["stops"]),
            step_count=sum(s["steps"] for s in payload["stops"]),
            stop_reasons=dict(Counter(s["reason"] for s in payload["stops"] if s["reason"] in STOPS)),
            participant_count=len(payload["participants"]),
        )
        info.update(
            {
                k: payload["usage"][k]
                for k in (
                    "local_input_units",
                    "model_tokens",
                    "model_attempts",
                    "rollout_attempts",
                    "amount",
                    "currency",
                )
            }
        )
    if kind == "CREATIVE_REVIEW_DETAILS":
        info["finding_count"] = len(payload["findings"])
        patterns = {name for layer in RUBRICS.values() for name in layer}
        info["issue_types"] = dict(
            Counter(note["pattern"] for note in payload["findings"] if note.get("pattern") in patterns)
        )
    if kind == "CREATIVE_DYNAMIC_FREEZE":
        info.update(
            profile_version=_version(payload["profile_version"]), profile_sha256=payload["profile_sha256"]
        )
    outcome = next((info[k] for k in ("decision", "action", "verdict", "status") if k in info), None)
    record(
        conn,
        "harness." + name,
        event_id=ref["artifact_revision_id"],
        evidence_id=ref["artifact_revision_id"],
        outcome=outcome,
        harness_details=info,
        **fields,
    )


def delegation(conn, task_id, generation, receipt, *, binding=None, denied=False):
    if not supports_harness_details(conn):
        return
    scope = _task_scope(conn, task_id, generation)
    info = {
        "stage": "S4" if binding else "DELEGATION",
        **{k: str(receipt[k]) for k in ("operation_id", "receipt_ref", "installation_id")},
    }
    info.update(_binding(binding))
    identity = receipt["receipt_ref"]
    if binding:
        identity = uuid5(UUID(str(identity)), f"skill:{binding['binding_id']}:{generation}:{denied}")
    event = (
        "harness.skill.denied"
        if denied
        else "harness.skill.verified"
        if binding
        else "harness.delegation.bound"
    )
    record(
        conn,
        event,
        event_id=identity,
        evidence_id=receipt["receipt_ref"],
        harness_details=info,
        outcome="DENIED" if denied else "VERIFIED" if binding else "BOUND",
        error_code="DYNAMIC_SKILL_DENIED" if denied else None,
        **scope,
    )


def recovery(conn, task, generation, state, transition, *, unresolved=None):
    if not supports_harness_details(conn):
        return
    scope = _task_scope(conn, task["task_id"], generation)
    info = {
        "stage": "RECOVERY",
        "transition": transition,
        "previous_generation": task["generation"],
        "previous_state": task["state"],
    }
    if unresolved is not None:
        info["unresolved_attempts"] = unresolved
    event = uuid5(UUID(str(task["task_id"])), f"recover:{transition}:{generation}")
    record(
        conn,
        "harness.task.recovered",
        event_id=event,
        evidence_id=task["task_id"],
        outcome=state,
        harness_details=info,
        **scope,
    )


def model_error(reason):
    if reason is None:
        return None
    known = {
        "MODEL_TRANSPORT_UNKNOWN",
        "MODEL_RATE_LIMITED",
        "MODEL_RESPONSE_TOO_LARGE",
        "MODEL_HTTP_ERROR",
        "MODEL_PROTOCOL_ERROR",
        "MODEL_UNAVAILABLE",
        "HOST_BINDING_MISMATCH",
        "MODEL_IDENTITY_MISMATCH",
        "INVALID_ARGUMENT",
        "INVALID_OUTPUT",
        "MODEL_ADAPTER_FAILURE",
        "CALL_RECONCILIATION_REQUIRED",
        "DYNAMIC_SKILL_DENIED",
        "VIEW_DENIED",
        "BUDGET_EXCEEDED",
        "MODEL_REQUEST_TOO_LARGE",
        "MODEL_CONFIGURATION_UNAVAILABLE",
        "MODEL_INPUT_TOO_LARGE",
    }
    return reason if reason in known else "OTHER_MODEL_ERROR"
