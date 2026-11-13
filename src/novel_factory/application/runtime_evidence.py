"""Project a branch's NF execution evidence into content-free, reproducibly ordered JSONL."""

import json
import re
from collections import Counter
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import text

from .audit_log import LogService
from .statistics import is_fixture_configuration

# All joins originate from a task, receipt, or selection inside this exact branch.
_GENERATIONS = """
SELECT t.task_id,t.role_id,t.task_kind,t.state AS current_state,t.generation AS current_generation,
       t.created_at AS task_created_at,t.updated_at AS task_updated_at,i.generation,i.input_hash,
       i.created_at AS generation_created_at,i.spec#>>'{world_ref,world_revision_id}' AS world_revision_id,
       c.context_bundle_id,c.dependency_fingerprint,c.created_at AS context_created_at,c.artifact_ref AS context_ref,
       e.attempt_id AS worker_attempt_id,e.created_at AS worker_claimed_at,
       w.mode AS wait_mode,w.created_at AS wait_entered_at,w.woken_at AS wait_ended_at,
       extract(epoch FROM (w.woken_at-w.created_at))*1000 AS wait_duration_ms,
       ai.artifact_revision_id AS input_revision_id, ai.sha256 AS input_sha256,
       ar.artifact_revision_id AS runtime_revision_id, ar.sha256 AS runtime_sha256,
       ar.payload->>'role_contract_version' AS role_version,ar.payload->>'skill_name' AS skill,
       ar.payload->>'skill_version' AS skill_version
FROM nf_tasks t JOIN nf_task_inputs i USING(task_id)
LEFT JOIN nf_contexts c ON c.work_id=t.work_id AND c.task_id=t.task_id AND c.generation=i.generation
LEFT JOIN nf_task_executions e ON e.task_id=t.task_id AND e.generation=i.generation
LEFT JOIN nf_workflow_waits w ON w.task_id=t.task_id AND w.generation=i.generation
LEFT JOIN LATERAL (
    SELECT a.artifact_revision_id,a.sha256,a.payload->'runtime_ref' AS runtime_ref
    FROM jsonb_array_elements(i.spec->'input_artifacts') x
    JOIN nf_artifacts a ON a.work_id=t.work_id AND a.artifact_revision_id=(x->>'artifact_revision_id')::uuid
      AND a.sha256=x->>'sha256' AND a.kind='AGENT_INPUT' AND a.payload->>'role_id'=t.role_id
      AND a.payload->>'runtime_sha256'=i.spec->>'runtime_contract_version'
) ai ON TRUE
LEFT JOIN nf_artifacts ar ON ar.work_id=t.work_id AND ar.kind='AGENT_RUNTIME'
  AND ar.artifact_revision_id=(ai.runtime_ref->>'artifact_revision_id')::uuid AND ar.sha256=ai.runtime_ref->>'sha256'
WHERE t.work_id=:work AND t.branch_id=:branch ORDER BY t.created_at,t.task_id,i.generation,ai.artifact_revision_id
"""
_CALLS = """
SELECT c.attempt_id,c.task_id,c.generation,c.budget_id,c.state AS ledger_state,c.request_hash,
       c.reserved_amount,c.actual_amount,c.reserved_tokens,c.actual_tokens,b.currency,
       c.provider_receipt,c.created_at AS reserved_at,c.updated_at AS ledger_updated_at,
       a.artifact_revision_id AS evidence_revision_id,a.sha256 AS evidence_sha256,
       a.payload->'prompt_ref' AS prompt_ref,a.payload->'response_ref' AS response_ref,
       a.payload->'output_ref' AS candidate_ref,a.payload->'input_ref' AS input_ref,
       a.payload->'creative_context_ref' AS creative_context_ref,
       a.payload->>'outcome' AS output_status,a.payload->>'accounting_state' AS recorded_accounting_state,
       a.payload#>'{usage,input_tokens}' AS input_tokens,a.payload#>'{usage,output_tokens}' AS output_tokens,
       a.payload#>'{usage,cached_input_tokens}' AS cached_input_tokens,
       a.payload->>'execution_kind' AS execution_kind,a.payload#>>'{model_configuration,adapter_kind}' AS adapter,
       a.payload#>>'{model_configuration,provider}' AS provider,a.payload#>>'{model_configuration,model}' AS model,
       a.payload#>>'{model_configuration,endpoint}' AS endpoint,
       e.event_time AS recorded_at,e.duration_ms AS elapsed_ms
FROM nf_tasks t JOIN nf_call_attempts c USING(task_id)
JOIN nf_budgets b ON b.budget_id=c.budget_id AND b.work_id=t.work_id
LEFT JOIN nf_audit_events e ON e.work_id=t.work_id AND e.branch_id=t.branch_id AND e.task_id=t.task_id
  AND e.generation=c.generation AND e.attempt_id=c.attempt_id AND e.event_type='model.completed'
LEFT JOIN nf_artifacts a ON a.work_id=t.work_id AND a.artifact_revision_id=e.evidence_id
  AND a.kind='AGENT_CALL_EVIDENCE' AND a.payload->>'request_sha256'=c.request_hash
WHERE t.work_id=:work AND t.branch_id=:branch ORDER BY c.created_at,c.attempt_id
"""
_TRANSITIONS = """
SELECT o.outbox_id AS event_id,t.task_id,o.payload->'generation' AS generation,
       o.payload->>'state' AS state,o.created_at AS changed_at
FROM nf_tasks t JOIN nf_outbox o ON o.aggregate_type='task' AND o.aggregate_id=t.task_id::text
WHERE t.work_id=:work AND t.branch_id=:branch AND o.topic='task.changed'
ORDER BY o.created_at,o.outbox_id
"""
_COMMANDS = """
SELECT r.command_type,r.created_at,r.request_hash,r.payload->>'status' AS outcome,
       r.payload->'task_id' AS task_id,r.payload->'generation' AS generation,
       r.payload->'attempt_id' AS attempt_id,r.payload->'context_bundle_id' AS context_bundle_id,
       r.payload->'design_ref' AS design_ref,r.payload->'result_ref' AS result_ref,
       r.payload->'candidate_ref' AS candidate_ref,r.payload->'input_ref' AS input_ref,
       r.payload->'length_ref' AS length_ref,r.payload#>'{design_review,result_ref}' AS design_review_ref,
       CASE WHEN r.payload ? 'artifact_revision_id' THEN jsonb_build_object(
           'artifact_id',r.payload->'artifact_id','artifact_revision_id',r.payload->'artifact_revision_id',
           'sha256',r.payload->'sha256') END AS artifact_ref,
       r.payload->'reconciliation_ref' AS reconciliation_ref,r.payload->'alignment_ref' AS alignment_ref,
       r.payload->'board_ref' AS board_ref,r.payload->'window_ref' AS window_ref,
       r.payload->'round_ref' AS round_ref,r.payload->'plan_ref' AS plan_ref
FROM nf_receipts r WHERE r.work_id=:work AND r.branch_id=:branch AND r.command_type IN (
    'NARRATIVE_DRAFT','NARRATIVE_EXTRACTION','NARRATIVE_RECONCILE','NARRATIVE_ACCEPT','PLAN_ADOPT',
    'ROLLING_REGISTER','ROLLING_RETRY','ROLLING_REPLAN','P6_BOOK_REVIEW','P6_INVOICE',
    'P6_IDEA_ENROLL','P6_IDEA_REVIEW','P6_IDEA_MATERIALIZE','P6_IDEA_PROVENANCE',
    'P6_NEXT_PLANNER','P6_NEXT_PLAN_REVIEW','P6_NEXT_PLAN_ACCEPT','P6_NEXT_PLAN_BLOCKED')
ORDER BY r.created_at,r.command_type,r.request_hash,r.principal_id,r.idempotency_key
"""
_WORLD = """
SELECT v.world_revision_id,v.parent_revision_id,v.branch_seq,v.manifest_hash,v.state_hash,
       v.created_at,c.request->>'task_id' AS task_id,c.request->'generation' AS generation
FROM nf_revisions v JOIN nf_commits c USING(world_revision_id)
WHERE v.work_id=:work AND v.branch_id=:branch ORDER BY v.branch_seq
"""
_SELECTIONS = """
SELECT a.artifact_id,a.artifact_revision_id,a.sha256,a.kind,s.epoch
FROM nf_selections s JOIN nf_artifacts a ON a.work_id=s.work_id
  AND a.artifact_revision_id=(s.artifact_ref->>'artifact_revision_id')::uuid AND a.sha256=s.artifact_ref->>'sha256'
WHERE s.work_id=:work AND s.selection_kind IN
  ('P6_PROFILE:'||:branch,'P6_DAG:'||:branch,'P6_EXTENT:'||:branch,'P6_ROLLING:'||:branch,'P6_BLIND_REVIEW:'||:branch)
ORDER BY s.selection_kind
"""
_RELEASES = """
SELECT r.created_at, r.payload->'release_ref' AS release_ref,r.payload->'manifest_ref' AS manifest_ref,
       r.payload#>>'{manifest,manifest_sha256}' AS manifest_sha256,
       r.payload#>>'{manifest,release_id}' AS release_id,r.payload#>>'{manifest,world_ref,world_revision_id}' AS world_revision_id,
       r.payload->>'external_delivery' AS external_delivery,r.payload->'selection_epoch' AS selection_epoch,
       a.payload->>'volume_id' AS volume_id,r.payload#>'{manifest,ordered_artifacts}' AS ordered_artifacts
FROM nf_receipts r JOIN nf_artifacts a ON a.work_id=r.work_id AND a.kind='RELEASE_RECORD'
  AND a.artifact_revision_id=(r.payload#>>'{release_ref,artifact_revision_id}')::uuid
  AND a.sha256=r.payload#>>'{release_ref,sha256}'
WHERE r.work_id=:work AND r.branch_id=:branch AND r.command_type='RELEASE_FREEZE'
  AND r.payload#>>'{manifest,world_ref,branch_id}'=r.branch_id::text ORDER BY r.created_at,a.artifact_revision_id
"""
_LABELS = {
    "role_id",
    "task_kind",
    "current_state",
    "role_version",
    "skill",
    "skill_version",
    "kind",
    "event_type",
    "tool_name",
    "outcome",
    "error_code",
    "artifact_kind",
    "adapter",
    "model",
    "provider",
    "output_status",
    "execution_kind",
    "recorded_accounting_state",
    "external_delivery",
}
_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*(?:/[A-Za-z0-9][A-Za-z0-9_.-]*){0,2}")
_CREDENTIAL = re.compile(
    r"(?i)(?:sk-(?:proj-)?|gh[pousr]_|github_pat_|xox[baprs]-|AKIA|ASIA|eyJ[A-Za-z0-9_-]{10,})"
)


def _label(value):
    if (
        isinstance(value, str)
        and len(value) <= 160
        and _LABEL.fullmatch(value)
        and not _CREDENTIAL.search(value)
    ):
        return value
    return None


def _ref(value):
    """Output exactly the three typed ArtifactRef fields, even on historical receipts."""
    if isinstance(value, dict) and re.fullmatch(r"[0-9a-f]{64}", str(value.get("sha256"))):
        try:
            return {
                "artifact_id": str(UUID(value["artifact_id"])),
                "artifact_revision_id": str(UUID(value["artifact_revision_id"])),
                "sha256": value["sha256"],
            }
        except (AttributeError, KeyError, TypeError, ValueError):
            pass
    return None


def _project(row, kind):
    result = dict(row)
    if kind == "call":
        receipt = result.pop("provider_receipt")
        result["provider_receipt_sha256"] = (
            sha256(receipt.encode()).hexdigest() if receipt else None
        )
        cfg = {
            "execution_kind": result["execution_kind"],
            "endpoint": result.pop("endpoint") or "",
            "provider": result["provider"],
            "adapter_kind": result["adapter"],
        }
        result["call_origin"] = (
            "UNOBSERVED"
            if not result["evidence_revision_id"]
            else "FIXTURE"
            if is_fixture_configuration(cfg)
            else "PROVIDER"
        )
        # Some native adapters store a provider base URL. Only its hostname is a provider label.
        if "://" in (result["provider"] or ""):
            result["provider"] = urlsplit(result["provider"]).hostname
    if kind == "release":
        result["ordered_text_refs"] = [
            _ref(item["artifact_ref"]) for item in result.pop("ordered_artifacts")
        ]
    for key, value in result.items():
        if key in _LABELS:
            result[key] = _label(value)
        elif kind == "business_receipt" and key in {"task_id", "attempt_id", "context_bundle_id"}:
            try:
                result[key] = str(UUID(value)) if value else None
            except (TypeError, ValueError, AttributeError):
                result[key] = None
        elif key.endswith("_ref"):
            result[key] = _ref(value)
        elif isinstance(value, (UUID, Decimal)):
            result[key] = str(value)
        elif isinstance(value, datetime):
            result[key] = value.isoformat()
    return {"record": kind, **result}


def export_runtime_evidence(kernel, principal, work, branch, stream):
    """Write an authorized snapshot to an unpublished binary stream. Return its complete digest."""
    digest = sha256()
    counts = Counter()

    def emit(record):
        data = (
            json.dumps(
                record, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
            )
            + "\n"
        ).encode()
        digest.update(data)
        stream.write(data)
        counts[record["record"]] += 1

    scope = {"work": work, "branch": branch}
    with kernel.db.uow(write=False, repeatable_read=True) as repo:
        events = LogService.scoped_event_metadata(kernel, repo, principal, work, branch)
        emit({"record": "scope", "version": "1.0", "work_id": work, "branch_id": branch})
        for row in events:
            emit(_project(row, "event"))
        for kind, query in (
            ("task_generation", _GENERATIONS),
            ("task_transition", _TRANSITIONS),
            ("business_receipt", _COMMANDS),
            ("world_commit", _WORLD),
            ("call", _CALLS),
            ("selection", _SELECTIONS),
            ("release", _RELEASES),
        ):
            rows = repo.conn.execute(text(query).execution_options(yield_per=256), scope)
            for row in rows.mappings():
                emit(_project(row, kind))
    return {
        "version": "1.0",
        "work_id": work,
        "branch_id": branch,
        "row_counts": dict(counts),
        "file_sha256": digest.hexdigest(),
    }
