"""Durable internal question handoffs; no model calls or world acceptance here."""
import json

from ..domain.canonical import fingerprint
from ..domain.errors import DomainError
from ..domain.reconciliation import verify_span
from .internal_review_resolution import (
    AUTHOR_DECISION,
    RESOLVED,
    WORLD_INPUT,
    consume_resolution,
    record_resolution,
    request_from_blocked,
    request_from_fact_report,
)
from .kernel import require
from .scene_revision import _archive


def recover_verification(kernel, repo, gateway, node, task, error):
    """At most two fresh read-only corrections of one completed internal case.

    Preserve old results/calls, including UNKNOWN subscription accounting. A
    missing native delivery proof is not a format retry and must still block.
    """
    if node.get("stage") != "internal_verification" or error.code not in {"INVALID_ARGUMENT", "INVALID_EVIDENCE"}:
        return False
    history = node.get("evidence_repairs", [])
    if any(row.get("family") == "internal_verification" and row["task_id"] == task["task_id"]
           and row["generation"] == task["generation"] for row in history):
        return True
    require(node.get("task_id") == task["task_id"] and task["state"] == "RESULT_STORED",
            "TASK_REBIND_REQUIRED", "Internal correction requires its current returned task")
    job = repo.one("supervisor_jobs", {"task_id": task["task_id"]})
    request = node["internal_case"]
    require(job and job["config"].get("required_review_mcp")
            and job["config"].get("production_node") == "verification:" + request["case_hash"]
            and (task["role_id"], task["task_kind"]) == ("reviewer", "verification"),
            "VIEW_DENIED", "Only the frozen internal verification job can correct its answer format")
    from .candidate_delivery import CandidateDelivery
    from .internal_review_resolution import _assignment_case_hash
    from .technical_repair_budget import technical_repair_budget
    evidence, _ = CandidateDelivery(kernel, gateway).audited_candidate(repo, task, production_accounting=True)
    frozen = kernel.artifact(repo, evidence["input_ref"], task["work_id"], kind="AGENT_INPUT")["payload"]
    require(_assignment_case_hash(frozen["assignment"]) == request["case_hash"],
            "TASK_REBIND_REQUIRED", "Internal correction cannot change its case")
    if reason := technical_repair_budget(node):
        node.update(state="STOPPED", reason=reason)
        return True
    if sum(row.get("family") == "internal_verification" for row in history) >= 2:
        node.update(state="STOPPED", reason="INTERNAL_VERIFICATION_REPAIR_EXHAUSTED")
        return True
    repair = {"family": "internal_verification", "task_id": task["task_id"], "generation": task["generation"],
        "attempt_id": evidence["attempt_id"], "candidate_ref": evidence["output_ref"], "input_ref": evidence["input_ref"],
        "case_hash": request["case_hash"], "code": error.code, "diagnostic": error.safe_message,
        **{key: frozen[key] for key in ("runtime_sha256", "model_config_sha256", "output_contract")},
        "method": "保持本次case_hash、原稿、WorldRef及全部问题不变，仅校正核查输出与证据对应。"
        "decision必须为包含case_hash和完整answers的JSON字符串；逐题按原index回答，explanation/correction各不超过256字，"
        "每题最多3条证据。每题亲自查询相关主体、谓词、时点和人物认知切面，引用对应真实MCP查询回执及当前原稿/报告。"
        "若DIAGNOSIS问题已解决必须给出不超过1000字的具体method；未知登记明确UNREGISTERED，不编造canon，不接受正文或续加额度。"}
    node.setdefault("evidence_repairs", []).append(repair)
    repo.change("tasks", {"task_id": task["task_id"]},
                {"state": "WAITING_REVIEW", "updated_at": repo.now(), "lease_owner": None, "lease_expiry": None})
    kernel.tasks._emit(repo.conn, task["task_id"], task["generation"], "WAITING_REVIEW")
    node["generation"] = task["generation"]
    _archive(node)
    node.update(reason="INTERNAL_VERIFICATION_REPAIR_REQUIRED", verification_repair=repair)
    return True


def queue_verification(kernel, repo, board):
    work = board["world_ref"]["work_id"]
    for identity, node in list(board["nodes"].items()):
        request = None
        if node["stage"] == "reassessment" and node.get("output_ref"):
            record = kernel.artifact(repo, node["output_ref"], work, kind="PRODUCTION_REASSESSMENT")["payload"]
            target = record["request"]["target"]
            writer = board["nodes"][target]
            if (record["outcome"] == "REASSESSMENT_BLOCKED" and record["reason"] == "REASSESSMENT_NEEDS_INPUT"
                    and writer.get("output_ref") == record["request"]["source_draft_ref"]):
                request = request_from_blocked(board, target, node, record)
        elif node["stage"] == "facts" and node.get("reason") == "FACT_VERIFICATION_REQUIRED":
            wrapper = kernel.artifact(repo, node["output_ref"], work, kind="ROLLING_FACT_REPORT")["payload"]
            report = kernel.artifact(repo, wrapper["reconciliation_ref"], work, kind="RECONCILIATION_REPORT")["payload"]
            extraction = kernel.artifact(repo, wrapper["extraction_ref"], work, kind="ASSERTION_EXTRACTION")["payload"]
            call = repo.one("call_attempts", {"task_id": node["task_id"], "generation": node["generation"],
                                              "call_key": "supervised-candidate"})
            previous = [n for n in board["nodes"].values() if n["stage"] == "internal_verification"
                        and n["internal_case"]["source_task_id"] == node["task_id"]
                        and n["internal_case"]["source_ref"] == node["output_ref"]
                        and n["internal_case"]["report_ref"] == wrapper["reconciliation_ref"]]
            if any(n["state"] != "VERIFICATION_CONSUMED" for n in previous):
                continue
            start = max((n["internal_case"].get("question_start", 0) + len(n["internal_case"]["questions"])
                         for n in previous), default=0)
            if previous and start >= previous[-1]["internal_case"].get("question_total", len(previous[-1]["internal_case"]["questions"])):
                continue
            request = request_from_fact_report(board, node["parents"][0], node, wrapper["reconciliation_ref"],
                report, extraction, attempt_id=call["attempt_id"], question_start=start, question_limit=8)
        if request:
            key = "verification:" + request["case_hash"]
            if key in board["nodes"]:
                continue
            writer = board["nodes"][request["target"]]
            cases = [n for n in board["nodes"].values() if n["stage"] == "internal_verification"
                     and n["internal_case"]["source_draft_ref"] == request["source_draft_ref"]
                     and n["internal_case"].get("question_start", 0) == 0]
            if request.get("question_start", 0) == 0 and len(cases) >= 2:
                if writer.get("state") == "STOPPED" and writer.get("reason") == "INTERNAL_VERIFICATION_EXHAUSTED":
                    continue
                writer.update(state="STOPPED", reason="INTERNAL_VERIFICATION_EXHAUSTED")
                return request["target"]  # Persist this changed hold even when no task can be queued.
            board["nodes"][key] = {"stage": "internal_verification", "state": "PENDING", "parents": [request["target"]],
                "chapter_id": writer["chapter_id"], "world_ref": request["world_ref"], "internal_case": request}
            writer.update(state="STOPPED", reason="INTERNAL_VERIFICATION_PENDING")
            return key
    return None


def _query_gate(kernel, repo, request, task, evidence, query_ref, parsed):
    from .review_mcp import require_review_queries
    receipts = require_review_queries(kernel, repo, task, evidence)
    response = kernel.artifact(repo, evidence["response_ref"], task["work_id"])["payload"]
    refs = json.loads(response["provider_response_json"])["review_mcp_receipt_refs"]
    require(query_ref in refs, "INVALID_EVIDENCE", "Internal answers require their actual MCP receipt")
    queries = {fingerprint(ref): receipt for ref, receipt in zip(refs, receipts, strict=True)
               if receipt["tool"] == "nf_query_batch" and receipt["outcome"] == "OK"
               and receipt["result"].get("structuredContent", {}).get("all_items_succeeded") is True}
    materials = {fingerprint(receipt["arguments"]["artifact_ref"]): fingerprint(ref)
                 for ref, receipt in zip(refs, receipts, strict=True)
                 if receipt["tool"] == "nf_review_material" and receipt["outcome"] == "OK"}
    source_materials = {fingerprint(request[key]) for key in ("source_draft_ref", "report_ref")}
    require(source_materials <= materials.keys(), "INVALID_EVIDENCE", "Internal answers require actual current draft and report reads")
    questions = {question["index"]: question for question in request["questions"]}
    for answer in parsed["answers"]:
        cited = {fingerprint(ref["artifact_ref"]) for ref in answer["evidence_refs"] if ref["kind"] == "ARTIFACT"}
        cited_queries = [receipt for key, receipt in queries.items() if key in cited]
        required = _required_lookups(questions[answer["index"]])
        spans = _current_text_spans(request, answer, receipts)
        require(cited_queries, "INVALID_EVIDENCE", "Every answer must cite its actual state-query evidence")
        if required:
            lookups = [lookup for receipt in cited_queries for lookup in receipt["arguments"].get("lookups", [])]
            require(all(any(_matches_lookup(check, lookup, request) for lookup in lookups) for check in required),
                    "INVALID_EVIDENCE", "Answer query evidence does not cover this question's subject, predicate or cut")
        else:
            require(bool(cited & source_materials) or bool(cited & {materials[key] for key in source_materials}) or bool(spans),
                    "INVALID_EVIDENCE", "An unstructured answer must cite its actually read current draft or report")
        if answer["status"] == "CONFIRMED_CONFLICT":
            known = bool(required) and all(any(_lookup_known(receipt, check, request) for receipt in cited_queries)
                                           for check in required)
            require(known or len(spans) >= 2, "INVALID_EVIDENCE",
                    "Unknown query values are not conflicts; require matching known state or two verified current-text spans")
    return True


def _required_lookups(question):
    result = []
    check = question.get("world_check")
    if check and check.get("subject_id") and check.get("predicate"):
        result.append({key: check.get(key) for key in ("subject_id", "predicate", "at", "plane", "actor_id", "known_at")})
    use = question.get("knowledge_use")
    if use and use.get("mode") in {"KNOWS", "BELIEVES"}:
        result.append({"subject_id": use["subject_id"], "predicate": use["predicate"], "at": use["at"],
                       "plane": "BELIEF", "actor_id": use["actor_id"], "known_at": use["at"]})
        if use["mode"] == "KNOWS":
            result.append({"subject_id": use["subject_id"], "predicate": use["predicate"], "at": use["at"], "plane": "WORLD"})
    return result


def _matches_lookup(expected, lookup, request):
    cut = lookup.get("knowledge_cut", {})
    plane = lookup.get("plane", cut.get("plane", "WORLD"))
    if (lookup.get("subject_id") != expected["subject_id"] or lookup.get("predicate") != expected["predicate"]
            or lookup.get("at", request["story_time"]) != expected["at"] or plane != expected["plane"]):
        return False
    if plane == "BELIEF":
        return (lookup.get("actor_id", cut.get("actor_id")) == expected["actor_id"]
                and lookup.get("known_at", cut.get("known_at")) == expected["known_at"])
    return True


def _lookup_known(receipt, expected, request):
    items = {item["item_id"]: item for item in receipt["result"]["structuredContent"].get("items", [])}
    for index, lookup in enumerate(receipt["arguments"].get("lookups", [])):
        if not _matches_lookup(expected, lookup, request):
            continue
        item = items.get(str(index), {})
        if item.get("status") != "SUCCESS":
            continue
        if any(state["subject_id"] == expected["subject_id"] and state["predicate"] == expected["predicate"]
               and state["answer"].get("value_status") in {"VALUE", "NONE"}
               for state in item.get("payload", {}).get("state_items", [])):
            return True
    return False


def _current_text_spans(request, answer, receipts):
    texts = {}
    for receipt in receipts:
        if (receipt["tool"] != "nf_review_material" or receipt["outcome"] != "OK"
                or receipt["arguments"].get("artifact_ref") != request["source_draft_ref"]):
            continue
        material = receipt["result"].get("structuredContent", {})
        if (material.get("draft_ref") == request["source_draft_ref"] and isinstance(material.get("text"), str)
                and isinstance(material.get("text_ref"), dict)):
            texts[fingerprint(material["text_ref"])] = material["text"]
    verified = set()
    for source in answer["evidence_refs"]:
        if source["kind"] != "ARTIFACT" or "span" not in source:
            continue
        key = fingerprint(source["artifact_ref"])
        require(key in texts, "INVALID_EVIDENCE", "Text conflict evidence must cite the actually read current text_ref")
        try:
            verify_span(texts[key], source["span"])
        except DomainError:
            require(False, "INVALID_EVIDENCE", "Text conflict span differs from the current immutable text")
        verified.add(fingerprint([source["artifact_ref"], source["span"]]))
    return verified


def deliver_verification(kernel, repo, owner, board, node, task, evidence, candidate):
    response = kernel.artifact(repo, evidence["response_ref"], task["work_id"])["payload"]
    refs = json.loads(response["provider_response_json"])["review_mcp_receipt_refs"]
    require(refs, "REVIEW_QUERY_REQUIRED", "Internal questions require actual MCP reads")
    return record_resolution(kernel, repo, owner, board, node["internal_case"], task, evidence, candidate,
                             query_receipt_ref=refs[0], query_gate=_query_gate)


def consume_verification(kernel, repo, owner, board):
    from .production_pipeline import _artifact
    for identity, node in board["nodes"].items():
        if node["stage"] != "internal_verification" or node["state"] not in {RESOLVED, WORLD_INPUT, AUTHOR_DECISION}:
            continue
        request = node["internal_case"]
        writer = board["nodes"][request["target"]]
        if node["state"] != RESOLVED:
            writer.update(state="STOPPED", reason=node["state"])
            node["state"] = "VERIFICATION_HELD"
            return identity
        if request["source_kind"] == "DIAGNOSIS":
            from .production_reassessment import repair_budget
            if reason := repair_budget(writer, phase="revision"):
                writer.update(state="STOPPED", reason=reason)
                node["state"] = "VERIFICATION_HELD"
                return identity
            keys = [request["target"], "knowledge:" + request["target"][6:], "facts:" + request["target"][6:],
                    "review:" + writer["chapter_id"]]
            require(all(board["nodes"][k]["state"] != "ENROLLED" for k in keys),
                    "TASK_NOT_ACCEPTABLE", "Drain exact-draft readings before a verified correction")
            record = kernel.artifact(repo, node["output_ref"], request["world_ref"]["work_id"], kind="INTERNAL_REVIEW_RESOLUTION")["payload"]
            require(record["method"], "INVALID_EVIDENCE", "A verified diagnosis needs a concrete next method")
        result = consume_resolution(kernel, repo, owner, board, request, node["output_ref"])
        if result["consumed_now"]:
            if request["source_kind"] == "DIAGNOSIS":
                current = writer["output_ref"]
                for key in keys:
                    _archive(board["nodes"][key])
                writer.update(prior_draft_ref=current, revision_count=writer.get("revision_count", 0) + 1,
                    revision_feedback={"source_draft_ref": current, "report_ref": node["output_ref"],
                        "strategy_instruction": result["method"], "findings": [a for a in result["answers"]
                                                       if a["status"] == "CONFIRMED_CONFLICT"]})
            else:
                source = board["nodes"][request["source_node_id"]]
                source.setdefault("internal_resolution_refs", []).append(node["output_ref"])
                if request.get("question_start", 0) + len(request["questions"]) >= request.get("question_total", len(request["questions"])):
                    _archive(source)
                    source["reason"] = "VERIFIED_FACT_REREAD"
                    writer.update(state="DRAFT_STORED")
                    writer.pop("reason", None)
            _artifact(kernel, repo, request["world_ref"]["work_id"], owner, "INTERNAL_VERIFICATION_CONTINUATION", result)
        node["state"] = "VERIFICATION_CONSUMED"
        return identity
    return None
