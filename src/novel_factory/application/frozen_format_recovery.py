"""Bounded re-reading after a completed native editor returned malformed bytes."""
import json
from decimal import Decimal
from uuid import UUID, uuid5

from ..contracts.registry import contract_diagnostic
from ..domain.canonical import fingerprint, strict_json_loads
from ..domain.errors import DomainError
from .idea_intake import _check_design_authority
from .kernel import require


def recover(kernel, repo, gateway, owner, state, task):
    from .frozen_editing import _current_job

    job = repo.one("supervisor_jobs", {"task_id": task["task_id"]})
    require(job and job["config"].get("required_review_mcp"), "TASK_NOT_ACCEPTABLE", "Native editor recovery is not enrolled")
    returned = job["config"].get("returned_edition")
    if returned:
        from .returned_edition import _current_job as current_child_job
        current_child_job(kernel, repo, task, returned)
    else:
        _current_job(kernel, repo, task, job["config"]["frozen_editing"])
    wr = state["binding"]["world_ref"]
    _check_design_authority(repo, task, wr)
    call = repo.one("call_attempts", {"task_id": task["task_id"], "generation": task["generation"],
                                      "call_key": "supervised-candidate"})
    require(task["state"] == "FAILED" and call, "TASK_NOT_ACCEPTABLE", "No returned failed editor task")
    audit_row = gateway._read_audit(repo, call["attempt_id"])
    require(audit_row, "INVALID_EVIDENCE", "Missing rejected editor audit")
    audit = audit_row["payload"]
    host = repo.one("host_turns", {"attempt_id": call["attempt_id"]})
    require(host and host["phase"] == "COMPLETED" and call["state"] == "UNKNOWN"
            and audit["outcome"] == "REJECTED" and audit["result_status"] == "FAILED" and audit["output_ref"] is None
            and audit["reason"] in {"INVALID_ARGUMENT", "INVALID_MODEL_OUTPUT", "MODEL_PROTOCOL_ERROR", "MODEL_IDENTITY_MISMATCH"},
            "CALL_RECONCILIATION_REQUIRED", "Only completed rejected native bytes may commission a corrected reading")
    prompt = kernel.artifact(repo, audit["prompt_ref"], task["work_id"], kind="AGENT_PROMPT")["payload"]
    response = kernel.artifact(repo, audit["response_ref"], task["work_id"], kind="AGENT_RESPONSE")["payload"]
    frozen = kernel.artifact(repo, audit["input_ref"], task["work_id"], kind="AGENT_INPUT")["payload"]
    metadata = strict_json_loads(response["provider_response_json"])
    config = audit["model_configuration"]
    usage = audit["usage"]
    result = repo.one("task_results", {"task_id": task["task_id"], "generation": task["generation"],
        "result_id": task["current_result_id"], "disposition": "CURRENT"})
    audit_ref = {k: audit_row[k] for k in ("artifact_id", "artifact_revision_id", "sha256")}
    require(config.get("adapter_kind") == "codex-cli" and config.get("auth_mode") == "host-login"
            and config.get("provider") == "codex-cli" and config.get("endpoint") is None
            and config.get("isolation") == "STATELESS_TEXT_ONLY"
            and config.get("execution_kind") == audit["execution_kind"] == "REAL_HOST"
            and audit["attempt_id"] == call["attempt_id"] and audit["accounting_state"] == call["state"] == "UNKNOWN"
            and call["actual_amount"] is call["actual_tokens"] is usage["actual_amount"] is None
            and usage["cost_status"] == "UNKNOWN_COST" and response["usage"] == usage
            and fingerprint(config) == frozen["model_config_sha256"]
            and fingerprint(frozen) == audit["input_ref"]["sha256"]
            and prompt["model_configuration"] == config
            and fingerprint(prompt) == call["request_hash"] == audit["request_sha256"] == response["request_sha256"] == audit["prompt_ref"]["sha256"]
            and fingerprint(response) == audit["response_ref"]["sha256"]
            and all(audit[key]["artifact_revision_id"] == str(uuid5(UUID(call["attempt_id"]), label + ":revision"))
                    for key, label in (("prompt_ref", "prompt"), ("response_ref", "response")))
            and fingerprint(audit) == audit_ref["sha256"]
            and prompt["input_ref"] == audit["input_ref"] == job["config"]["dispatch"]["input_ref"]
            and prompt["call_key"] == call["call_key"] and prompt["budget_id"] == call["budget_id"]
            and Decimal(prompt["reserve_amount"]) == call["reserved_amount"]
            and prompt["token_reservation"] == call["reserved_tokens"]
            and all(audit[k] == task[k] == prompt[k] for k in ("task_id", "generation"))
            and audit["context_bundle_id"] == task["context_bundle_id"] and audit["world_ref"] == wr
            and metadata.get("codex_thread_id") == host["thread_id"]
            and isinstance(host["turn_id"], str) and bool(host["turn_id"])
            and metadata.get("codex_turn_id") == host["turn_id"] == call["provider_receipt"]
            == audit["provider_receipt"] == response["provider_receipt"]
            and metadata.get("auth_mode") == "host-login" and metadata.get("model") == config["model"]
            and metadata.get("billing") == "SUBSCRIPTION_AMOUNT_UNKNOWN"
            and result and result["result_id"] == str(uuid5(UUID(call["attempt_id"]), "task-result"))
            and result["payload"]["artifacts"] == [audit_ref],
            "INVALID_EVIDENCE", "The rejected editor bytes do not match their immutable source and completed Host")
    tokens_in, tokens_out, cached = (usage[key] for key in ("input_tokens", "output_tokens", "cached_input_tokens"))
    require(type(tokens_in) is int and tokens_in > 0 and type(tokens_out) is int
            and 0 < tokens_out <= config["max_output_tokens"] and tokens_in + tokens_out <= call["reserved_tokens"]
            and (cached is None or type(cached) is int and 0 <= cached <= tokens_in),
            "INVALID_EVIDENCE", "Rejected native usage must remain within its original reservation")
    try:
        envelope = strict_json_loads(response["text"])
    except DomainError:
        diagnostic = {"keyword": "jsonSyntax"}
    else:
        diagnostic = contract_diagnostic("AgentModelResponse", envelope, "agent")
        if not diagnostic and isinstance(envelope, dict) and "payload" in envelope:
            diagnostic = contract_diagnostic(frozen["output_contract"]["name"], envelope["payload"], frozen["output_contract"]["version"])
        if not diagnostic and audit["reason"] in {"MODEL_PROTOCOL_ERROR", "MODEL_IDENTITY_MISMATCH"}:
            diagnostic = {"keyword": audit["reason"]}
    require(diagnostic, "INVALID_EVIDENCE", "The recorded editor failure cannot be reproduced")
    key = ("returned:" + fingerprint([state["session"], returned["scene"], returned["phase"]])
           if returned else str(state["index"]))
    history = state.setdefault("format_history", {}).setdefault(key, [])
    used = len(history) + len(state.get("evidence_repairs", {}).get(key, [])) + len(state.get("technical_history", {}).get(key, []))
    if len(history) >= 2 or used >= 6:
        state.update(status="EDITION_REVISION_BLOCKED" if returned else "EDITING_BLOCKED",
                     reason="TECHNICAL_REPAIR_BUDGET_EXHAUSTED")
        return
    method = ("保留本任务全部原稿和判断范围。重新通过MCP读取指定来源。仅返回一个完整AgentModelResponse JSON；"
              "严格复制冻结身份，判断放在payload，字段类型和枚举遵循当前Schema。不要把审核字段放到外层；"
              "诊断：" + json.dumps(diagnostic, ensure_ascii=False))
    history.append({"task_id": task["task_id"], "generation": task["generation"], "attempt_id": call["attempt_id"],
                    "audit_ref": audit_ref, "response_ref": audit["response_ref"], "diagnostic": diagnostic})
    state.update(active=None, technical_correction={"method": method, "diagnostic": diagnostic,
        "attempt_id": call["attempt_id"], **{k: frozen[k] for k in ("runtime_sha256", "model_config_sha256", "output_contract")}})
