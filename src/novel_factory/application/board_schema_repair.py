"""Recover a returned, rejected board candidate without trusting its story or identity."""
import json

from .idea_repair import UnparsedDesign, returned_design
from .kernel import require
from .quality_memory import PAUSED, plan_repair, reflect_repair
from .semantic_dependencies import check_task_dependencies
from ..infrastructure.wire_receipts import read_receipt


def verify_board_transport(kernel, audit, raw):
    """A malformed postpaid return requires the attempt's complete native response bytes."""
    wire = read_receipt(kernel.objects, audit["attempt_id"], audit["request_sha256"])
    require(wire, "CALL_RECONCILIATION_REQUIRED", "The board attempt has no durable transport receipt")
    receipt, body = wire
    try:
        response = json.loads(body)
        choice = response["choices"][0]
        valid = (receipt["complete"] is True and receipt["http_status"] == 200
                 and (receipt.get("receipt") or response["id"]) == audit["provider_receipt"]
                 and response["model"] == audit["model_configuration"]["model"]
                 and choice["finish_reason"] in {"stop", "length"} and choice["message"]["content"] == raw["text"])
    except (ValueError, TypeError, IndexError, KeyError):
        valid = False
    require(valid, "CALL_RECONCILIATION_REQUIRED", "Board response bytes require transport reconciliation")


def board_format_method(node):
    """Specific repair for observed reviewer fields misplaced on the envelope."""
    repairs = node.get("schema_repairs") or []
    if not repairs:
        return None
    last = repairs[-1]
    diag = last.get("diagnostic", {})
    if node["stage"] == "draft" and diag.get("keyword") == "jsonSyntax":
        return ("继续本角色的写作任务。先完成正文，再作为 JSON 字符串写入 payload.text。"
                "换行使用 JSON 换行转义，字符串中的双引号和反斜杠必须转义。仅返回一个完整 JSON 对象。"
                "在发送前从报错位置附近核对字符串边界与括号，检查全文可解析。")
    if node["stage"] not in {"review", "epistemic"}:
        return None
    fields = diag.get("move_inside_payload")
    if (not fields or diag.get("location") != "envelope" or diag.get("keyword") != "additionalProperties"
            or not set(fields) <= {"summary", "issues", "counterinterpretations", "evidence_refs"}):
        return None
    from .prompt_schema import inline_schema
    allowed = inline_schema("AgentModelResponse", "agent")["required"]
    return ("先单独完成审核内容，再将它作为一个对象赋给外层 payload。外层只保留这些键：" +
            json.dumps(allowed) + "。下列键只能位于 payload 内：" + json.dumps(fields) +
            "。发送前逐个核对最外层键；保留全部实质审核意见。")


def resume_review_format(kernel, repo, owner, board, identity):
    node = board["nodes"][identity]
    method = board_format_method(node)
    if not method or node["state"] != "STOPPED" or node.get("reason") != PAUSED:
        return False
    last = node["schema_repairs"][-1]
    require(last["decision"] == PAUSED and last["task_id"] == node["task_id"] and not last.get("actual_fix"),
            "STALE_GENERATION", "Review format no longer matches the rejected candidate")
    task = repo.one("tasks", {"task_id":last["task_id"], "generation":last["generation"], "state":"FAILED"})
    require(task, "TASK_REBIND_REQUIRED", "Rejected review generation changed")
    check_task_dependencies(repo, task["task_id"], task["generation"])
    plan = {"family":"schema", "signature":last["signature"], "occurrences":last["occurrences"],
            "decision":"CHANGED_STRATEGY", "strategy_version":2, "method":method}
    plan = reflect_repair(kernel, repo, owner, board["world_ref"], task["task_id"], last["attempt_id"],
                          last["audit_ref"], plan, "WRITER_JSON_SYNTAX" if node["stage"] == "draft" else "REVIEW_PAYLOAD_PLACEMENT")
    last["reassessment"] = plan
    node.pop("task_id")
    node.pop("enrolled_at", None)
    node.update(state="PENDING", reason="SCHEMA_REPAIR_REQUESTED")
    return True


def recover_board_node(kernel, repo, gateway, owner, board, identity, task):
    node = board["nodes"][identity]
    wr = node.get("world_ref", board["world_ref"])
    job = repo.one("supervisor_jobs", {"task_id": task["task_id"]})["config"]
    require(job.get("production_node") == identity and task["state"] == "FAILED", "STALE_GENERATION",
            "The failed task no longer owns this production node")
    if node["stage"] == "draft":
        scene_id = identity.removeprefix("draft:")
        require(not repo.one("selections", {"work_id": wr["work_id"],
                                            "selection_kind": "NARRATIVE_SCENE:" + scene_id}),
                "NARRATIVE_SELECTION_CONFLICT", "A frozen scene cannot be redrafted by technical recovery")
    check_task_dependencies(repo, task["task_id"], task["generation"])
    grant = repo.one("grants", {"grant_id": task["grant_id"]}, lock="share")
    require(grant and grant["active"] and grant["epoch"] == job["grant_epoch"]
            and task["deadline"] > repo.now(), "TASK_REBIND_REQUIRED", "Source task authorization expired")
    source_ref = job["dispatch"]["input_ref"]
    old = kernel.artifact(repo, source_ref, wr["work_id"], kind="AGENT_INPUT")["payload"]
    result, audit, audit_ref = returned_design(kernel, repo, gateway, task, wr, {"input_ref": source_ref},
                                              allow_malformed=True, allow_contract=True, board_node=identity)
    require(isinstance(result, UnparsedDesign), "INVALID_EVIDENCE", "Board failure has no reproducible diagnostic")
    diag = result.diagnostic
    findings = {k: v for k, v in diag.items() if k not in {"line", "column", "offset"}}
    history = node.setdefault("schema_repairs", [])
    plan = plan_repair("schema", findings, history)
    plan = reflect_repair(kernel, repo, owner, wr, task["task_id"], audit["attempt_id"], audit_ref, plan, identity)
    history.append({**plan, "diagnostic": diag, "attempt_id": audit["attempt_id"], "audit_ref": audit_ref,
                    "task_id": task["task_id"], "input_ref": source_ref, "generation": task["generation"],
                    "model_config_sha256": old["model_config_sha256"], "runtime_sha256": old["runtime_sha256"],
                    "output_contract": old["output_contract"]})
    if plan["decision"] == PAUSED:
        node.update(state="STOPPED", reason=PAUSED)
        return
    node.pop("task_id")
    node.pop("enrolled_at", None)
    node.update(state="PENDING", reason="SCHEMA_REPAIR_REQUESTED")


def record_contract_recovery(node, task, audit, output_ref):
    # Technical recovery can succeed even when a later editorial/extent check requests revision.
    history = node.get("schema_repairs")
    if history and "actual_fix" not in history[-1]:
        history[-1].update(actual_fix=output_ref, corrected_task_id=task["task_id"],
                           corrected_attempt_id=audit["attempt_id"], regression_result="CONTRACT_VALIDATED")
