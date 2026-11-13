"""Harness simulation backend. No credentials, sockets, subprocesses or persistence."""

import hashlib
import json
import math
import random
from decimal import Decimal
from uuid import NAMESPACE_URL, uuid5

from .protocol import DryRunSpec, ModelCallFailure, ModelResponse

OUTCOMES = {"normal", "rate_limit", "timeout", "crash_before_send", "crash_after_send",
            "invalid_json", "truncated", "context_mismatch", "needs_input"}


def generate(profile, request, *, authorize, record):
    spec = request.simulation or DryRunSpec()
    if (not isinstance(spec, DryRunSpec) or type(spec.seed) is not int
            or type(spec.review_pass_rate) not in (int, float)
            or not math.isfinite(spec.review_pass_rate) or not 0 <= spec.review_pass_rate <= 1
            or not isinstance(spec.outcome, str) or spec.outcome not in OUTCOMES or not isinstance(spec.payload, dict)):
        raise ModelCallFailure("INVALID_DRY_RUN_SPEC", sent=False)
    try:
        # Copy the payload; simulated review never mutates a caller's template.
        payload = json.loads(json.dumps(spec.payload, allow_nan=False))
        user = next(json.loads(m["content"]) for m in request.messages if m["role"] == "user")
        identity = user.get("context")
        for message in request.messages:
            if message["role"] == "system" and "\nIdentity: " in message["content"]:
                identity = json.loads(message["content"].split("\nIdentity: ", 1)[1].splitlines()[0])
        if not isinstance(identity, dict) or not {"task_id", "generation", "role_id"} <= identity.keys():
            raise ValueError
    except (KeyError, TypeError, ValueError, StopIteration):
        raise ModelCallFailure("INVALID_DRY_RUN_INPUT", sent=False) from None
    if spec.outcome in {"rate_limit", "crash_before_send"}:
        raise ModelCallFailure("MODEL_RATE_LIMITED" if spec.outcome == "rate_limit" else "WORKER_PROCESS_EXITED",
                               sent=False, http_status=429 if spec.outcome == "rate_limit" else None)
    if authorize(request.attempt_id) is False:
        raise ModelCallFailure("MODEL_CALL_NOT_AUTHORIZED", sent=False)
    thread, turn = "dry-run-thread:" + request.attempt_id, "dry-run-turn:" + request.attempt_id
    if profile.kind == "codex-cli":
        record(request.attempt_id, thread, None, "THREAD_STARTED")
        record(request.attempt_id, thread, turn, "TURN_STARTED")
    if spec.outcome in {"timeout", "crash_after_send"}:
        raise ModelCallFailure("MODEL_TRANSPORT_UNKNOWN" if spec.outcome == "timeout" else "WORKER_PROCESS_EXITED")
    random_seed = hashlib.sha256(f"{spec.seed}:{request.attempt_id}".encode()).digest()
    approved = random.Random(int.from_bytes(random_seed)).random() < spec.review_pass_rate
    is_review = identity["role_id"] == "reviewer"
    if is_review and spec.outcome != "needs_input":
        payload["summary"] = "DRY_RUN_REVIEW_PASS" if approved else "DRY_RUN_REVIEW_RETURN"
        if not approved:
            subject = user.get("dry_run_subject_ref")
            if not isinstance(subject, dict):
                raise ModelCallFailure("INVALID_DRY_RUN_INPUT", sent=False)
            payload["issues"] = [{"issue_id": str(uuid5(NAMESPACE_URL, "dry-run-issue:" + request.attempt_id)),
                "category": "LITERARY", "severity": "BLOCKER", "subject_ref": subject,
                "description": "DRY_RUN_SIMULATED_REVIEW_RETURN", "issue_kind": "CHANGE_REQUEST",
                "evidence_refs": [{"kind": "ARTIFACT", "artifact_ref": subject}],
                "suggested_change": "DRY_RUN_TARGETED_REVISION", "disposition": "OPEN"}]
    envelope = {**{key: value for key, value in identity.items() if key != "dependency_fingerprint"},
                "schema_version": "2.1", "status": "SUCCEEDED", "unresolved_questions": [], "payload": payload}
    if spec.outcome == "needs_input":
        envelope.update(status="NEEDS_INPUT", unresolved_questions=["DRY_RUN_AUTHORIZED_MATERIAL_REQUIRED"])
    if spec.outcome == "context_mismatch":
        envelope["context_bundle_id"] = str(uuid5(NAMESPACE_URL, "dry-run:wrong-context"))
    content = json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if spec.outcome == "invalid_json":
        content = "{"
    if spec.outcome == "truncated":
        content = content[:max(1, len(content) // 2)]
    if profile.kind == "codex-cli":
        record(request.attempt_id, thread, turn, "COMPLETED")
    return ModelResponse(text=content, provider_receipt="dry-run:" + request.attempt_id,
        input_tokens=sum(len(m["content"].encode()) for m in request.messages) // 4,
        output_tokens=min(request.max_output_tokens, max(1, len(content.encode()) // 4)), cached_input_tokens=0,
        actual_amount=Decimal(0), currency="USD", request_sha256=request.request_sha256, dry_run=True,
        raw_response={"dry_run": True, "execution_kind": "TEST_FIXTURE", "model": profile.model,
                      "review_approved": approved if is_review and spec.outcome != "needs_input" else None,
                      "usage_source": "SIMULATED_BYTES_ESTIMATE",
                      "finish_reason": "length" if spec.outcome == "truncated" else "stop"})
