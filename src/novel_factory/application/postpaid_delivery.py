"""Verify completed DeepSeek transport separately from its outstanding supplier invoice."""

import json
from datetime import datetime
from decimal import Decimal
from urllib.parse import urlsplit
from uuid import UUID, uuid5

from ..domain.canonical import fingerprint
from .kernel import require


def _check(condition):
    require(
        condition,
        "CALL_RECONCILIATION_REQUIRED",
        "The returned production candidate or its accounting still requires reconciliation",
    )


def require_postpaid_delivery(kernel, repo, task, call, audit_row, response_row, candidate_row):
    """A pure evidence read: no spending, reservation reduction, invoice or provider redispatch."""
    audit = audit_row["payload"]
    config, usage, raw = audit["model_configuration"], audit["usage"], response_row["payload"]
    wr, attempt = audit["world_ref"], call["attempt_id"]
    endpoint = urlsplit(config.get("endpoint") or "")
    _check(
        config.get("defer_invoice") is True
        and config.get("adapter_kind") == "deepseek"
        and config.get("provider") == "https://api.deepseek.com"
        and endpoint.scheme == "https"
        and endpoint.hostname == "api.deepseek.com"
        and endpoint.port in (None, 443)
        and endpoint.path in {"/chat/completions", "/v1/chat/completions"}
        and not any((endpoint.username, endpoint.password, endpoint.query, endpoint.fragment))
        and config.get("isolation") == "STATELESS_TEXT_ONLY"
        and audit["execution_kind"] == config.get("execution_kind") == "REAL_EXTERNAL"
        and audit["outcome"] == "VALIDATED_CANDIDATE"
        and audit["result_status"] == "SUCCEEDED"
        and audit["accounting_state"] == call["state"] == "UNKNOWN"
        and audit["reason"] is None
        and call["actual_amount"] is None
        and call["actual_tokens"] is None
        and usage["actual_amount"] is None
        and usage["cost_status"] == "UNKNOWN_COST"
        and raw["usage"] == usage
        and isinstance(audit["provider_receipt"], str)
        and bool(audit["provider_receipt"].strip())
        and audit["provider_receipt"] == call["provider_receipt"] == raw["provider_receipt"]
        and call["call_key"] == "supervised-candidate"
        and audit["attempt_id"] == attempt
        and audit["task_id"] == task["task_id"] == call["task_id"]
        and audit["generation"] == task["generation"] == call["generation"]
        and audit["role_id"] == task["role_id"]
        and audit["context_bundle_id"] == task["context_bundle_id"]
        and (wr["work_id"], wr["branch_id"]) == (task["work_id"], task["branch_id"])
    )
    tokens_in, tokens_out, cached = (
        usage[k] for k in ("input_tokens", "output_tokens", "cached_input_tokens")
    )
    _check(
        type(tokens_in) is int
        and tokens_in > 0
        and type(tokens_out) is int
        and 0 < tokens_out <= config["max_output_tokens"]
        and tokens_in + tokens_out <= call["reserved_tokens"]
        and (cached is None or type(cached) is int and 0 <= cached <= tokens_in)
    )
    prompt = kernel.artifact(repo, audit["prompt_ref"], task["work_id"], kind="AGENT_PROMPT")[
        "payload"
    ]
    bound_input = kernel.artifact(repo, audit["input_ref"], task["work_id"], kind="AGENT_INPUT")[
        "payload"
    ]
    _check(
        fingerprint(prompt)
        == call["request_hash"]
        == audit["request_sha256"]
        == raw["request_sha256"]
        == audit["prompt_ref"]["sha256"]
        and prompt["model_configuration"] == config
        and fingerprint(bound_input) == audit["input_ref"]["sha256"]
        and bound_input["model_config_sha256"] == fingerprint(config)
        and prompt["input_ref"] == audit["input_ref"]
        and prompt["task_id"] == task["task_id"]
        and prompt["generation"] == task["generation"]
        and prompt["call_key"] == call["call_key"]
        and prompt["budget_id"] == call["budget_id"]
        and Decimal(prompt["reserve_amount"]) == call["reserved_amount"]
        and prompt["token_reservation"] == call["reserved_tokens"]
        and fingerprint(raw) == audit["response_ref"]["sha256"]
        and fingerprint(audit) == audit_row["sha256"]
        and fingerprint(candidate_row["payload"]) == audit["output_ref"]["sha256"]
        and all(
            audit[key]["artifact_revision_id"] == str(uuid5(UUID(attempt), label + ":revision"))
            for key, label in (
                ("prompt_ref", "prompt"),
                ("response_ref", "response"),
                ("output_ref", "candidate"),
            )
        )
    )
    try:
        metadata = json.loads(raw["provider_response_json"])
        window = metadata["transport_window"]
        started, received = (
            datetime.fromisoformat(window[key]) for key in ("started_at", "received_at")
        )
        envelope = json.loads(raw["text"])
        _check(
            started.utcoffset() is not None
            and received.utcoffset() is not None
            and started < received
            and isinstance(metadata["id"], str)
            and bool(metadata["id"].strip())
            and isinstance(metadata["model"], str)
            and metadata["model"] == config["model"]
            and metadata["finish_reason"] == "stop"
            and all(
                envelope[key] == audit[key]
                for key in (
                    "task_id",
                    "generation",
                    "role_id",
                    "actor_id",
                    "context_bundle_id",
                    "world_ref",
                )
            )
            and envelope["status"] == "SUCCEEDED"
            and envelope["unresolved_questions"] == []
            and envelope["payload"] == candidate_row["payload"]
        )
    except (KeyError, TypeError, ValueError, AttributeError):
        _check(False)
