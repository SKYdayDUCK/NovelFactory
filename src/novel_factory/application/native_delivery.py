"""Adopt a completed native-login result while retaining unknown subscription cost."""

from decimal import Decimal
from uuid import UUID, uuid5

from novel_factory.domain.canonical import fingerprint, strict_json_loads

from .postpaid_delivery import _check, _transport_identity, bound_ending_input
from .prompt_schema import validated_payload


def require_native_delivery(kernel, repo, task, call, audit_row, response_row, candidate_row,
                            *, design_revision_read=False, ending_plan_read=False, needs_input_read=False,
                            allow_unresolved_review=False):
    """Verify frozen bytes and the trusted Host journal; do not settle or redispatch."""
    audit, response, candidate = audit_row["payload"], response_row["payload"], candidate_row["payload"]
    config, usage = audit["model_configuration"], audit["usage"]
    attempt = call["attempt_id"]
    host = repo.one("host_turns", {"attempt_id": attempt})
    task_input = repo.one("task_inputs", {"task_id": task["task_id"], "generation": task["generation"]})
    expected_status = "NEEDS_INPUT" if needs_input_read else "SUCCEEDED"
    _check(type(allow_unresolved_review) is bool and not (needs_input_read and allow_unresolved_review))
    if allow_unresolved_review:
        # An administrator may verify this completed return to commission a new
        # independent review. This proof never approves or delivers the old one.
        _check(task["role_id"] == "reviewer" and task["task_kind"] in {"literary", "epistemic"}
               and task["state"] == "RESULT_STORED"
               and task["current_result_id"] == str(uuid5(UUID(attempt), "task-result")))
    if needs_input_read:
        # Only an administrator's independent fact-reading recovery uses this
        # proof. It is never an approval or a successful candidate delivery.
        _check((task["role_id"], task["task_kind"]) == ("evidence_maintainer", "assertion_extract")
               and task["state"] == "BLOCKED" and task["current_result_id"])
    _check(config.get("adapter_kind") == "codex-cli" and config.get("auth_mode") == "host-login"
           and config.get("provider") == "codex-cli" and config.get("endpoint") is None
           and audit["execution_kind"] == config["execution_kind"] == "REAL_HOST"
           and config.get("isolation") == "STATELESS_TEXT_ONLY"
           and audit["outcome"] == "VALIDATED_CANDIDATE" and audit["result_status"] == expected_status
           and audit["reason"] is None and host and host["phase"] == "COMPLETED"
           and call["state"] == audit["accounting_state"] == "UNKNOWN"
           and call["actual_amount"] is call["actual_tokens"] is usage["actual_amount"] is None
           and usage["cost_status"] == "UNKNOWN_COST" and response["usage"] == usage
           and call["call_key"] == "supervised-candidate"
           and audit["attempt_id"] == attempt
           and audit["task_id"] == task["task_id"] == call["task_id"]
           and audit["generation"] == task["generation"] == call["generation"]
           and audit["context_bundle_id"] == task["context_bundle_id"]
           and task_input and audit["world_ref"] == task_input["spec"]["world_ref"]
           and (audit["world_ref"]["work_id"], audit["world_ref"]["branch_id"])
           == (task["work_id"], task["branch_id"]))
    tokens_in, tokens_out, cached = (usage[k] for k in ("input_tokens", "output_tokens", "cached_input_tokens"))
    _check(type(tokens_in) is int and tokens_in > 0 and type(tokens_out) is int
           and 0 < tokens_out <= config["max_output_tokens"]
           and tokens_in + tokens_out <= call["reserved_tokens"]
           and (cached is None or type(cached) is int and 0 <= cached <= tokens_in))
    prompt = kernel.artifact(repo, audit["prompt_ref"], task["work_id"], kind="AGENT_PROMPT")["payload"]
    bound_input = kernel.artifact(repo, audit["input_ref"], task["work_id"], kind="AGENT_INPUT")["payload"]
    _check(fingerprint(prompt) == call["request_hash"] == audit["request_sha256"]
           == response["request_sha256"] == audit["prompt_ref"]["sha256"]
           and prompt["model_configuration"] == config
           and bound_input["model_config_sha256"] == fingerprint(config)
           and fingerprint(bound_input) == audit["input_ref"]["sha256"]
           and prompt["input_ref"] == audit["input_ref"]
           and prompt["task_id"] == task["task_id"] and prompt["generation"] == task["generation"]
           and prompt["call_key"] == call["call_key"] and prompt["budget_id"] == call["budget_id"]
           and Decimal(prompt["reserve_amount"]) == call["reserved_amount"]
           and prompt["token_reservation"] == call["reserved_tokens"]
           and fingerprint(response) == audit["response_ref"]["sha256"]
           and fingerprint(audit) == audit_row["sha256"]
           and fingerprint(candidate) == audit["output_ref"]["sha256"]
           and all(audit[key]["artifact_revision_id"] == str(uuid5(UUID(attempt), label + ":revision"))
                   for key, label in (("prompt_ref", "prompt"), ("response_ref", "response"), ("output_ref", "candidate"))))
    metadata = strict_json_loads(response["provider_response_json"])
    _check(metadata.get("auth_mode") == "host-login" and metadata.get("model") == config["model"]
           and metadata.get("billing") == "SUBSCRIPTION_AMOUNT_UNKNOWN"
           and metadata.get("codex_thread_id") == host["thread_id"]
           and isinstance(host["turn_id"], str) and bool(host["turn_id"])
           and metadata.get("codex_turn_id") == host["turn_id"]
           == audit["provider_receipt"] == response["provider_receipt"] == call["provider_receipt"])
    envelope = strict_json_loads(response["text"])
    if allow_unresolved_review:
        _check(bool(envelope["unresolved_questions"])
               and bound_input["output_contract"] == {"name": "AgentReviewOutput", "version": "agent"})
    _check(_transport_identity(envelope, audit, task, structural_source=False)
           and envelope["status"] == expected_status
           and ((needs_input_read or allow_unresolved_review) and bool(envelope["unresolved_questions"])
                or not needs_input_read and not envelope["unresolved_questions"] or design_revision_read
                and (task["role_id"], task["task_kind"]) == ("reviewer", "fact")
                and bound_input["output_contract"] == {"name": "ShortDesignReview", "version": "production_profile"}
                and envelope["payload"].get("short_arc", {}).get("decision") == "ENDING_PLAN_REQUIRED"
                or ending_plan_read and bound_ending_input(repo, task, audit["input_ref"], bound_input["output_contract"])))
    _check(validated_payload(envelope, bound_input["output_contract"]["name"],
                             bound_input["output_contract"]["version"]) == candidate)
