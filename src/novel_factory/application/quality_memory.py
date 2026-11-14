"""Controller-owned, work-scoped evidence of failures, repair decisions and verified methods.

Model-facing memory contains only fixed method text. Evidence references, excerpts,
and hypotheses stay inside the originating work and never grant material access.
"""
import json
from uuid import UUID, uuid5

from ..domain.canonical import canonical_bytes, fingerprint
from ..domain.errors import DomainError
from .kernel import require
from .planning import PlanningService

METHODS = {
    "schema": (
        "依照本次输出契约逐项修复诊断指向的字段，完成后检查 JSON 可解析性和必填字段。",
        "改变构造顺序：先复制本次外层身份，再依照 Schema 的必填键和类型重建空骨架，最后逐字段填入。",
    ),
    "extent": (
        "按给定汉字数与本次实测差额修订；核对修改后的篇幅并保持场景契约、已发生事件与视角。",
        "调整写作步骤：先按场景内部动作或转折拆分段落字数，再逐段扩写或压缩并复核全文。",
    ),
    "editorial": (
        "逐项定位审读意见及其依据，修订对应段落，核验未涉及部分的连续性。",
        "改用倒序核查：从审读问题所要求的可见结果回溯前提，再复查受影响的段落和未改的前后文。",
    ),
}
ENROLLED = "TARGETED_REPAIR"
RESELECTED = "CHANGED_STRATEGY"
PAUSED = "STRATEGY_REASSESSMENT_REQUIRED"


def plan_repair(family, findings, history):
    """A fresh failure signature can advance. A recurrence changes method or requests diagnosis."""
    signature = fingerprint([family, findings])
    previous = [r for r in history if r.get("signature") == signature]
    index = len(previous)
    return {"family": family, "signature": signature, "decision": (ENROLLED, RESELECTED, PAUSED)[min(index, 2)],
            "method": METHODS[family][index] if index < len(METHODS[family]) else None,
            "strategy_version": 1, "occurrences": index + 1}


def _record(kernel, repo, work, principal_id, namespace, label, payload, kind):
    """Deterministic under the caller's task/work lock, including crash replay."""
    ref = {"artifact_id": str(uuid5(UUID(namespace), label)),
           "artifact_revision_id": str(uuid5(UUID(namespace), label + ":revision")),
           "sha256": fingerprint(payload)}
    if repo.one("artifacts", {"work_id": work, "artifact_revision_id": ref["artifact_revision_id"]}):
        kernel.artifact(repo, ref, work, kind=kind)
    else:
        kernel.objects.put(canonical_bytes(payload))
        kernel._register_artifact(repo, ref, work, payload, kind, principal_id)
    return ref


def _review_record(kernel, repo, work, principal_id, namespace, label, payload, kind):
    """A replay may recreate the same controller result with a fresh reference."""
    revision = str(uuid5(UUID(namespace), label + ":revision"))
    existing = repo.one("artifacts", {"work_id": work, "artifact_revision_id": revision})
    if existing and existing["payload"].get("regression_result") != payload["regression_result"]:
        old_ref = existing["payload"]["regression_result"]
        new_ref = payload["regression_result"]
        old = kernel.artifact(repo, old_ref, work)
        new = kernel.artifact(repo, new_ref, work)
        require(old["kind"] == new["kind"] and old_ref["sha256"] == new_ref["sha256"]
                and old["payload"] == new["payload"] and fingerprint(old["payload"]) == old_ref["sha256"],
                "INVALID_EVIDENCE", "A repeated independent review produced a different controller result")
        payload = {**payload, "regression_result": old_ref}
    return _record(kernel, repo, work, principal_id, namespace, label, payload, kind)


def task_scope(wr, role_id, task_kind, runtime_sha256, contract, model_sha256):
    return {"branch_id": wr["branch_id"], "role_id": role_id, "task_kind": task_kind,
            "runtime_sha256": runtime_sha256, "output_contract": contract, "model_sha256": model_sha256}


def _key(scope):
    return "P6_VERIFIED_METHOD:" + fingerprint(scope)


def recall(kernel, repo, wr, scope):
    selected = repo.one("selections", {"work_id": wr["work_id"], "selection_kind": _key(scope)})
    if not selected:
        return None
    memory = kernel.artifact(repo, selected["artifact_ref"], wr["work_id"], kind="P6_EXPERIENCE_MEMORY")["payload"]
    require(memory["scope"] == scope, "TASK_REBIND_REQUIRED", "Experience belongs to another frozen scope")
    if memory["status"] == "VERIFIED":
        return {"experience_ref": selected["artifact_ref"], "method": memory["method"], "version": selected["epoch"]}
    return None


def observe_attempt(kernel, repo, binding, audit, audit_ref):
    task = binding["task"]
    base = {"task_id": task["task_id"], "attempt_id": audit["attempt_id"], "generation": task["generation"],
            "role_id": task["role_id"], "task_kind": task["task_kind"], "world_ref": audit["world_ref"],
            "audit_ref": audit_ref, "response_ref": audit["response_ref"], "prompt_ref": audit["prompt_ref"]}
    pid, work = task["principal_id"], task["work_id"]
    succeeded = audit["outcome"] == "VALIDATED_CANDIDATE" and audit["result_status"] == "SUCCEEDED"
    if not succeeded:
        reason = audit["reason"] or audit["result_status"]
        technical = reason in {"INVALID_MODEL_OUTPUT", "INVALID_ARGUMENT", "MODEL_PROTOCOL_ERROR", "MODEL_IDENTITY_MISMATCH"}
        hypothesis = ("The returned representation did not satisfy the frozen transport, envelope or payload contract."
                      if technical else "Inspect the attempt and its accounting or task prerequisites before selecting a remedy.")
        error = _record(kernel, repo, work, pid, audit["attempt_id"], "error-record", {
            **base, "error_type": reason, "trigger": audit["outcome"], "root_cause_hypothesis": hypothesis,
            "accounting_state": audit["accounting_state"], "actual_fix": None, "regression_result": None,
            "reusable": False, "record_version": 1}, "P6_ERROR_RECORD")
        _record(kernel, repo, work, pid, audit["attempt_id"], "failure-reflection", {
            **base, "error_ref": error, "phase": "AFTER_FAILURE", "hypothesis": hypothesis,
            "action": "INSPECT_FROZEN_CONTRACT" if technical else "INSPECT_ATTEMPT",
        }, "P6_REFLECTION_RECORD")
    prior = binding["input"].get("verified_experience")
    if prior:
        _record(kernel, repo, work, pid, audit["attempt_id"], "method-followup", {
            **base, "experience_ref": prior["experience_ref"], "phase": "METHOD_FOLLOW_UP",
            "passed_contract": succeeded, "method_version": prior["version"],
        }, "P6_REFLECTION_RECORD")


def reflect_repair(kernel, repo, owner, wr, task_id, attempt_id, evidence_ref, plan, trigger):
    base = {"work_id": wr["work_id"], "branch_id": wr["branch_id"], "task_id": task_id,
            "attempt_id": attempt_id, "evidence_ref": evidence_ref}
    tag = fingerprint([trigger, plan["signature"]])
    error = _record(kernel, repo, wr["work_id"], owner.principal_id, attempt_id, "quality-error:" + tag, {
        **base, "record_version": 1, "error_type": plan["family"], "trigger": trigger,
        "root_cause_hypothesis": "Inspect the linked validation or reviewer findings for this exact candidate.",
        "actual_fix": None, "regression_result": None, "reusable": False}, "P6_ERROR_RECORD")
    ref = _record(kernel, repo, wr["work_id"], owner.principal_id, attempt_id, "quality-reflection:" + tag,
                  {**base, **plan, "phase": "REPAIR_DECISION", "error_ref": error}, "P6_REFLECTION_RECORD")
    return {**plan, "error_ref": error, "reflection_ref": ref}


def _wire_proof(kernel, repo, wr, audit):
    """Require delivery evidence, never an adapter's declaration or model self-report."""
    from ..infrastructure.wire_receipts import read_receipt
    from .agent_gateway import AgentGateway
    from .candidate_delivery import CandidateDelivery
    from .native_delivery import require_native_delivery
    from .statistics import is_official_deepseek

    try:
        config = audit["model_configuration"]
        task = repo.one("tasks", {"task_id": audit["task_id"], "generation": audit["generation"],
                                   "work_id": wr["work_id"], "branch_id": wr["branch_id"]})
        if not task or not audit.get("response_ref") or not audit.get("provider_receipt"):
            return None
        # This also binds the CURRENT task result, audit, candidate and unresolved-question gate.
        delivery = CandidateDelivery(kernel, AgentGateway(kernel, {}))
        persisted_audit, _ = delivery.audited_candidate(repo, task, production_accounting=True)
        if persisted_audit != audit:
            return None
        if config.get("adapter_kind") == "codex-cli":
            call = repo.one("call_attempts", {"attempt_id": audit["attempt_id"]})
            audit_row = repo.one("artifacts", {"work_id": wr["work_id"], "kind": "AGENT_CALL_EVIDENCE",
                "artifact_revision_id": str(uuid5(UUID(audit["attempt_id"]), "audit:revision"))})
            response = kernel.artifact(repo, audit["response_ref"], wr["work_id"], kind="AGENT_RESPONSE")
            candidate = kernel.artifact(repo, audit["output_ref"], wr["work_id"], kind="AGENT_CANDIDATE")
            # Always run the full Host proof, even if a caller changed the accounting state.
            # Recovery-only exemptions must never turn a review into a reusable method.
            require_native_delivery(kernel, repo, task, call, audit_row, response, candidate)
            host = repo.one("host_turns", {"attempt_id": audit["attempt_id"]})
            return fingerprint({"kind": "COMPLETED_NATIVE_HOST", "attempt_id": audit["attempt_id"],
                                "request_sha256": audit["request_sha256"], "response_ref": audit["response_ref"],
                                "thread_id": host["thread_id"], "turn_id": host["turn_id"]})
        if not is_official_deepseek(config):
            return None
        wire = read_receipt(kernel.objects, audit["attempt_id"], audit["request_sha256"])
        if not wire:
            return None
        receipt, body = wire
        parsed = json.loads(body)
        choice = parsed["choices"][0]
        persisted = kernel.artifact(repo, audit["response_ref"], wr["work_id"], kind="AGENT_RESPONSE")["payload"]
        if (receipt.get("http_status") != 200 or receipt.get("complete") is not True
                or (receipt.get("receipt") or parsed.get("id")) != audit["provider_receipt"]
                or parsed.get("model") != config.get("model") or choice.get("finish_reason") != "stop"
                or choice["message"]["content"] != persisted["text"]
                or audit["outcome"] != "VALIDATED_CANDIDATE" or audit["result_status"] != "SUCCEEDED"):
            return None
        return receipt["body_sha256"]
    except (DomainError, KeyError, IndexError, TypeError, ValueError, OSError):
        return None


def _review_of_revision(kernel, repo, wr, fix, fix_ref, review, review_ref):
    """Bind an independent reading to the repaired revision and its controller result."""
    from ..domain.review_gate import blocking_review_issues

    try:
        writer = repo.one("tasks", {"task_id": fix["task_id"], "generation": fix["generation"]})
        reader = repo.one("tasks", {"task_id": review["task_id"], "generation": review["generation"]})
        if (not writer or not reader or writer["principal_id"] == reader["principal_id"]
                or writer["grant_id"] == reader["grant_id"]
                or reader["role_id"] not in {"reviewer", "evidence_maintainer"}
                or fix["world_ref"] != review["world_ref"]):
            return False
        candidate = kernel.artifact(repo, review["output_ref"], wr["work_id"], kind="AGENT_CANDIDATE")["payload"]
        if reader["role_id"] == "reviewer" and blocking_review_issues(candidate["issues"]):
            return False
        frozen = kernel.artifact(repo, review["input_ref"], wr["work_id"], kind="AGENT_INPUT")["payload"]
        source = repo.one("task_inputs", {"task_id": reader["task_id"], "generation": reader["generation"]})
        if not source or fix_ref not in source["spec"]["input_artifacts"]:
            return False
        result = kernel.artifact(repo, review_ref, wr["work_id"])
        if fix_ref == fix["output_ref"]:
            packet = json.loads(frozen["assignment"])
            return (reader["role_id"] == "reviewer" and packet.get("candidate_ref") == fix_ref
                    and review_ref == review["output_ref"])
        draft = kernel.artifact(repo, fix_ref, wr["work_id"], kind="NARRATIVE_DRAFT")["payload"]
        if (draft["agent_output_ref"] != fix["output_ref"] or draft["task_id"] != writer["task_id"]
                or draft["generation"] != writer["generation"] or fix_ref not in frozen["material_refs"]):
            return False
        scope = repo.one("selections", {"work_id": wr["work_id"],
                    "selection_kind": f"NARRATIVE_READ:{reader['task_id']}:{reader['generation']}"})
        if not scope:
            return False
        reading = kernel.artifact(repo, scope["artifact_ref"], wr["work_id"], kind="NARRATIVE_READ_SCOPE")["payload"]
        if (fix_ref not in reading["draft_refs"] or reading["task_id"] != reader["task_id"]
                or reading["generation"] != reader["generation"] or reading["grant_id"] != reader["grant_id"]):
            return False
        payload = result["payload"]
        if result["kind"] == "CHAPTER_READING_ADVICE":
            return (reader["role_id"] == "reviewer" and fix_ref in payload["draft_refs"]
                    and payload["task_id"] == reader["task_id"] and payload["generation"] == reader["generation"]
                    and payload["candidate_ref"] == review["output_ref"])
        if result["kind"] == "EPISTEMIC_READING":
            return (reader["role_id"] == "reviewer" and payload["draft_ref"] == fix_ref
                    and payload["reader_task"] == reader["task_id"] and payload["generation"] == reader["generation"]
                    and payload["attempt_id"] == review["attempt_id"] and payload["candidate_ref"] == review["output_ref"]
                    and payload["status"] == "CLEAR")
        if result["kind"] == "ROLLING_FACT_REPORT" and reader["role_id"] == "evidence_maintainer":
            extraction = kernel.artifact(repo, payload["extraction_ref"], wr["work_id"], kind="ASSERTION_EXTRACTION")["payload"]
            report = kernel.artifact(repo, payload["reconciliation_ref"], wr["work_id"], kind="RECONCILIATION_REPORT")["payload"]
            return (payload["status"] == "CLEAR" and extraction["draft_ref"] == report["draft_ref"] == fix_ref
                    and report["extraction_ref"] == payload["extraction_ref"]
                    and extraction["task_id"] == reader["task_id"] and extraction["generation"] == reader["generation"])
        return False
    except (DomainError, KeyError, TypeError, ValueError, OSError):
        return False


def _method_applied(kernel, repo, wr, plan, evidence, fix_ref):
    """A successful revision cannot validate a superseded or merely proposed strategy."""
    try:
        frozen = kernel.artifact(repo, evidence["input_ref"], wr["work_id"], kind="AGENT_INPUT")["payload"]
        method, assignment = plan["method"], frozen["assignment"]
        try:
            packet = json.loads(assignment)
        except ValueError:
            packet = None
        if (isinstance(packet, dict) and packet.get("repair_strategy") != method
                or not isinstance(packet, dict) and method not in assignment):
            return False
        error = kernel.artifact(repo, plan["error_ref"], wr["work_id"], kind="P6_ERROR_RECORD")["payload"]
        if (error.get("work_id") != wr["work_id"] or error.get("branch_id") != wr["branch_id"]):
            return False
        row = kernel.artifact(repo, plan["reflection_ref"], wr["work_id"])
        reflection = row["payload"]
        if row["kind"] == "P6_REFLECTION_RECORD":
            if (reflection.get("phase") != "REPAIR_DECISION" or reflection.get("method") != method
                    or reflection.get("error_ref") != plan["error_ref"]):
                return False
        elif row["kind"] == "PRODUCTION_REASSESSMENT":
            if (reflection["outcome"] != "STRATEGY_READY" or reflection["method"] != method
                    or not any(item.get("error_ref") == plan["error_ref"] and item["signature"] == plan["signature"]
                               for item in reflection["request"]["history"]["repair_plans"])):
                return False
        else:
            return False
        if fix_ref != evidence["output_ref"]:
            source = repo.one("task_inputs", {"task_id": evidence["task_id"], "generation": evidence["generation"]})
            return source and all(ref in source["spec"]["input_artifacts"]
                                  for ref in (plan["error_ref"], plan["reflection_ref"]))
        return True
    except (DomainError, KeyError, TypeError, ValueError, OSError):
        return False


def verify_method(kernel, repo, owner, wr, plan, evidence, task_kind, fix_ref, review_attempt, review_ref):
    """An independently cleared actual revision, never a proposed fix or an unrelated successful call."""
    if (not plan or not isinstance(plan.get("method"), str) or not plan["method"].strip()
            or plan.get("decision") == PAUSED or evidence["execution_kind"] not in {"REAL_HOST", "REAL_EXTERNAL"}):
        return
    review_row = repo.one("artifacts", {"work_id": wr["work_id"], "kind": "AGENT_CALL_EVIDENCE",
                    "artifact_revision_id": str(uuid5(UUID(review_attempt), "audit:revision"))})
    # One writer can be read repeatedly after a transport or evidence repair.
    # Retain legacy writer-only records; new records name this independent read.
    review_identity = {"review_attempt_id": review_attempt,
                       "review_candidate_ref": review_row["payload"]["output_ref"] if review_row else None,
                       "review_generation": review_row["payload"]["generation"] if review_row else None}
    namespace = str(uuid5(UUID(evidence["attempt_id"]), "quality-review:" + fingerprint({
        "world_ref": wr, "actual_fix": fix_ref, "error_ref": plan["error_ref"],
        "reflection_ref": plan.get("reflection_ref"), **review_identity})))
    tested = _wire_proof(kernel, repo, wr, evidence)
    checked = _wire_proof(kernel, repo, wr, review_row["payload"]) if review_row else None
    if not (tested and checked and _method_applied(kernel, repo, wr, plan, evidence, fix_ref)
            and _review_of_revision(kernel, repo, wr, evidence, fix_ref,
                                                      review_row["payload"], review_ref)):
        _review_record(kernel, repo, wr["work_id"], owner.principal_id, namespace, "reviewed-local-repair", {
            "world_ref": wr, "phase": "STAGE_COMPLETED", "attempt_id": evidence["attempt_id"],
            "error_ref": plan["error_ref"], "actual_fix": fix_ref, "regression_result": review_ref,
            "status": "LOCAL_VALIDATION_ONLY", "reusable": False, **review_identity}, "P6_REFLECTION_RECORD")
        return
    frozen = kernel.artifact(repo, evidence["input_ref"], wr["work_id"], kind="AGENT_INPUT")["payload"]
    scope = task_scope(wr, evidence["role_id"], task_kind, frozen["runtime_sha256"],
                       frozen["output_contract"], frozen["model_config_sha256"])
    # Preserve the exact historical DeepSeek-only record bytes on crash replay.
    # For native deliveries the legacy *_wire_sha256 fields hold the Host proof digest.
    delivery_kinds = {}
    if "codex-cli" in {evidence["model_configuration"]["adapter_kind"],
                       review_row["payload"]["model_configuration"]["adapter_kind"]}:
        delivery_kinds = {"fix_delivery_kind": evidence["model_configuration"]["adapter_kind"],
                          "review_delivery_kind": review_row["payload"]["model_configuration"]["adapter_kind"]}
    ref = _review_record(kernel, repo, wr["work_id"], owner.principal_id, namespace, "verified-method",
                  {"record_version": 1, "status": "VERIFIED", "scope": scope, "method": plan["method"],
                   "error_ref": plan["error_ref"], "reflection_ref": plan["reflection_ref"],
                   "actual_fix": fix_ref, "regression_result": review_ref,
                   "fix_wire_sha256": tested, "review_wire_sha256": checked,
                   **delivery_kinds,
                   "fix_attempt_id": evidence["attempt_id"], "review_attempt_id": review_attempt,
                   "review_candidate_ref": review_identity["review_candidate_ref"],
                   "review_generation": review_identity["review_generation"],
                   "validation_scope": "INDEPENDENT_REVIEW_OF_EXACT_REVISION",
                   "reusable": True}, "P6_EXPERIENCE_MEMORY")
    current = repo.one("selections", {"work_id": wr["work_id"], "selection_kind": _key(scope)})
    if not current or current["artifact_ref"] != ref:
        PlanningService(kernel)._select(repo, wr, _key(scope), ref)
    _review_record(kernel, repo, wr["work_id"], owner.principal_id, namespace, "reviewed-repair", {
        "world_ref": wr, "phase": "STAGE_COMPLETED", "attempt_id": evidence["attempt_id"], "experience_ref": ref,
        "reflection_ref": plan["reflection_ref"], "error_ref": plan["error_ref"], "actual_fix": fix_ref,
        "regression_result": review_ref, "scope": scope, **review_identity}, "P6_REFLECTION_RECORD")


def revoke_experience(kernel, owner, wr, ref):
    """An explicit owner decision supersedes a verified method only for future task preparation."""
    with kernel.objects.lock(), kernel.db.uow() as repo:
        repo.one("works", {"work_id": wr["work_id"]}, lock="update")
        kernel.authorize(repo, owner, wr["work_id"], "ADMIN", wr["branch_id"])
        prior = kernel.artifact(repo, ref, wr["work_id"], kind="P6_EXPERIENCE_MEMORY")["payload"]
        require(prior["scope"]["branch_id"] == wr["branch_id"] and prior["status"] == "VERIFIED",
                "VIEW_DENIED", "Verified experience must belong to the authorized branch")
        key = _key(prior["scope"])
        current = repo.one("selections", {"work_id": wr["work_id"], "selection_kind": key})
        require(current and current["artifact_ref"] == ref, "STALE_GENERATION", "Experience selection changed")
        withdrawal = _record(kernel, repo, wr["work_id"], owner.principal_id, ref["artifact_revision_id"],
                             "withdrawal", {"scope": prior["scope"], "supersedes": ref, "status": "REVOKED"},
                             "P6_EXPERIENCE_MEMORY")
        PlanningService(kernel)._select(repo, wr, key, withdrawal)
        return withdrawal


def transfer_verified_method(kernel, source_owner, source_wr, source_ref, target_owner, target_wr):
    """Two authorized work owners may transfer a verified, fixed method to the next experiment.

    Only an allowlisted engineering instruction crosses works. The private evidence and
    source object references stay behind the source branch's administrative read fence.
    """
    with kernel.objects.lock(), kernel.db.uow() as repo:
        for work in sorted({source_wr["work_id"], target_wr["work_id"]}):
            repo.one("works", {"work_id": work}, lock="update")
        for owner, wr in ((source_owner, source_wr), (target_owner, target_wr)):
            kernel.authorize(repo, owner, wr["work_id"], "ADMIN", wr["branch_id"])
            kernel.revision(repo, wr)
        source = kernel.artifact(repo, source_ref, source_wr["work_id"], kind="P6_EXPERIENCE_MEMORY")["payload"]
        require(source["status"] == "VERIFIED" and source["scope"]["branch_id"] == source_wr["branch_id"]
                and source["method"] in {item for group in METHODS.values() for item in group}
                and source.get("validation_scope") == "INDEPENDENT_REVIEW_OF_EXACT_REVISION"
                and (recall(kernel, repo, source_wr, source["scope"]) or {}).get("experience_ref") == source_ref,
                "VIEW_DENIED", "Only a currently selected, independently verified local method can be transferred")
        scope = {**source["scope"], "branch_id": target_wr["branch_id"]}
        key = _key(scope)
        previous = repo.one("selections", {"work_id": target_wr["work_id"], "selection_kind": key})
        token = fingerprint([source_ref["sha256"], scope])
        if previous:
            existing = kernel.artifact(repo, previous["artifact_ref"], target_wr["work_id"], kind="P6_EXPERIENCE_MEMORY")
            require(existing["payload"].get("transfer_token") == token and existing["payload"]["status"] == "VERIFIED",
                    "STALE_GENERATION", "A different or withdrawn method already occupies this scope")
            return previous["artifact_ref"]
        copied = _record(kernel, repo, target_wr["work_id"], target_owner.principal_id, target_wr["branch_id"],
                         "method-transfer:" + token, {"scope": scope, "status": "VERIFIED", "method": source["method"],
                         "record_version": 1, "transfer_token": token, "validation_scope": "SOURCE_WORK_VERIFICATION",
                         "reusable": True}, "P6_EXPERIENCE_MEMORY")
        PlanningService(kernel)._select(repo, target_wr, key, copied)
        return copied
