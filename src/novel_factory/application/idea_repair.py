"""Verify a returned, rejected design before a targeted revision or lossless derivation."""
from dataclasses import dataclass
from json import JSONDecodeError
from uuid import UUID, uuid5

from novel_factory.contracts.registry import validate_contract
from novel_factory.domain.canonical import DuplicateJSONObjectKey, fingerprint, strict_json_loads
from novel_factory.domain.errors import DomainError
from .kernel import require
from .prompt_schema import redundant_payload_fields, validated_payload
from .production_pipeline import _artifact


@dataclass(frozen=True)
class UnparsedDesign:
    text: str
    diagnostic: dict


def unparsed_diagnostic(error):
    if isinstance(error, DuplicateJSONObjectKey):
        return {"keyword": "duplicateKey"}
    syntax = error.__cause__
    if isinstance(syntax, JSONDecodeError):
        return {"keyword": "jsonSyntax", "line": syntax.lineno, "column": syntax.colno, "offset": syntax.pos}
    return None


def initial_or_structural_repair(round_index, designer):
    return round_index == 0 or (
        designer.get("status") == "DESIGN_REVISION_ENROLLED" and not designer.get("review_ref")
        and designer.get("revision_purpose") in {None, "JSON_SYNTAX_CORRECTION", "WINDOW_SCHEMA_CORRECTION"}
    )


def returned_design(kernel, repo, gateway, task, wr, designer, *, allow_malformed=False):
    work = wr["work_id"]
    require(task["state"] == "FAILED" and task["role_id"] == "world_designer"
            and task["task_kind"] == "initial_world",
            "TASK_NOT_ACCEPTABLE", "Only a current rejected design can be repaired")
    call = repo.one("call_attempts", {"task_id": task["task_id"], "generation": task["generation"],
                                     "call_key": "supervised-candidate"})
    require(call and call["state"] in {"UNKNOWN", "FAILED", "SUCCEEDED"} and call["provider_receipt"],
            "CALL_RECONCILIATION_REQUIRED", "The original design has no completed provider return")
    attempt = call["attempt_id"]
    row = gateway._read_audit(repo, attempt)
    require(row and row["work_id"] == work and row["kind"] == "AGENT_CALL_EVIDENCE",
            "INVALID_EVIDENCE", "The rejected design has no matching audit")
    audit = row["payload"]
    audit_ref = {key: row[key] for key in ("artifact_id", "artifact_revision_id", "sha256")}
    result = repo.one("task_results", {"task_id": task["task_id"], "generation": task["generation"],
                      "result_id": task["current_result_id"], "disposition": "CURRENT"})
    require(audit["outcome"] == "REJECTED" and audit["result_status"] == "FAILED"
            and audit["output_ref"] is None and audit["reason"] in {"INVALID_ARGUMENT", "INVALID_MODEL_OUTPUT"}
            and result and result["result_id"] == str(uuid5(UUID(attempt), "task-result"))
            and result["payload"]["status"] == "FAILED" and result["payload"]["artifacts"] == [audit_ref]
            and fingerprint(audit) == audit_ref["sha256"] and audit["world_ref"] == wr
            and audit["task_id"] == task["task_id"] and audit["generation"] == task["generation"]
            and audit["context_bundle_id"] == task["context_bundle_id"] and audit["attempt_id"] == attempt
            and audit["input_ref"] == designer["input_ref"] and audit["provider_receipt"] == call["provider_receipt"]
            and audit["response_ref"] and audit["response_ref"]["artifact_revision_id"] == str(uuid5(UUID(attempt), "response:revision")),
            "INVALID_EVIDENCE", "Rejected design differs from its current task or provider response")
    raw = kernel.artifact(repo, audit["response_ref"], work, kind="AGENT_RESPONSE")["payload"]
    require(fingerprint(raw) == audit["response_ref"]["sha256"]
            and raw["request_sha256"] == call["request_hash"] == audit["request_sha256"]
            and raw["provider_receipt"] == call["provider_receipt"],
            "INVALID_EVIDENCE", "The design response does not match the original supervised call")
    try:
        envelope = strict_json_loads(raw["text"])
    except DomainError as error:
        diagnostic = unparsed_diagnostic(error)
        if not allow_malformed or audit["reason"] != "INVALID_MODEL_OUTPUT" or diagnostic is None:
            raise
        if call["state"] == "UNKNOWN":
            from .postpaid_delivery import verify_postpaid_transport
            verify_postpaid_transport(kernel, repo, task, call, row,
                                      kernel.artifact(repo, audit["response_ref"], work, kind="AGENT_RESPONSE"),
                                      syntax_source=True)
        else:
            require(call["state"] == "FAILED" and call["actual_amount"] is not None and call["actual_tokens"] is not None,
                    "CALL_RECONCILIATION_REQUIRED", "The rejected return is not a completed, accounted attempt")
        return UnparsedDesign(raw["text"], diagnostic), audit, audit_ref
    require(isinstance(envelope, dict), "INVALID_EVIDENCE", "Returned design has no JSON envelope")
    return envelope, audit, audit_ref


def normalized_payload(kernel, repo, wr, designer, envelope, audit):
    raw = envelope.get("payload")
    target = kernel.artifact(repo, designer["input_ref"], wr["work_id"], kind="AGENT_INPUT")["payload"]["output_contract"]
    fields = redundant_payload_fields(envelope, target["name"], target["version"])
    if not fields:
        return None
    validate_contract("AgentModelResponse", envelope, "agent")
    require(envelope["status"] == "SUCCEEDED" and envelope["unresolved_questions"] == []
            and all(envelope[key] == audit[key] for key in (
                "task_id", "generation", "context_bundle_id", "world_ref", "role_id", "actor_id"
            )), "MODEL_IDENTITY_MISMATCH", "Returned design has mismatched envelope metadata")
    design = validated_payload(envelope, target["name"], target["version"])
    require(design == {k: v for k, v in raw.items() if k not in fields},
            "INVALID_EVIDENCE", "Only content-free, recognized metadata can be removed")
    return design


def normalized_design(kernel, repo, owner, task, wr, designer, envelope, audit, audit_ref):
    design = normalized_payload(kernel, repo, wr, designer, envelope, audit)
    if design is None:
        return None
    key = str(audit["attempt_id"])
    request = {"audit_ref": audit_ref, "response_ref": audit["response_ref"], "input_ref": designer["input_ref"]}
    receipt = kernel._prior(repo, owner, wr["work_id"], "P6_DESIGN_NORMALIZED", key, request)
    if receipt is None:
        ref = _artifact(kernel, repo, wr["work_id"], owner, "NORMALIZED_DESIGN_CANDIDATE", design)
        removed = sorted(set(envelope["payload"]) - set(design))
        operation = ({"operation": "REMOVE_DUPLICATE_EMPTY_UNRESOLVED_QUESTIONS"}
                     if removed == ["unresolved_questions"] else
                     {"operation": "REMOVE_CONTENT_FREE_DESIGN_METADATA", "removed_fields": removed})
        receipt = {**request, "output_ref": ref, "attempt_id": key, **operation}
        kernel._receipt(repo, owner, wr, "P6_DESIGN_NORMALIZED", key, request, receipt)
    return {**audit, "output_ref": receipt["output_ref"], "normalization": receipt}, design
