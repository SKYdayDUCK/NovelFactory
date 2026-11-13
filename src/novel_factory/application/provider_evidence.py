"""Read only the selected attempts in an operator's authorized work and branch."""

from datetime import datetime
from itertools import combinations

from sqlalchemy import text

from .kernel import require


_QUERY = """
SELECT c.attempt_id::text, c.task_id::text, c.generation, c.state, c.request_hash,
       c.budget_id::text, b.currency, c.actual_amount, t.role_id,
       a.payload#>>'{model_configuration,adapter_kind}' AS adapter,
       a.payload#>>'{model_configuration,provider}' AS provider,
       a.payload#>>'{model_configuration,auth_mode}' AS auth_mode,
       a.payload->>'execution_kind' AS execution_kind, a.payload->>'outcome' AS output_status,
       a.payload#>'{usage,input_tokens}' AS input_tokens, a.payload#>'{usage,output_tokens}' AS output_tokens,
       a.payload->>'provider_receipt' AS receipt_id, a.artifact_revision_id::text AS audit_revision,
       r.artifact_revision_id::text AS response_revision, r.sha256 AS response_sha256,
       (r.payload->>'provider_response_json')::jsonb->'transport_window' AS transport_window
FROM nf_call_attempts c JOIN nf_tasks t ON t.task_id=c.task_id
JOIN nf_budgets b ON b.work_id=t.work_id AND b.budget_id=c.budget_id
LEFT JOIN nf_audit_events e ON e.work_id=t.work_id AND e.branch_id=t.branch_id AND e.task_id=c.task_id
 AND e.generation=c.generation AND e.attempt_id=c.attempt_id AND e.event_type='model.completed'
LEFT JOIN nf_artifacts a ON a.artifact_revision_id=e.evidence_id AND a.kind='AGENT_CALL_EVIDENCE'
 AND a.work_id=t.work_id AND a.payload->>'request_sha256'=c.request_hash
LEFT JOIN nf_artifacts r ON r.artifact_revision_id=(a.payload#>>'{response_ref,artifact_revision_id}')::uuid
 AND r.work_id=t.work_id AND r.sha256=a.payload#>>'{response_ref,sha256}' AND r.kind='AGENT_RESPONSE'
WHERE t.work_id=:work AND t.branch_id=:branch AND c.attempt_id=ANY(CAST(:ids AS uuid[]))
ORDER BY c.attempt_id
"""


def _interval(row):
    window = row["transport_window"]
    if not (row["receipt_id"] and row["response_sha256"] and isinstance(window, dict)):
        return None
    try:
        start, end = (datetime.fromisoformat(window[k]) for k in ("started_at", "received_at"))
        if start.tzinfo and end.tzinfo and start < end:
            return start, end
    except (ValueError, KeyError, TypeError):
        pass
    return None


def summarize(rows):
    pairs = []
    for first, second in combinations(rows, 2):
        if first["role_id"] == second["role_id"]:
            continue
        kinds = {(r["adapter"], r["provider"], r["execution_kind"]) for r in (first, second)}
        if kinds != {
            ("codex-cli", "codex-cli", "REAL_HOST"),
            ("deepseek", "https://api.deepseek.com", "REAL_EXTERNAL"),
        }:
            continue
        a, b = _interval(first), _interval(second)
        if a and b and (span := (min(a[1], b[1]) - max(a[0], b[0])).total_seconds()) > 0:
            pairs.append(
                {
                    "attempt_ids": [first["attempt_id"], second["attempt_id"]],
                    "overlap_seconds": span,
                }
            )
    return {
        "attempts": rows,
        "overlapping_real_transports": pairs,
        "dual_provider_overlap": "OBSERVED" if pairs else "NOT_DEMONSTRATED",
        "literary_quality": "NOT_EVALUATED",
    }


def inspect(kernel, owner, work, branch, ids):
    require(
        1 <= len(ids) <= 64 and len(set(ids)) == len(ids),
        "INVALID_ARGUMENT",
        "Select one to sixty-four distinct attempt IDs",
    )
    with kernel.db.uow(write=False, repeatable_read=True) as repo:
        grant = kernel.authorize(repo, owner, work, "ADMIN", branch)
        require(
            grant["role_id"] == "control_operator", "VIEW_DENIED", "Project controller required"
        )
        result = repo.conn.execute(text(_QUERY), {"work": work, "branch": branch, "ids": ids})
        rows = [dict(r) for r in result.mappings()]
        require(len(rows) == len(ids), "VIEW_DENIED", "One or more attempts are outside this scope")
    for row in rows:
        amount = row.pop("actual_amount")
        row["settled_amount"] = (
            str(amount) if row["state"] in {"SUCCEEDED", "FAILED"} and amount is not None else None
        )
    return {"work_id": work, "branch_id": branch, **summarize(rows)}


def reconcile_invoice(
    kernel, owner, work, branch, attempt, *, receipt_id, currency, amount, tokens, document_sha256
):
    """Post an operator-verified supplier line to its original call, once."""
    import re

    require(
        isinstance(document_sha256, str)
        and re.fullmatch("[0-9a-f]{64}", document_sha256)
        and isinstance(receipt_id, str)
        and 1 <= len(receipt_id) <= 512
        and isinstance(amount, str)
        and re.fullmatch(r"[0-9]{1,15}(?:\.[0-9]{1,6})?", amount)
        and type(tokens) is int
        and tokens >= 0,
        "INVALID_ARGUMENT",
        "A bound invoice line and actual usage are required",
    )
    request = {
        "attempt": attempt,
        "receipt_id": receipt_id,
        "currency": currency,
        "amount": amount,
        "tokens": tokens,
        "document_sha256": document_sha256,
    }
    with kernel.db.uow() as repo:
        grant = kernel.authorize(repo, owner, work, "ADMIN", branch)
        require(
            grant["role_id"] == "control_operator" and grant["branch_ids"] == ["*"],
            "VIEW_DENIED",
            "Work-wide budget administration required",
        )
        rows = list(
            repo.conn.execute(
                text(_QUERY), {"ids": [attempt], "branch": branch, "work": work}
            ).mappings()
        )
        require(len(rows) == 1, "VIEW_DENIED", "Attempt unavailable")
        row = rows[0]
        require(
            row["receipt_id"] == receipt_id
            and row["currency"] == currency
            and row["response_revision"]
            and row["output_status"] in {"VALIDATED_CANDIDATE", "REJECTED"},
            "INVALID_EVIDENCE",
            "Billing evidence must match the recorded response and currency",
        )
        require(
            row["input_tokens"] is not None
            and row["output_tokens"] is not None
            and row["input_tokens"] + row["output_tokens"] == tokens,
            "INVALID_EVIDENCE",
            "Invoice usage must match the recorded provider usage",
        )
        repo.one("tasks", {"task_id": row["task_id"]}, lock="update")
        prior = kernel._prior(repo, owner, work, "P6_INVOICE", attempt, request)
        if prior:
            return prior
        current = repo.one("call_attempts", {"attempt_id": attempt})
        require(
            current["state"] == "UNKNOWN",
            "INVALID_CALL_TRANSITION",
            "Only an unresolved call can be reconciled",
        )
        state = "SUCCEEDED" if row["output_status"] == "VALIDATED_CANDIDATE" else "FAILED"
        kernel.in_transaction(repo).tasks.settle_call(
            attempt,
            state=state,
            actual_amount=amount,
            actual_tokens=tokens,
            provider_receipt=receipt_id,
        )
        result = {
            "attempt": attempt,
            "state": state,
            "amount": amount,
            "currency": currency,
            "tokens": tokens,
            "document_sha256": document_sha256,
        }
        kernel._receipt(
            repo, owner, kernel.head(repo, work, branch), "P6_INVOICE", attempt, request, result
        )
        return result


def inspect_wire_receipt(kernel, owner, work, branch, attempt):
    """Authorized local read only. Delivery and supplier settlement retain their original gates."""
    import hashlib
    import json
    from uuid import UUID, uuid5

    from ..domain.canonical import fingerprint
    from ..infrastructure.wire_receipts import read_receipt

    with kernel.db.uow(write=False) as repo:
        kernel.authorize(repo, owner, work, "ADMIN", branch)
        call = repo.one("call_attempts", {"attempt_id": attempt})
        require(
            call
            and repo.one(
                "tasks", {"task_id": call["task_id"], "work_id": work, "branch_id": branch}
            ),
            "VIEW_DENIED",
            "Attempt unavailable",
        )
        refs = {}
        for label, kind in (
            ("prompt", "AGENT_PROMPT"),
            ("response", "AGENT_RESPONSE"),
            ("candidate", "AGENT_CANDIDATE"),
            ("audit", "AGENT_CALL_EVIDENCE"),
        ):
            row = repo.one(
                "artifacts",
                {
                    "artifact_revision_id": str(uuid5(UUID(attempt), label + ":revision")),
                    "kind": kind,
                    "work_id": work,
                },
            )
            refs[label] = row and {
                k: row[k] for k in ("artifact_id", "artifact_revision_id", "sha256")
            }
        result = {
            "attempt_id": attempt,
            "ledger_state": call["state"],
            "request_sha256": call["request_hash"],
            "artifacts": refs,
            "wire_receipt": None,
            "supplier_invoice_recorded": bool(
                repo.one(
                    "receipts",
                    {
                        "work_id": work,
                        "branch_id": branch,
                        "command_type": "P6_INVOICE",
                        "idempotency_key": attempt,
                    },
                )
            ),
        }
        try:
            wire = read_receipt(kernel.objects, attempt, call["request_hash"])
        except (ValueError, TypeError, KeyError):
            require(
                False, "INVALID_EVIDENCE", "Private receipt does not match the original request"
            )
        if wire is not None:
            require(
                refs["prompt"] and refs["prompt"]["sha256"] == call["request_hash"],
                "INVALID_EVIDENCE",
                "Missing matching authorized prompt",
            )
            envelope, body = wire
            raw_id, usage = envelope.get("receipt"), {}
            if envelope["complete"]:
                try:
                    content = json.loads(body)
                except (UnicodeDecodeError, ValueError):
                    content = None
                if isinstance(content, dict):
                    raw_id = raw_id or content.get("id")
                    stats = content.get("usage")
                    if isinstance(stats, dict):
                        usage = {
                            k: stats[k]
                            for k in (
                                "prompt_tokens",
                                "completion_tokens",
                                "prompt_cache_hit_tokens",
                                "prompt_cache_miss_tokens",
                                "total_tokens",
                            )
                            if type(stats.get(k)) is int and stats[k] >= 0
                        }
            result["wire_receipt"] = {
                "envelope_sha256": fingerprint(envelope),
                "body_sha256": envelope["body_sha256"],
                "body_bytes": len(body),
                "complete": envelope["complete"],
                "http_status": envelope["http_status"],
                "receipt_sha256": hashlib.sha256(raw_id.encode()).hexdigest()
                if isinstance(raw_id, str)
                else None,
                "transport_window": envelope["transport_window"],
                "reported_usage": usage,
            }
        return result
