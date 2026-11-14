"""Retry an unproven review against unchanged sources, preserving the rejected result."""
from .kernel import require
from .scene_revision import _archive
from .technical_repair_budget import technical_repair_budget

ERRORS = {"REVIEW_QUERY_REQUIRED", "REVIEW_EVIDENCE_REQUIRED", "REVIEW_UNPROVEN_CONFLICT"}
FAMILY = "REVIEW_EVIDENCE_REPAIR"


def recover_review(kernel, repo, gateway, node, task, error):
    job = repo.one("supervisor_jobs", {"task_id": task["task_id"]})
    if error.code not in ERRORS or not job or not job["config"].get("required_review_mcp"):
        return False
    require(node["task_id"] == task["task_id"] and task["state"] == "RESULT_STORED",
            "TASK_REBIND_REQUIRED", "Rejected review no longer matches the returned task")
    call = repo.one("call_attempts", {"task_id": task["task_id"], "generation": task["generation"],
                                      "call_key": "supervised-candidate"})
    audit = call and gateway._read_audit(repo, call["attempt_id"])
    require(audit and audit["payload"]["outcome"] == "VALIDATED_CANDIDATE", "INVALID_EVIDENCE",
            "Evidence correction requires the immutable returned candidate")
    if reason := technical_repair_budget(node):
        node.update(state="STOPPED", reason=reason)
        return True
    history = node.setdefault("evidence_repairs", [])
    evidence_repairs = sum(row.get("family") == FAMILY or
                           row.get("family") is None and row.get("code") in ERRORS for row in history)
    if evidence_repairs >= 2:
        node.update(state="STOPPED", reason="REVIEW_EVIDENCE_REPAIR_EXHAUSTED")
        return True
    diagnostic = {"code": error.code, "message": error.safe_message}
    history.append({"family": FAMILY, "task_id": task["task_id"], "generation": task["generation"],
                    "attempt_id": call["attempt_id"], "candidate_ref": audit["payload"]["output_ref"],
                    "input_ref": audit["payload"]["input_ref"], "code": error.code,
                    "diagnostic": diagnostic,
                    "method": "上次审核的具体拒绝原因：" + error.safe_message + "。"
                    "保持当前原稿和切面不变，重新独立审核。必须亲自调用本任务MCP读取全部指定材料并核对相关数据。"
                    "nf_review_material返回passages，每项已有程序计算的完整span。强制修改的evidence_refs须引用当前text_ref，"
                    "逐字复制对应passages[].span整个对象（包括quote_sha256）；不要自行计算偏移或哈希，也不要只引用TEXT而省略span。"
                    "给出明确修改方法；世界硬冲突另须引用真实查询receipt_ref。"
                    "UNKNOWN或未登记不能证明矛盾，列为QUESTION/DEFER并交内部核查。不得为通过校验而修改原稿或编造回执。"})
    repo.change("tasks", {"task_id": task["task_id"]},
                {"state": "WAITING_REVIEW", "updated_at": repo.now(), "lease_owner": None, "lease_expiry": None})
    kernel.tasks._emit(repo.conn, task["task_id"], task["generation"], "WAITING_REVIEW")
    _archive(node)
    node["reason"] = "REVIEW_EVIDENCE_REREAD"
    return True
