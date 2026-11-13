"""Controller-owned, work-scoped evidence of failures, repair decisions and verified methods.

Model-facing memory contains only fixed method text. Evidence references, excerpts,
and hypotheses stay inside the originating work and never grant material access.
"""
import json
from uuid import UUID, uuid5

from .kernel import require
from .planning import PlanningService
from ..domain.canonical import canonical_bytes, fingerprint

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
    """An adapter-declared REAL_EXTERNAL is insufficient for promoting a production method."""
    from urllib.parse import urlsplit

    from .statistics import is_fixture_configuration
    from ..infrastructure.wire_receipts import read_receipt

    config = audit["model_configuration"]
    endpoint = urlsplit(config.get("endpoint") or "")
    if (config.get("adapter_kind") != "deepseek" or is_fixture_configuration(config)
            or config.get("execution_kind") != "REAL_EXTERNAL" or endpoint.scheme != "https"
            or endpoint.netloc.lower() not in {"api.deepseek.com", "api.deepseek.com:443"}
            or endpoint.path not in {"/chat/completions", "/v1/chat/completions"}
            or endpoint.query or endpoint.fragment
            or config.get("provider") != "https://api.deepseek.com"
            or not audit.get("response_ref") or not audit.get("provider_receipt")):
        return None
    try:
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
    except (KeyError, IndexError, TypeError, ValueError, OSError):
        return None


def verify_method(kernel, repo, owner, wr, plan, evidence, task_kind, fix_ref, review_attempt, review_ref):
    """An independently cleared actual revision, never a proposed fix or an unrelated successful call."""
    if not plan or evidence["execution_kind"] not in {"REAL_HOST", "REAL_EXTERNAL"}:
        return
    review_row = repo.one("artifacts", {"work_id": wr["work_id"], "kind": "AGENT_CALL_EVIDENCE",
                    "artifact_revision_id": str(uuid5(UUID(review_attempt), "audit:revision"))})
    tested = _wire_proof(kernel, repo, wr, evidence)
    checked = _wire_proof(kernel, repo, wr, review_row["payload"]) if review_row else None
    if not (tested and checked):
        _record(kernel, repo, wr["work_id"], owner.principal_id, evidence["attempt_id"], "reviewed-local-repair", {
            "world_ref": wr, "phase": "STAGE_COMPLETED", "attempt_id": evidence["attempt_id"],
            "error_ref": plan["error_ref"], "actual_fix": fix_ref, "regression_result": review_ref,
            "status": "LOCAL_VALIDATION_ONLY", "reusable": False}, "P6_REFLECTION_RECORD")
        return
    frozen = kernel.artifact(repo, evidence["input_ref"], wr["work_id"], kind="AGENT_INPUT")["payload"]
    scope = task_scope(wr, evidence["role_id"], task_kind, frozen["runtime_sha256"],
                       frozen["output_contract"], frozen["model_config_sha256"])
    ref = _record(kernel, repo, wr["work_id"], owner.principal_id, evidence["attempt_id"], "verified-method",
                  {"record_version": 1, "status": "VERIFIED", "scope": scope, "method": plan["method"],
                   "error_ref": plan["error_ref"], "reflection_ref": plan["reflection_ref"],
                   "actual_fix": fix_ref, "regression_result": review_ref,
                   "fix_wire_sha256": tested, "review_wire_sha256": checked,
                   "fix_attempt_id": evidence["attempt_id"], "review_attempt_id": review_attempt,
                   "validation_scope": "INDEPENDENT_REVIEW_OF_EXACT_REVISION",
                   "reusable": True}, "P6_EXPERIENCE_MEMORY")
    PlanningService(kernel)._select(repo, wr, _key(scope), ref)
    _record(kernel, repo, wr["work_id"], owner.principal_id, evidence["attempt_id"], "reviewed-repair", {
        "world_ref": wr, "phase": "STAGE_COMPLETED", "attempt_id": evidence["attempt_id"], "experience_ref": ref,
        "reflection_ref": plan["reflection_ref"], "error_ref": plan["error_ref"], "actual_fix": fix_ref,
        "regression_result": review_ref, "scope": scope}, "P6_REFLECTION_RECORD")


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
