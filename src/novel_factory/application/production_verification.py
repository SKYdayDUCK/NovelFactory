"""Durable internal question handoffs; no model calls or world acceptance here."""
import json

from ..domain.canonical import fingerprint
from ..domain.errors import DomainError
from ..domain.reconciliation import verify_span
from ..domain.temporal import point_key
from .internal_review_resolution import (
    AUTHOR_DECISION,
    RESOLVED,
    WORLD_COVERAGE,
    WORLD_EVIDENCE,
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
        "若DIAGNOSIS问题已解决必须给出不超过1000字的具体method；PENDING只可用PENDING_EVALUATION，已求值未知用UNKNOWN。"
        "PENDING必须按原稿scene发生区间查询，不能用board授权上限替代；全部已解决时deferred_questions必须为空。"
        "只有真实登记类ERROR才用UNREGISTERED，不能把VIEW_DENIED或UNRESOLVED说成缺登记。纯文本结论须引用本版两个独立不重叠span，"
        "STALE_REPORT须引finding_provenance_ref及具体旧report_ref。不编造canon，不接受正文或续加额度。"}
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
                     and n["internal_case"].get("question_start", 0) == 0
                     and not n["internal_case"].get("parent_case_hash")]
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
               and isinstance(receipt["result"].get("structuredContent", {}).get("items"), list)
               and not receipt["result"].get("isError")}
    materials = {fingerprint(receipt["arguments"]["artifact_ref"]): fingerprint(ref)
                 for ref, receipt in zip(refs, receipts, strict=True)
                 if receipt["tool"] == "nf_review_material" and receipt["outcome"] == "OK"}
    source_materials = {fingerprint(request[key]) for key in ("source_draft_ref", "report_ref")}
    require(source_materials <= materials.keys(), "INVALID_EVIDENCE", "Internal answers require actual current draft and report reads")
    questions = {question["index"]: question for question in request["questions"]}
    query_evidence = []
    for answer in parsed["answers"]:
        cited = {fingerprint(ref["artifact_ref"]) for ref in answer["evidence_refs"] if ref["kind"] == "ARTIFACT"}
        cited_queries = [(key, receipt) for key, receipt in queries.items() if key in cited]
        required = _required_lookups(questions[answer["index"]])
        spans = _current_text_spans(request, answer, receipts)
        require(cited_queries, "INVALID_EVIDENCE", "Every answer must cite its actual state-query evidence")
        if required:
            lookups = [lookup for _, receipt in cited_queries for lookup in receipt["arguments"].get("lookups", [])]
            require(all(any(_matches_lookup(check, lookup, request) for lookup in lookups) for check in required),
                    "INVALID_EVIDENCE", "Answer query evidence does not cover this question's subject, predicate or cut")
        elif answer["status"] != "STALE_REPORT":
            require(bool(cited & source_materials) or bool(cited & {materials[key] for key in source_materials}) or bool(spans),
                    "INVALID_EVIDENCE", "An unstructured answer must cite its actually read current draft or report")
        observations = []
        for key, receipt in cited_queries:
            actual_ref = next(ref for ref in refs if fingerprint(ref) == key)
            observations.extend({**row, "query_receipt_ref": actual_ref} for row in _query_observations(receipt, request)
                                if not required or any(_matches_lookup(check, row["selector"], request) for check in required))
        require(observations, "INVALID_EVIDENCE", "No exact STATE item or authorized registration error covers this answer")
        statuses = {row["status"] for row in observations}
        text_proof = _independent_spans(spans)
        known = all(any(_matches_lookup(check, row["selector"], request) and row["status"] in {"VALUE", "NONE", "NOT_APPLICABLE"}
                        for row in observations) for check in required) if required else bool(statuses <= {"VALUE", "NONE", "NOT_APPLICABLE"})
        comparisons = [_comparison(check, row) for check in required for row in observations
                       if _matches_lookup(check, row["selector"], request)]
        if answer["status"] == "UNREGISTERED":
            require("UNREGISTERED" in statuses and not statuses & {"PENDING", "UNKNOWN", "ERROR"}, "INVALID_EVIDENCE",
                    "UNREGISTERED requires an exact registration error; pending, unknown or denied queries do not prove missing registration")
        elif answer["status"] == "PENDING_EVALUATION":
            require("PENDING" in statuses, "INVALID_EVIDENCE", "Pending evaluation needs an exact PENDING state answer")
            draft = kernel.artifact(repo, request["source_draft_ref"], task["work_id"], kind="NARRATIVE_DRAFT")["payload"]
            scene = kernel.artifact(repo, draft["scene_ref"], task["work_id"], kind="SCENE_PLAN")["payload"]
            interval = scene["scene_contract"]["story_interval"]
            require(all(point_key(interval["start"]) <= point_key(row["selector"]["at"]) < point_key(interval["end"])
                        for row in observations if row["status"] == "PENDING"),
                    "INVALID_EVIDENCE", "Pending coverage must use the current scene interval, not the board authorization time")
        elif answer["status"] == "UNKNOWN":
            require("UNKNOWN" in statuses and "PENDING" not in statuses, "INVALID_EVIDENCE",
                    "Unknown knowledge must be evaluated, distinct from pending world coverage")
        elif answer["status"] == "CONFIRMED_CONFLICT":
            require((known and False in comparisons) or text_proof, "INVALID_EVIDENCE",
                    "A conflict needs contradictory known state or two independent exact current-text spans")
        elif answer["status"] == "NO_CONFLICT":
            require((known and False not in comparisons) or (not known and text_proof), "INVALID_EVIDENCE",
                    "Pending or unknown world state is not a no-conflict proof; cite two independent current-text spans")
        elif answer["status"] == "STALE_REPORT":
            require(_stale_report(request, answer, receipts), "INVALID_EVIDENCE",
                    "A stale report needs actually read provenance and an explicitly cited old report version")
        elif answer["status"] == "EXTERNAL_DECISION_REQUIRED":
            require(_author_protection(kernel, repo, request, task, answer, receipts), "INVALID_EVIDENCE",
                    "Author decisions require the actual current protected commitment, explicitly identified and read through MCP")
        query_evidence.extend({"answer_index": answer["index"], "answer_status": answer["status"], **row} for row in observations)
    parsed["query_evidence"] = query_evidence
    return True


def _required_lookups(question):
    result = []
    check = question.get("world_check")
    if check and check.get("subject_id") and check.get("predicate"):
        result.append({key: check.get(key) for key in ("subject_id", "predicate", "at", "plane", "actor_id", "known_at", "expected_status", "expected_value")})
    use = question.get("knowledge_use")
    if use and use.get("mode") in {"KNOWS", "BELIEVES"}:
        result.append({"subject_id": use["subject_id"], "predicate": use["predicate"], "at": use["at"],
                       "plane": "BELIEF", "actor_id": use["actor_id"], "known_at": use["at"],
                       "expected_status": "VALUE", "expected_value": use["value"]})
        if use["mode"] == "KNOWS":
            result.append({"subject_id": use["subject_id"], "predicate": use["predicate"], "at": use["at"], "plane": "WORLD",
                           "expected_status": "VALUE", "expected_value": use["value"]})
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
    return any(_matches_lookup(expected, row["selector"], request) and row["status"] in {"VALUE", "NONE", "NOT_APPLICABLE"}
               for row in _query_observations(receipt, request))


def _query_observations(receipt, request):
    """Decode exact actual STATE items; UNRESOLVED alone never means unregistered."""
    items = {item["item_id"]: item for item in receipt["result"]["structuredContent"].get("items", [])}
    result = []
    registration_errors = {"ENTITY_NOT_FOUND", "UNKNOWN_PREDICATE"}
    for index, lookup in enumerate(receipt["arguments"].get("lookups", [])):
        plane = lookup.get("plane", lookup.get("knowledge_cut", {}).get("plane", "WORLD"))
        selector = {"subject_id": lookup["subject_id"], "predicate": lookup["predicate"],
                    "at": lookup.get("at", request["story_time"]), "plane": plane}
        if plane == "BELIEF":
            selector.update(actor_id=lookup.get("actor_id", lookup.get("knowledge_cut", {}).get("actor_id")),
                            known_at=lookup.get("known_at", lookup.get("knowledge_cut", {}).get("known_at")))
        item = items.get(str(index), {})
        if item.get("status") == "ERROR":
            code = item.get("error", {}).get("code")
            result.append({"selector": selector, "world_ref": request["world_ref"], "error_code": code,
                           "status": "UNREGISTERED" if code in registration_errors else "ERROR"})
            continue
        if item.get("status") != "SUCCESS" or item.get("payload", {}).get("kind") != "STATE":
            continue
        for state in item["payload"].get("state_items", []):
            if (state.get("subject_id"), state.get("predicate")) != (selector["subject_id"], selector["predicate"]):
                continue
            answer = state.get("answer", {})
            require(answer.get("world_ref") == request["world_ref"] and answer.get("story_time") == selector["at"]
                    and answer.get("plane") == plane and answer.get("freshness") == "CURRENT",
                    "INVALID_EVIDENCE", "Query state differs from its fixed world, time or plane")
            value, evaluation = answer.get("value_status"), answer.get("evaluation_status")
            if evaluation == "PENDING" and value == "UNRESOLVED" and answer.get("knowledge_status") == "UNKNOWN" and answer.get("truth_status") == "UNDETERMINED":
                status = "PENDING"
            elif evaluation == "EVALUATED" and value in {"VALUE", "NONE", "NOT_APPLICABLE"} and answer.get("knowledge_status") == "KNOWN" and answer.get("truth_status") == "DETERMINED":
                status = value
            elif evaluation == "EVALUATED" and value == "UNRESOLVED" and answer.get("knowledge_status") == "UNKNOWN" and answer.get("truth_status") == "UNDETERMINED":
                status = "UNKNOWN"
            else:
                status = "ERROR"
            result.append({"selector": selector, "world_ref": answer["world_ref"], "status": status,
                           "value_status": value, "evaluation_status": evaluation, "data": answer.get("data")})
    return result


def _comparison(check, observation):
    if observation["status"] not in {"VALUE", "NONE", "NOT_APPLICABLE"} or check.get("expected_status") is None:
        return None
    return observation["value_status"] == check["expected_status"] and observation.get("data") == check.get("expected_value")


def _independent_spans(spans):
    return any(key == other_key and (end <= other_start or other_end <= start)
               for key, start, end in spans for other_key, other_start, other_end in spans)


def _author_protection(kernel, repo, request, task, answer, receipts):
    """A model cannot invent an author veto or borrow another volume's policy."""
    draft = kernel.artifact(repo, request["source_draft_ref"], task["work_id"], kind="NARRATIVE_DRAFT")["payload"]
    scene = kernel.artifact(repo, draft["scene_ref"], task["work_id"], kind="SCENE_PLAN")["payload"]
    plan = kernel.artifact(repo, scene["plan_ref"], task["work_id"], kind="PLAN_REVISION")["payload"]
    contract_ref = plan["volume_contract_ref"]
    contract = kernel.artifact(repo, contract_ref, task["work_id"], kind="VOLUME_CONTRACT")["payload"]
    clauses = contract["protected_commitments"]
    cited = {fingerprint(reference["artifact_ref"]) for reference in answer["evidence_refs"] if reference["kind"] == "ARTIFACT"}
    if not clauses or fingerprint(contract_ref) not in cited or plan["volume_id"] != contract["volume_id"]:
        return False
    actual = any(receipt["tool"] == "nf_review_material" and receipt["outcome"] == "OK"
        and receipt["arguments"].get("artifact_ref") == contract_ref
        and receipt["result"].get("structuredContent", {}).get("projection") == "REGISTERED_AUTHOR_PROTECTION"
        and receipt["result"]["structuredContent"].get("source_ref") == contract_ref
        and receipt["result"]["structuredContent"].get("volume_id") == contract["volume_id"]
        and receipt["result"]["structuredContent"].get("contract_revision_id") == contract["contract_revision_id"]
        and receipt["result"]["structuredContent"].get("protected_commitments") == clauses
        for receipt in receipts)
    if not actual:
        return False
    explanation = answer["explanation"]
    return any((isinstance(clause, dict) and isinstance(clause.get("description"), str)
                and 0 < len(clause["description"]) <= 128 and clause["description"] in explanation)
               or (isinstance(clause, dict) and isinstance(clause.get("commitment_id"), str)
                   and clause["commitment_id"] in explanation)
               or (f"protected_commitments[{index}]" in explanation and fingerprint(clause) in explanation)
               for index, clause in enumerate(clauses))


def _stale_report(request, answer, receipts):
    cited = {fingerprint(source["artifact_ref"]) for source in answer["evidence_refs"] if source["kind"] == "ARTIFACT"}
    current_text = next((receipt["result"].get("structuredContent", {}).get("text_ref") for receipt in receipts
        if receipt["tool"] == "nf_review_material" and receipt["outcome"] == "OK"
        and receipt["arguments"].get("artifact_ref") == request["source_draft_ref"]), None)
    for receipt in receipts:
        if (receipt["tool"] != "nf_review_material" or receipt["outcome"] != "OK"
                or receipt["arguments"].get("artifact_ref") != request["report_ref"]):
            continue
        material = receipt["result"].get("structuredContent", {})
        provenance = material.get("finding_provenance", {})
        reference = material.get("finding_provenance_ref")
        if (not reference or fingerprint(reference) not in cited or provenance.get("version") != 1
                or provenance.get("case_hash") != request["case_hash"]
                or provenance.get("current_draft_ref") != request["source_draft_ref"]
                or not current_text or provenance.get("current_text_ref") != current_text
                or provenance.get("report_ref") != request["report_ref"]):
            continue
        selected = [row for row in provenance.get("candidates", []) if any(
            isinstance(row.get(key), dict) and fingerprint(row[key]) in cited for key in ("report_ref", "reconciliation_ref"))]
        if selected and all(row.get("matched_finding_indices") and row.get("matches_current") is False
                            and row.get("source_draft_ref") != request["source_draft_ref"] for row in selected):
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
        verified.add((key, source["span"]["start"], source["span"]["end"]))
    return verified


def deliver_verification(kernel, repo, owner, board, node, task, evidence, candidate):
    response = kernel.artifact(repo, evidence["response_ref"], task["work_id"])["payload"]
    refs = json.loads(response["provider_response_json"])["review_mcp_receipt_refs"]
    require(refs, "REVIEW_QUERY_REQUIRED", "Internal questions require actual MCP reads")
    query_ref = next((ref for ref in refs if kernel.artifact(repo, ref, task["work_id"])["payload"].get("tool") == "nf_query_batch"), None)
    require(query_ref, "REVIEW_QUERY_REQUIRED", "Internal questions require an actual query receipt")
    return record_resolution(kernel, repo, owner, board, node["internal_case"], task, evidence, candidate,
                             query_receipt_ref=query_ref, query_gate=_query_gate)


def _recover_coverage(kernel, repo, owner, board, identity, node):
    """A changed authoritative version may wake a program wait; no model retry."""
    from .internal_review_resolution import request_after_coverage
    from .world_coverage_recovery import plan_coverage, recover_coverage
    case = node["internal_case"]
    writer = board["nodes"][case["target"]]
    if case.get("parent_case_hash"):
        node.update(state="WAITING_WORLD_COVERAGE", reason="COVERAGE_RETRY_EXHAUSTED")
        writer.update(state="STOPPED", reason="COVERAGE_RETRY_EXHAUSTED")
        return identity
    head = kernel.head(repo, board["world_ref"]["work_id"], board["world_ref"]["branch_id"])
    if node.get("coverage_observed_head") == head:
        return None
    node["coverage_observed_head"] = head
    node["coverage_program_checks"] = node.get("coverage_program_checks", 0) + 1
    record = kernel.artifact(repo, node["output_ref"], board["world_ref"]["work_id"], kind="INTERNAL_REVIEW_RESOLUTION")["payload"]
    receipts = []
    for row in record.get("query_evidence", []):
        if row["answer_status"] == "PENDING_EVALUATION" and row["status"] == "PENDING" and row["query_receipt_ref"] not in receipts:
            receipts.append(row["query_receipt_ref"])
    try:
        request = plan_coverage(kernel, repo, owner, board, case["target"], parent_case_hash=case["case_hash"], query_receipt_refs=receipts)
        node["coverage_request"] = request
        recovered = recover_coverage(kernel, owner, request, "internal-coverage:" + case["case_hash"] + ":" + request["request_sha256"][:16])
    except DomainError as error:
        node.update(state="WAITING_WORLD_COVERAGE", reason=error.code)
        writer.update(state="STOPPED", reason=WORLD_COVERAGE)
        return identity
    if recovered["status"] != "WORLD_COVERAGE_RECOVERED":
        node.update(state="WAITING_WORLD_COVERAGE", reason=recovered["status"], coverage_wait=recovered)
        writer.update(state="STOPPED", reason=WORLD_COVERAGE)
        return identity
    child_case = request_after_coverage(kernel, repo, board, node["output_ref"],
                                        recovered["reading_world_ref"], recovered["coverage_transition_ref"])
    child_id = "verification:" + child_case["case_hash"]
    require(child_id not in board["nodes"], "INTERNAL_REVIEW_ALREADY_RECORDED", "Coverage child inquiry is already enrolled")
    board["nodes"][child_id] = {"stage": "internal_verification", "state": "PENDING", "parents": [case["target"]],
        "chapter_id": writer["chapter_id"], "world_ref": recovered["reading_world_ref"], "internal_case": child_case,
        "reading_world_ref": recovered["reading_world_ref"], "coverage_transition_ref": recovered["coverage_transition_ref"]}
    node.update(state="COVERAGE_TRANSITIONED", reason="INDEPENDENT_COVERAGE_REREAD", child_node_id=child_id,
                coverage_transition_ref=recovered["coverage_transition_ref"])
    writer.update(state="STOPPED", reason=WORLD_COVERAGE)
    return identity


def consume_verification(kernel, repo, owner, board):
    from .production_pipeline import _artifact
    for identity, node in board["nodes"].items():
        if node["stage"] != "internal_verification":
            continue
        if node["state"] in {WORLD_COVERAGE, "WAITING_WORLD_COVERAGE"}:
            if changed := _recover_coverage(kernel, repo, owner, board, identity, node):
                return changed
            continue
        if node["state"] not in {RESOLVED, WORLD_INPUT, AUTHOR_DECISION, WORLD_EVIDENCE}:
            continue
        request = node["internal_case"]
        writer = board["nodes"][request["target"]]
        if node["state"] != RESOLVED:
            writer.update(state="STOPPED", reason=node["state"])
            node.update(state={WORLD_COVERAGE: "WAITING_WORLD_COVERAGE", WORLD_EVIDENCE: "WAITING_WORLD_EVIDENCE"}.get(node["state"], "VERIFICATION_HELD"),
                        reason=node["state"])
            return identity
        if request["source_kind"] == "DIAGNOSIS" and not request.get("parent_case_hash"):
            from .production_reassessment import repair_budget
            if reason := repair_budget(writer, phase="revision"):
                writer.update(state="STOPPED", reason=reason)
                node["state"] = "VERIFICATION_HELD"
                return identity
        record = kernel.artifact(repo, node["output_ref"], request["world_ref"]["work_id"], kind="INTERNAL_REVIEW_RESOLUTION")["payload"]
        extent_findings = []
        if request["source_kind"] == "DIAGNOSIS" and writer.get("han_characters"):
            from .production_extent import _count_han
            draft = kernel.artifact(repo, writer["output_ref"], request["world_ref"]["work_id"], kind="NARRATIVE_DRAFT")["payload"]
            actual = _count_han(kernel.objects.read(draft["text_ref"]["sha256"]).decode("utf-8"))
            extent = writer["han_characters"]
            if not extent["min"] <= actual <= extent["max"]:
                extent_findings.append({"actual_han": actual, "target_han": extent})
        same_draft_coverage = (request.get("parent_case_hash") and not extent_findings
                              and not any(answer["status"] == "CONFIRMED_CONFLICT" for answer in record["answers"]))
        if request["source_kind"] == "DIAGNOSIS":
            from .production_reassessment import repair_budget
            if request.get("parent_case_hash") and not same_draft_coverage and (reason := repair_budget(writer, phase="revision")):
                writer.update(state="STOPPED", reason=reason)
                node["state"] = "VERIFICATION_HELD"
                return identity
            keys = [request["target"], "knowledge:" + request["target"][6:], "facts:" + request["target"][6:],
                    "review:" + writer["chapter_id"]]
            require(all(board["nodes"][k]["state"] != "ENROLLED" for k in keys),
                    "TASK_NOT_ACCEPTABLE", "Drain exact-draft readings before a verified correction")
            require(record["method"], "INVALID_EVIDENCE", "A verified diagnosis needs a concrete next method")
        result = consume_resolution(kernel, repo, owner, board, request, node["output_ref"])
        if result["consumed_now"]:
            if same_draft_coverage:
                suffix = request["target"][6:]
                for key in ("knowledge:" + suffix, "facts:" + suffix, "review:" + writer["chapter_id"]):
                    reading = board["nodes"][key]
                    require(reading["state"] != "ENROLLED", "TASK_NOT_ACCEPTABLE", "Drain the old-cut scene reading before coverage continuation")
                    _archive(reading)
                    reading.update(reading_world_ref=request["world_ref"], coverage_transition_ref=request["coverage_transition_ref"])
                board["nodes"]["facts:" + suffix].setdefault("internal_resolution_refs", []).append(node["output_ref"])
                writer.update(state="DRAFT_STORED", coverage_continuation={"resolution_ref": node["output_ref"],
                    "parent_case_hash": request["parent_case_hash"], "method": result["method"],
                    "source_world_ref": board["world_ref"], "adopted_world_ref": request["world_ref"],
                    "coverage_transition_ref": request["coverage_transition_ref"], "method_validation": "INTERNAL_QUERY_ONLY"})
                writer.pop("reason", None)
                _artifact(kernel, repo, request["world_ref"]["work_id"], owner, "INTERNAL_VERIFICATION_CONTINUATION", result)
                node.update(state="VERIFICATION_CONSUMED", reason="SAME_DRAFT_COVERAGE_REREAD")
                return identity
            if request["source_kind"] == "DIAGNOSIS":
                current = writer["output_ref"]
                for key in keys:
                    _archive(board["nodes"][key])
                writer.update(prior_draft_ref=current, revision_count=writer.get("revision_count", 0) + 1,
                    revision_feedback={"source_draft_ref": current, "report_ref": node["output_ref"],
                        "strategy_instruction": result["method"], "findings": [a for a in result["answers"]
                                                       if a["status"] == "CONFIRMED_CONFLICT"] + extent_findings})
                if request.get("parent_case_hash"):
                    writer["revision_feedback"].update(adopted_world_ref=request["world_ref"], coverage_transition_ref=request["coverage_transition_ref"])
                    for key in keys[1:]:
                        board["nodes"][key].update(reading_world_ref=request["world_ref"], coverage_transition_ref=request["coverage_transition_ref"])
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
