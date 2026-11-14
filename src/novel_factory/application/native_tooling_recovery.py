"""Bounded replacement of completed native reads that used no required tools.

An administrative post-fix authorization permits one new reader, never adoption
of the missing-tool candidate, settlement, or redispatch of its original call.
"""
import hashlib
import re
from pathlib import Path
from uuid import UUID, uuid5

from sqlalchemy import text

from ..contracts.registry import validate_contract
from ..domain.canonical import fingerprint, strict_json_loads
from ..domain.errors import DomainError
from .candidate_delivery import CandidateDelivery
from .kernel import require
from .native_delivery import require_native_delivery
from .scene_revision import _archive
from .technical_repair_budget import technical_repair_budget
from .volume_successor import _selected

COMMAND = "NATIVE_REVIEW_TOOL_RECOVERY"
AUTHORIZATION = "NATIVE_REVIEW_TOOL_REPAIR_AUTHORIZATION"
REASON = "NATIVE_REVIEW_TOOLS_UNUSED"
EXHAUSTED = "NATIVE_REVIEW_TOOL_REPAIR_EXHAUSTED"
ROLES = {"review": ("reviewer", "literary"), "epistemic": ("reviewer", "epistemic"),
         "facts": ("evidence_maintainer", "assertion_extract"), "internal_verification": ("reviewer", "verification")}
LEGACY_CANCEL_REASON = "Author-authorized abandonment of orphaned read; retain UNKNOWN cost and fence late output"


def _transport_revision():
    from agent_runtime.native_review import REVIEW_TRANSPORT_REVISION, review_host_options
    options = review_host_options()
    require("features.code_mode=true" in options and "features.code_mode_host=true" in options,
            "HOST_ISOLATION_UNVERIFIED", "The fixed task-bound tool transport is not installed")
    return REVIEW_TRANSPORT_REVISION


def _runtime_sources():
    import agent_runtime.native_codex
    import agent_runtime.native_review
    return {module.__name__: hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
            for module in (agent_runtime.native_codex, agent_runtime.native_review)}


def _runtime_binary_sha256():
    from agent_runtime.host_process import resolve_host_binary
    path = resolve_host_binary()
    require(path is not None, "HOST_ISOLATION_UNVERIFIED", "The pinned native executable is unavailable")
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def authorize_host_repair(kernel, gateway, principal, world_ref, *, probe_task_id, source_revision, apply=False,
                          probe_kernel=None, probe_principal=None, probe_certificate=None):
    """A real successful scoped probe authorizes this work's single repair family."""
    from . import production_pipeline as boards
    from .planning import PlanningService
    from .review_mcp import require_review_queries
    validate_contract("WorldRef", world_ref)
    validate_contract("UUID", probe_task_id)
    require(type(apply) is bool and isinstance(source_revision, str) and re.fullmatch(r"[0-9a-f]{40}", source_revision),
            "INVALID_ARGUMENT", "Exact fixed source revision and explicit apply flag required")
    revision = _transport_revision()
    # Internal server collaboration only. External probe references are a
    # transport certificate, never imported as this novel's world evidence.
    probe_kernel = probe_kernel or kernel
    probe_principal = probe_principal or principal
    probe_gateway = gateway.with_kernel(probe_kernel)
    with probe_kernel.db.uow(write=False) as probe_repo:
        probe = probe_repo.one("tasks", {"task_id": probe_task_id})
        require(probe, "INVALID_EVIDENCE", "A real completed probe task is required")
        probe_grant = probe_kernel.authorize(probe_repo, probe_principal, probe["work_id"], "ADMIN", probe["branch_id"])
        audit, _ = CandidateDelivery(probe_kernel, probe_gateway).audited_candidate(probe_repo, probe, production_accounting=True)
        require_review_queries(probe_kernel, probe_repo, probe, audit)
        response = probe_kernel.artifact(probe_repo, audit["response_ref"], probe["work_id"], kind="AGENT_RESPONSE")["payload"]
        metadata = strict_json_loads(response["provider_response_json"])
        require(audit["model_configuration"].get("adapter_kind") == "codex-cli"
                and metadata.get("native_review_transport_revision") == revision,
                "REVIEW_QUERY_REQUIRED", "The repaired transport needs its own actually completed scoped MCP probe")
        require(isinstance(probe_certificate, dict)
                and set(probe_certificate) == {"binary_sha256", "source_sha256"}
                and isinstance(probe_certificate.get("binary_sha256"), str)
                and re.fullmatch(r"[0-9a-f]{64}", probe_certificate["binary_sha256"])
                and probe_certificate["binary_sha256"] == _runtime_binary_sha256()
                and probe_certificate.get("source_sha256") == _runtime_sources(),
                "INVALID_EVIDENCE", "The server's probe certificate must bind the pinned binary and current transport source bytes")
        probe_evidence = probe_gateway._read_audit(probe_repo, audit["attempt_id"])
        certificate = {"scope": "EXTERNAL_TOOL_TRANSPORT_ONLY", "probe_world_ref": audit["world_ref"],
            "probe_evidence_ref": {key: probe_evidence[key] for key in ("artifact_id", "artifact_revision_id", "sha256")},
            "probe_response_ref": audit["response_ref"], "probe_input_ref": audit["input_ref"],
            "probe_controller": {"grant_id": probe_principal.grant_id, "principal_id": probe_principal.principal_id,
                                 "epoch": probe_grant["epoch"]}, **probe_certificate}
    with kernel.objects.lock(), kernel.db.uow() as repo:
        repo.one("works", {"work_id": world_ref["work_id"]}, lock="update")
        grant = kernel.authorize(repo, principal, world_ref["work_id"], "ADMIN", world_ref["branch_id"])
        board = boards._load(kernel, repo, world_ref["work_id"], world_ref["branch_id"])
        require(board and boards._current_owner(kernel, repo, board) == principal,
                "VIEW_DENIED", "Only the enrolled controller may authorize this repair")
        intent = {"world_ref": world_ref, "probe_task_id": probe_task_id, "probe_attempt_id": audit["attempt_id"],
                  "source_revision": source_revision, "transport_revision": revision, "probe_certificate": certificate}
        command = "AUTHORIZE_NATIVE_REVIEW_TOOL_REPAIR"
        prior = kernel._prior(repo, principal, world_ref["work_id"], command, revision, intent)
        if prior:
            return prior
        payload = {**intent, "controller": {"grant_id": principal.grant_id, "principal_id": principal.principal_id,
                                           "epoch": grant["epoch"]},
                   "policy_version": repo.one("works", {"work_id": world_ref["work_id"]})["policy_version"],
                   "probe_input_ref": audit["input_ref"], "probe_response_ref": audit["response_ref"]}
        result = {"status": "NATIVE_TOOL_REPAIR_AUTHORIZATION_PREVIEW", "provider_calls_sent": 0, "transport_revision": revision}
        if apply:
            ref = boards._artifact(kernel, repo, world_ref["work_id"], principal, AUTHORIZATION, payload)
            PlanningService(kernel)._select(repo, world_ref, AUTHORIZATION + ":" + world_ref["branch_id"], ref)
            result.update(status="NATIVE_TOOL_REPAIR_AUTHORIZED", authorization_ref=ref)
            kernel._receipt(repo, principal, world_ref, command, revision, intent, result)
        return result


def completed_toolless_reading(kernel, repo, gateway, task):
    """Classify a protocol failure; model prose cannot establish host root cause."""
    job = repo.one("supervisor_jobs", {"task_id": task["task_id"]})
    if (not job or not job["config"].get("required_review_mcp") or task["state"] != "BLOCKED"
            or (task["role_id"], task["task_kind"]) not in ROLES.values()):
        return None
    call = repo.one("call_attempts", {"task_id": task["task_id"], "generation": task["generation"], "call_key": "supervised-candidate"})
    audit = call and gateway._read_audit(repo, call["attempt_id"])
    if (not audit or audit["payload"].get("model_configuration", {}).get("adapter_kind") != "codex-cli"
            or audit["payload"].get("outcome") != "VALIDATED_CANDIDATE"
            or audit["payload"].get("result_status") != "NEEDS_INPUT"
            or not audit["payload"].get("response_ref") or not audit["payload"].get("output_ref")):
        return None
    evidence = audit["payload"]
    response = kernel.artifact(repo, evidence["response_ref"], task["work_id"], kind="AGENT_RESPONSE")
    metadata = strict_json_loads(response["payload"]["provider_response_json"])
    if metadata.get("review_mcp_receipt_refs") != []:
        return None
    actual = repo.conn.scalar(text("""SELECT count(*) FROM nf_audit_events
        WHERE task_id=:task AND generation=:generation AND event_type='mcp.started'
          AND tool_name IN ('nf_context_get','nf_query_batch','nf_review_material')"""),
        {"task": task["task_id"], "generation": task["generation"]})
    require(actual == 0, "INVALID_EVIDENCE", "A returned empty tool list conflicts with actual native invocations")
    result = repo.one("task_results", {"result_id": task["current_result_id"], "task_id": task["task_id"],
                                      "generation": task["generation"], "disposition": "CURRENT"})
    audit_ref = {key: audit[key] for key in ("artifact_id", "artifact_revision_id", "sha256")}
    require(result and result["result_id"] == str(uuid5(UUID(call["attempt_id"]), "task-result"))
            and result["payload"]["status"] == evidence["result_status"]
            and result["payload"]["artifacts"] == [audit_ref, evidence["output_ref"]],
            "INVALID_EVIDENCE", "The missing-tool return is not this task's immutable current result")
    require_native_delivery(kernel, repo, task, call, audit, response,
        kernel.artifact(repo, evidence["output_ref"], task["work_id"], kind="AGENT_CANDIDATE"), tooling_recovery_read=True)
    return {"code": REASON, "attempt_id": call["attempt_id"], "evidence_ref": audit_ref,
            "input_ref": evidence["input_ref"], "response_ref": evidence["response_ref"], "candidate_ref": evidence["output_ref"],
            "accounting_state": "UNKNOWN", "required_tools_used": 0}


def recover_toolless_native_reading(kernel, repo, gateway, owner, board, identity):
    """Transaction-local, send-free, bounded replacement for the selected node."""
    from . import production_pipeline as boards
    node = board["nodes"][identity]
    if node.get("stage") not in ROLES or not node.get("task_id") or node.get("output_ref"):
        return False
    task = repo.one("tasks", {"task_id": node["task_id"]}, lock="update")
    previous = (node.get("state"), node.get("reason"), node.get("tooling_failure"), node.get("tooling_wait"))
    try:
        diagnosis = task and completed_toolless_reading(kernel, repo, gateway, task)
    except DomainError as error:
        if error.code not in {"CALL_RECONCILIATION_REQUIRED", "INVALID_EVIDENCE"}:
            raise
        node.update(state="STOPPED", reason=error.code,
                    tooling_failure={"code": error.code, "task_id": task["task_id"], "generation": task["generation"]})
        return previous != (node.get("state"), node.get("reason"), node.get("tooling_failure"), node.get("tooling_wait"))
    if diagnosis is None:
        return False
    node["tooling_failure"] = diagnosis
    wr = board["world_ref"]
    require(boards._current_owner(kernel, repo, board) == owner and ROLES[node["stage"]] == (task["role_id"], task["task_kind"]),
            "VIEW_DENIED", "Only the current controller may replace the exact enrolled reader")
    authorization_ref = _selected(repo, wr["work_id"], AUTHORIZATION + ":" + wr["branch_id"])
    if not authorization_ref:
        node.update(state="STOPPED", reason=REASON)
        node.pop("tooling_wait", None)
        return previous != (node.get("state"), node.get("reason"), node.get("tooling_failure"), node.get("tooling_wait"))
    authorization = kernel.artifact(repo, authorization_ref, wr["work_id"], kind=AUTHORIZATION)["payload"]
    grant = kernel.authorize(repo, owner, wr["work_id"], "ADMIN", wr["branch_id"])
    require(authorization["controller"] == {"grant_id": owner.grant_id, "principal_id": owner.principal_id, "epoch": grant["epoch"]}
            and authorization["transport_revision"] == _transport_revision()
            and authorization["policy_version"] == repo.one("works", {"work_id": wr["work_id"]})["policy_version"]
            and authorization["probe_certificate"]["source_sha256"] == _runtime_sources()
            and authorization["probe_certificate"]["binary_sha256"] == _runtime_binary_sha256()
            and all(authorization["world_ref"][key] == wr[key] for key in ("work_id", "branch_id")),
            "TASK_REBIND_REQUIRED", "The post-fix authorization no longer matches this controller or transport")
    authorization_intent = {key: authorization[key] for key in ("world_ref", "probe_task_id", "probe_attempt_id", "source_revision", "transport_revision", "probe_certificate")}
    require(any(row["principal_id"] == owner.principal_id and row["request_hash"] == fingerprint(authorization_intent)
                and row["payload"].get("authorization_ref") == authorization_ref
                for row in repo.many("receipts", {"work_id": wr["work_id"], "branch_id": wr["branch_id"],
                                                "command_type": "AUTHORIZE_NATIVE_REVIEW_TOOL_REPAIR"})),
            "INVALID_EVIDENCE", "The post-fix authorization has no actual administrative receipt")
    if any(row.get("family") == COMMAND for row in node.get("evidence_repairs", [])) or technical_repair_budget(node):
        node.update(state="STOPPED", reason=EXHAUSTED)
        node.pop("tooling_wait", None)
        return previous != (node.get("state"), node.get("reason"), node.get("tooling_failure"), node.get("tooling_wait"))
    active_readers = 0
    for key, other in board["nodes"].items():
        if key != identity and other["state"] == "ENROLLED" and other["stage"] in {*ROLES, "reassessment"}:
            other_task = repo.one("tasks", {"task_id": other.get("task_id")})
            active_readers += bool(other_task and other_task["state"] in {"READY", "RUNNING", "RESULT_STORED", "VALIDATING"})
    draining = repo.conn.scalar(text("""SELECT count(*) FROM nf_call_attempts c JOIN nf_tasks t USING(task_id)
        WHERE t.work_id=:work AND t.branch_id=:branch AND c.state IN ('PREPARED','SENT','RUNNING')"""),
        {"work": wr["work_id"], "branch": wr["branch_id"]})
    if active_readers >= board["limits"]["review"] or draining:
        node.update(state="STOPPED", reason=REASON,
                    tooling_wait="READING_SLOT" if active_readers >= board["limits"]["review"] else "ACTIVE_CALL_DRAIN")
        return previous != (node.get("state"), node.get("reason"), node.get("tooling_failure"), node.get("tooling_wait"))
    job = repo.one("supervisor_jobs", {"task_id": task["task_id"]})["config"]
    old_input = kernel.artifact(repo, diagnosis["input_ref"], wr["work_id"], kind="AGENT_INPUT")["payload"]
    old_spec = repo.one("task_inputs", {"task_id": task["task_id"], "generation": task["generation"]})["spec"]
    repair = {"family": COMMAND, **diagnosis, "task_id": task["task_id"], "generation": task["generation"],
              "authorization_ref": authorization_ref, "method": "同稿独立重读；先实际调用本任务MCP读取指定材料与系统数据。宿主工具缺失不等于小说缺少作者输入。"}
    intent = {"old_task_id": task["task_id"], "old_generation": task["generation"], "old_attempt_id": diagnosis["attempt_id"],
              "node_id": identity, "authorization_ref": authorization_ref, "input_ref": diagnosis["input_ref"]}
    cancellation = kernel.tasks.cancel(task["task_id"], task["generation"], principal_id=owner.principal_id,
                        idempotency_key=COMMAND + ":" + diagnosis["attempt_id"], reason="Superseded completed tool-less native read; old UNKNOWN accounting and output retained")
    node.setdefault("evidence_repairs", []).append(repair)
    _archive(node)
    node["reason"] = "REVIEW_EVIDENCE_REREAD"
    node.pop("input_requirement", None)
    node.pop("tooling_failure", None)
    node.pop("tooling_wait", None)
    boards._enroll(kernel, repo, gateway, owner, board, identity, frozen_assignment=old_input["assignment"])
    successor = repo.one("tasks", {"task_id": node["task_id"]})
    new_job = repo.one("supervisor_jobs", {"task_id": node["task_id"]})["config"]
    new_input = kernel.artifact(repo, new_job["dispatch"]["input_ref"], wr["work_id"], kind="AGENT_INPUT")["payload"]
    new_spec = repo.one("task_inputs", {"task_id": successor["task_id"], "generation": successor["generation"]})["spec"]
    require(successor["principal_id"] != task["principal_id"] and successor["grant_id"] != task["grant_id"]
            and all(old_input[key] == new_input[key] for key in ("role_id", "task_kind", "material_refs", "output_contract", "runtime_sha256", "model_config_sha256", "assignment"))
            and all(old_spec[key] == new_spec[key] for key in ("world_ref", "story_time"))
            and job["dispatch"]["budget_id"] == new_job["dispatch"]["budget_id"] and new_job.get("required_review_mcp"),
            "TASK_REBIND_REQUIRED", "A tooling repair may not change the manuscript, cut, model, contract or budget")
    result = {"status": "NATIVE_TOOL_READING_ENROLLED", **intent, "intent": intent, "new_task_id": successor["task_id"],
              "new_input_ref": new_job["dispatch"]["input_ref"], "source_evidence_ref": diagnosis["evidence_ref"],
              "source_response_ref": diagnosis["response_ref"], "accounting_state": "OLD_LEDGERS_PRESERVED", "provider_calls_sent": 0,
              "cancellation": cancellation, "cancelled_generation": repo.one("tasks", {"task_id": task["task_id"]})["generation"],
              "cancelled_result_id": repo.one("tasks", {"task_id": task["task_id"]})["current_result_id"]}
    kernel._receipt(repo, owner, wr, COMMAND, diagnosis["attempt_id"], intent, result)
    return True


def _completed_successor(kernel, repo, gateway, board, draft_ref, *, stage=None, task_id=None):
    candidates = [(node["stage"], record) for node in board["nodes"].values()
                  if stage is None or node["stage"] == stage
                  for record in [node, *node.get("history", [])]
                  if record.get("task_id") and (task_id is None or record["task_id"] == task_id)]
    for source_stage, record in candidates:
        task = repo.one("tasks", {"task_id": record["task_id"]})
        if not task or task["state"] != "ACCEPTED":
            continue
        require(ROLES.get(source_stage) == (task["role_id"], task["task_kind"])
                and all(task[key] == board["world_ref"][key] for key in ("work_id", "branch_id"))
                and ("generation" not in record or record["generation"] == task["generation"]),
                "INVALID_EVIDENCE", "The administrative successor changed its original stage, generation or branch")
        job = repo.one("supervisor_jobs", {"task_id": task["task_id"]})
        if not job or not job["config"].get("required_review_mcp"):
            continue
        evidence, _ = CandidateDelivery(kernel, gateway).audited_candidate(repo, task, production_accounting=True)
        from .review_mcp import require_review_queries
        require_review_queries(kernel, repo, task, evidence)
        frozen = kernel.artifact(repo, evidence["input_ref"], task["work_id"], kind="AGENT_INPUT")["payload"]
        if draft_ref in frozen["material_refs"]:
            return task
    return None


def verify_superseded_tooling_return(kernel, repo, gateway, board, task, call):
    """Narrow _register exception; CANCELLED/UNKNOWN alone is never sufficient."""
    if task["state"] != "CANCELLED" or call["state"] != "UNKNOWN" or call["actual_amount"] is not None or call["actual_tokens"] is not None:
        return False
    work, branch = task["work_id"], task["branch_id"]
    for receipt in repo.many("receipts", {"work_id": work, "branch_id": branch, "command_type": COMMAND}):
        value = receipt["payload"]
        if value["old_task_id"] != task["task_id"] or value["old_attempt_id"] != call["attempt_id"]:
            continue
        cancellation = repo.one("task_commands", {"principal_id": receipt["principal_id"],
                                                  "idempotency_key": COMMAND + ":" + call["attempt_id"]})
        require(receipt["request_hash"] == fingerprint(value["intent"]) and value["old_generation"] == call["generation"]
                and value["cancelled_generation"] == task["generation"] and value["cancelled_result_id"] == task["current_result_id"]
                and cancellation and cancellation["receipt"] == value["cancellation"]
                and value["cancellation"]["generation"] == value["old_generation"]
                and value["cancellation"]["status"] == "CANCELLED"
                and value["cancellation"]["task_id"] == task["task_id"],
                "INVALID_EVIDENCE", "Native repair receipt differs from its cancelled source")
        audit = gateway._read_audit(repo, call["attempt_id"])
        require(audit and value["source_evidence_ref"] == {key: audit[key] for key in ("artifact_id", "artifact_revision_id", "sha256")}
                and value["source_response_ref"] == audit["payload"]["response_ref"], "INVALID_EVIDENCE", "Superseded return lost its immutable transport proof")
        source = audit["payload"]
        require_native_delivery(kernel, repo, task, call, audit,
            kernel.artifact(repo, source["response_ref"], work, kind="AGENT_RESPONSE"),
            kernel.artifact(repo, source["output_ref"], work, kind="AGENT_CANDIDATE"), tooling_recovery_read=True)
        old_input = kernel.artifact(repo, value["input_ref"], work, kind="AGENT_INPUT")["payload"]
        new_input = kernel.artifact(repo, value["new_input_ref"], work, kind="AGENT_INPUT")["payload"]
        require(all(old_input[key] == new_input[key] for key in ("role_id", "task_kind", "material_refs", "output_contract", "runtime_sha256", "model_config_sha256")),
                "INVALID_EVIDENCE", "Superseded return no longer has its exact independent successor")
        drafts = [ref for ref in old_input["material_refs"] if kernel.artifact(repo, ref, work)["kind"] == "NARRATIVE_DRAFT"]
        return bool(drafts and _completed_successor(kernel, repo, gateway, board, drafts[0], task_id=value["new_task_id"]))
    # A recorded legacy MCP migration can also fence an abandoned generation.
    from .reading_mcp_upgrade import COMMAND as UPGRADE
    for receipt in repo.many("receipts", {"work_id": work, "branch_id": branch, "command_type": UPGRADE}):
        value = receipt["payload"]
        authorization = kernel.artifact(repo, value["authorization_ref"], work, kind="READING_MCP_UPGRADE")["payload"]
        require(receipt["request_hash"] == fingerprint(authorization["request"])
                and value["cancelled_unknown_attempts"] == authorization["cancelled_unknown_attempts"],
                "INVALID_EVIDENCE", "Legacy MCP migration receipt differs from its exact authorization")
        matches = [item for item in authorization["cancelled_unknown_attempts"] if item["task_id"] == task["task_id"]
                   and item["attempt_id"] == call["attempt_id"] and item["generation"] == call["generation"]
                   and item["current_generation"] == task["generation"] and item["state"] == "UNKNOWN" and item["task_state"] == "CANCELLED"]
        if not matches:
            continue
        require(len(matches) == 1 and task["current_result_id"] is None, "INVALID_EVIDENCE", "Legacy cancelled generation changed after its administrative fence")
        cancellation = repo.one("task_commands", {"principal_id": receipt["principal_id"],
            "idempotency_key": "mcp-upgrade-orphan:" + call["attempt_id"]})
        cancel_content = {"operation": "cancel", "task_id": task["task_id"], "expected_generation": task["generation"],
                          "reason": LEGACY_CANCEL_REASON}
        require(cancellation and cancellation["command_hash"] == fingerprint(cancel_content)
                and cancellation["receipt"]["task_id"] == task["task_id"]
                and cancellation["receipt"]["generation"] == task["generation"]
                and cancellation["receipt"]["status"] == "CANCELLED",
                "INVALID_EVIDENCE", "The legacy orphan has no exact original administrative cancellation receipt")
        old_job = repo.one("supervisor_jobs", {"task_id": task["task_id"]})
        old_input = old_job and kernel.artifact(repo, old_job["config"]["dispatch"]["input_ref"], work, kind="AGENT_INPUT")["payload"]
        draft_ref = authorization["request"]["draft_ref"]
        require(old_input and draft_ref in old_input["material_refs"], "INVALID_EVIDENCE", "The abandoned generation belongs to another manuscript")
        stage = next((name for name, role in ROLES.items() if role == (task["role_id"], task["task_kind"])), None)
        successor = stage and _completed_successor(kernel, repo, gateway, board, draft_ref, stage=stage)
        if not successor:
            return False
        old_spec = repo.one("task_inputs", {"task_id": task["task_id"], "generation": call["generation"]})["spec"]
        new_spec = repo.one("task_inputs", {"task_id": successor["task_id"], "generation": successor["generation"]})["spec"]
        from .world_coverage_recovery import prove_pure_coverage
        prove_pure_coverage(kernel, repo, old_spec["world_ref"], new_spec["world_ref"])
        return True
    return False
