"""Verify completed DeepSeek transport separately from its outstanding supplier invoice."""

import json
from datetime import datetime
from decimal import Decimal
from urllib.parse import urlsplit
from uuid import UUID, uuid5

from ..domain.canonical import fingerprint, strict_json_loads
from ..domain.errors import DomainError
from .kernel import require
from .prompt_schema import validated_payload


def _check(condition):
    require(
        condition,
        "CALL_RECONCILIATION_REQUIRED",
        "The returned production candidate or its accounting still requires reconciliation",
    )


def require_postpaid_delivery(
    kernel,
    repo,
    task,
    call,
    audit_row,
    response_row,
    candidate_row,
    *,
    design_revision_read=False,
    ending_plan_read=False,
):
    """A pure evidence read: no spending, reservation reduction, invoice or provider redispatch."""
    audit = audit_row["payload"]
    _check(
        audit["outcome"] == "VALIDATED_CANDIDATE"
        and audit["result_status"] == "SUCCEEDED"
        and audit["reason"] is None
        and fingerprint(candidate_row["payload"]) == audit["output_ref"]["sha256"]
        and audit["output_ref"]["artifact_revision_id"]
        == str(uuid5(UUID(call["attempt_id"]), "candidate:revision"))
    )
    envelope, bound_input = verify_postpaid_transport(
        kernel,
        repo,
        task,
        call,
        audit_row,
        response_row,
        design_revision_read=design_revision_read,
        ending_plan_read=ending_plan_read,
    )
    _check(
        validated_payload(
            envelope,
            bound_input["output_contract"]["name"],
            bound_input["output_contract"]["version"],
        )
        == candidate_row["payload"]
    )


def bound_ending_input(repo, task, input_ref, contract):
    """Only a registered ending-plan task may hand unresolved questions to its dedicated reviewer."""
    if (task["role_id"], task["task_kind"]) != (
        "world_designer",
        "initial_world",
    ) or contract not in (
        {"name": "ShortEndingPlan", "version": "production_profile"},
        {"name": "ShortEndingFix", "version": "production_profile"},
        {"name": "OpeningWorldDesign", "version": "production_profile"},
        {"name": "EnvironmentalOpeningWorldDesign", "version": "production_profile"},
    ):
        return False
    rounds = repo.many(
        "receipts",
        {
            "work_id": task["work_id"],
            "branch_id": task["branch_id"],
            "command_type": "P6_IDEA_REVISE",
        },
    )
    purpose = "SHORT_ENDING_FIX" if contract["name"] == "ShortEndingFix" else "SHORT_ENDING"
    return any(
        r["payload"].get("revision_purpose") == purpose
        and r["payload"]["task_id"] == task["task_id"]
        and r["payload"]["input_ref"] == input_ref
        for r in rounds
    )


def _transport_identity(envelope, audit, task, *, structural_source):
    fields = ("task_id", "generation", "role_id", "actor_id", "context_bundle_id", "world_ref")
    identity = envelope
    if structural_source and "identity" in envelope:
        _check(
            audit["outcome"] == "REJECTED"
            and audit["output_ref"] is None
            and task["state"] == "FAILED"
            and (task["role_id"], task["task_kind"]) == ("world_designer", "initial_world")
            and set(envelope) == {"identity", "status", "payload", "unresolved_questions"}
            and set(envelope["identity"]) == {*fields, "schema_version"}
            and envelope["identity"]["schema_version"] == "2.1"
        )
        identity = envelope["identity"]
    return all(identity[key] == audit[key] for key in fields)


def verify_postpaid_transport(
    kernel,
    repo,
    task,
    call,
    audit_row,
    response_row,
    *,
    design_revision_read=False,
    ending_plan_read=False,
    structural_source=False,
    syntax_source=False,
    contract_source=False,
    board_node=None,
    opening_slot=None,
    opening_director=False,
    source_result=None,
):
    audit = audit_row["payload"]
    config, usage, raw = audit["model_configuration"], audit["usage"], response_row["payload"]
    wr, attempt = audit["world_ref"], call["attempt_id"]
    endpoint = urlsplit(config.get("endpoint") or "")
    if source_result is not None:
        # A controller's derivation may preserve the original FAILED row while
        # installing a separate current result. Prove that exact source row,
        # never a fabricated copy of the task's previous state.
        saved = repo.one("task_results", {"result_id": source_result["result_id"],
                         "task_id": task["task_id"], "generation": task["generation"]})
        audit_ref = {key: audit_row[key] for key in ("artifact_id", "artifact_revision_id", "sha256")}
        _check(syntax_source and not contract_source and not structural_source and board_node is not None
               and task["role_id"] == "writer" and saved == source_result
               and saved["result_id"] == str(uuid5(UUID(attempt), "task-result"))
               and saved["payload_hash"] == fingerprint(saved["payload"])
               and saved["payload"]["status"] == "FAILED" and saved["payload"]["artifacts"] == [audit_ref])
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
        and audit["accounting_state"] == call["state"] == "UNKNOWN"
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
        and all(
            audit[key]["artifact_revision_id"] == str(uuid5(UUID(attempt), label + ":revision"))
            for key, label in (
                ("prompt_ref", "prompt"),
                ("response_ref", "response"),
            )
        )
    )
    try:
        metadata = json.loads(raw["provider_response_json"])
        window = metadata["transport_window"]
        started, received = (
            datetime.fromisoformat(window[key]) for key in ("started_at", "received_at")
        )
        _check(started.utcoffset() is not None and received.utcoffset() is not None and started < received
               and isinstance(metadata["id"], str) and bool(metadata["id"].strip())
               and metadata["model"] == config["model"] and (metadata["finish_reason"] == "stop" or contract_source and metadata["finish_reason"] == "length"))
        design_scope = (task["role_id"], task["task_kind"]) in {("world_designer", "initial_world"), ("reviewer", "fact")}
        if board_node is not None:
            from .production_pipeline import guard_send
            job = repo.one("supervisor_jobs", {"task_id": task["task_id"]})
            _check(job and job["config"].get("production_node") == board_node and
                   job["config"]["dispatch"]["input_ref"] == audit["input_ref"] and (syntax_source or contract_source))
            if source_result is None:
                board, _, _ = guard_send(kernel, repo, task, job["config"])
                _check(board.get("production_mode") == "quality")
            else:
                # The administrative derivation separately proves the frozen
                # source board and current authority. Transport replay remains
                # valid after that board has advanced to another window.
                _check(bound_input["role_id"] == "writer" and bound_input["output_contract"] == {
                    "name": "AgentTextOutput", "version": "agent"})
        if opening_slot is not None:
            interaction_id, actor = opening_slot
            group = repo.one("simulation_interactions", {"work_id": task["work_id"], "branch_id": task["branch_id"],
                                                          "interaction_id": interaction_id})
            job = repo.one("supervisor_jobs", {"task_id": task["task_id"]})["config"]
            _check((syntax_source or contract_source) and (task["role_id"], task["task_kind"]) ==
                   ("character_actor", "character_action") and job.get("seal_interaction_id") == interaction_id
                   and group and group["payload"]["base_world_ref"] == wr and
                   {"actor_id": actor, "grant_id": task["grant_id"]} in group["payload"]["participant_slots"]
                   and audit["actor_id"] == actor and audit["input_ref"] == job["dispatch"]["input_ref"])
        if opening_director:
            from .opening_recovery import verify_director_binding
            _check((syntax_source or contract_source) and verify_director_binding(kernel, repo, task, audit["input_ref"]) == wr)
        if contract_source:
            _check(task["state"] == "FAILED" and (design_scope or board_node is not None or opening_director or opening_slot is not None)
                   and audit["outcome"] == "REJECTED" and audit["result_status"] == "FAILED" and audit["output_ref"] is None
                   and audit["reason"] in {"INVALID_ARGUMENT", "MODEL_PROTOCOL_ERROR", "MODEL_IDENTITY_MISMATCH"})
            from .idea_repair import structured_diagnostic
            _check(structured_diagnostic(raw, audit, bound_input))
            return None, bound_input
        if syntax_source:
            _check((task["state"] == "FAILED" or source_result is not None)
                   and (design_scope or board_node is not None or opening_director or opening_slot is not None)
                   and audit["outcome"] == "REJECTED" and audit["result_status"] == "FAILED"
                   and audit["reason"] == "INVALID_MODEL_OUTPUT" and audit["output_ref"] is None)
            from .idea_repair import unparsed_diagnostic
            try:
                strict_json_loads(raw["text"])
            except DomainError as error:
                # The supplier returned the bytes. Neither invalid syntax nor duplicate keys
                # yield an unambiguous candidate or a model assertion about identity.
                if unparsed_diagnostic(error) is not None:
                    return None, bound_input
            _check(False)
        from .writer_format_normalization import replay_response

        envelope = replay_response(kernel, repo, task, audit, response_row)
        if structural_source:
            _check(
                task["state"] == "FAILED"
                and audit["outcome"] == "REJECTED"
                and (task["role_id"], task["task_kind"]) == ("world_designer", "initial_world")
                and any(
                    r["payload"]["design_task_id"] == task["task_id"]
                    and r["payload"]["candidate_ref"] == audit["response_ref"]
                    and "review_ref" not in r["payload"]
                    for r in repo.many(
                        "receipts",
                        {
                            "work_id": task["work_id"],
                            "branch_id": task["branch_id"],
                            "command_type": "P6_IDEA_REVISE",
                        },
                    )
                )
            )
        _check(
            _transport_identity(envelope, audit, task, structural_source=structural_source)
            and envelope["status"] == "SUCCEEDED"
            and (
                envelope["unresolved_questions"] == []
                or structural_source
                or (
                    design_revision_read
                    and task["role_id"] == "reviewer"
                    and task["task_kind"] == "fact"
                    and bound_input["output_contract"]["name"] == "ShortDesignReview"
                    and bound_input["output_contract"]["version"] == "production_profile"
                    and envelope["payload"]["short_arc"]["decision"] == "ENDING_PLAN_REQUIRED"
                )
                or (
                    ending_plan_read
                    and bound_ending_input(
                        repo, task, audit["input_ref"], bound_input["output_contract"]
                    )
                )
            )
        )
    except (KeyError, TypeError, ValueError, AttributeError):
        _check(False)
    return envelope, bound_input
