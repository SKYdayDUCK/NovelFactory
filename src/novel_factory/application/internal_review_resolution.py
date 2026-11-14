"""Resolve a frozen internal diagnosis question without accepting text or world facts.

The caller owns the work lock/transaction and verifies native delivery first.
Actual MCP query proof is checked by a trusted server callback, never by a
model's assertion that it used tools. Consumption authorizes only the existing
scene's next review/strategy handoff; it does not increase any allowance.
"""
import json
from copy import deepcopy
from uuid import UUID, uuid5

from ..contracts.registry import validate_contract
from ..domain.canonical import canonical_bytes, fingerprint, strict_json_loads
from ..domain.errors import DomainError
from .kernel import require

RESOLVED = "RESOLVED_INTERNAL"
WORLD_INPUT = "NEEDS_WORLD_INPUT"
AUTHOR_DECISION = "NEEDS_AUTHOR_DECISION"
STATUSES = {"CONFIRMED_CONFLICT", "NO_CONFLICT", "STALE_REPORT", "UNREGISTERED", "EXTERNAL_DECISION_REQUIRED"}
KIND = "INTERNAL_REVIEW_RESOLUTION"
COMMAND = "INTERNAL_REVIEW_RESOLUTION_CONSUME"


def _case_hash(request):
    return fingerprint({key: value for key, value in request.items() if key != "case_hash"})


def _current(board, request):
    require(isinstance(request, dict) and request.get("case_hash") == _case_hash(request),
            "INVALID_EVIDENCE", "Internal question case hash differs")
    writer = board["nodes"].get(request["target"])
    source = board["nodes"].get(request["source_node_id"])
    require(all(request["world_ref"][key] == request["board_world_ref"][key] for key in ("work_id", "branch_id"))
            and board["world_ref"] == request["board_world_ref"] and board["story_time"] == request["story_time"]
            and writer and writer.get("output_ref") == request["source_draft_ref"]
            and writer.get("task_id") == request["writer_task_id"]
            and writer.get("generation") == request["writer_generation"]
            and writer.get("revision_count", 0) == request["revision_count"]
            and writer.get("repair_allowance", {}) == request["repair_allowance"]
            and source and source.get("output_ref") == request["source_ref"]
            and source.get("task_id") == request["source_task_id"]
            and source.get("generation") == request["source_generation"]
            and len(writer.get("reassessments", [])) == request["diagnosis_round"],
            "STALE_GENERATION", "Internal question no longer belongs to the selected draft and diagnosis slot")
    if request["source_kind"] == "DIAGNOSIS":
        require(request["world_ref"] == request["board_world_ref"] and request["diagnosis_round"] >= 1
                and writer["reassessments"][request["diagnosis_round"] - 1]["node_id"] == request["source_node_id"],
                "STALE_GENERATION", "Diagnosis no longer owns its reserved slot")
    else:
        require(request["source_kind"] == "FACT_RECONCILIATION" and source.get("stage") == "facts"
                and request["target"] in source.get("parents", []),
                "INVALID_EVIDENCE", "Fact question is outside its source scene")
    return writer


def request_from_blocked(board, target, diagnosis_node, record):
    """Freeze the exact questions from one returned REASSESSMENT_NEEDS_INPUT."""
    source = record["request"]
    require(record["outcome"] == "REASSESSMENT_BLOCKED" and record["reason"] == "REASSESSMENT_NEEDS_INPUT"
            and source["target"] == target and diagnosis_node.get("stage") == "reassessment"
            and diagnosis_node.get("task_id") == record["task_id"]
            and diagnosis_node.get("generation") == record["generation"],
            "TASK_REBIND_REQUIRED", "A returned blocked diagnosis with exact task binding is required")
    identities = [key for key, node in board["nodes"].items()
                  if node.get("task_id") == record["task_id"] and node.get("output_ref") == diagnosis_node.get("output_ref")]
    require(len(identities) == 1 and type(source["round"]) is int and source["round"] >= 1,
            "INVALID_EVIDENCE", "Diagnosis slot is ambiguous")
    questions = record["deferred_questions"]
    require(isinstance(questions, list) and 1 <= len(questions) <= 32
            and all(isinstance(question, str) and question.strip() and len(question) <= 4000 for question in questions),
            "INVALID_EVIDENCE", "Every deferred question must be preserved verbatim")
    request = {"record_version": 1, "source_kind": "DIAGNOSIS", "world_ref": source["world_ref"],
        "board_world_ref": board["world_ref"], "story_time": board["story_time"],
        "target": target, "source_draft_ref": source["source_draft_ref"], "report_ref": source["report_ref"],
        "writer_task_id": source["writer_task_id"], "writer_generation": source["writer_generation"],
        "revision_count": source["revision_count"], "repair_allowance": source["repair_allowance"],
        "diagnosis_node_id": identities[0], "diagnosis_ref": diagnosis_node["output_ref"],
        "diagnosis_task_id": record["task_id"], "diagnosis_generation": record["generation"],
        "diagnosis_attempt_id": record["attempt_id"], "diagnosis_round": source["round"],
        "source_node_id": identities[0], "source_ref": diagnosis_node["output_ref"],
        "source_task_id": record["task_id"], "source_generation": record["generation"],
        "source_attempt_id": record["attempt_id"],
        "diagnosis_request_sha256": fingerprint(source),
        "questions": [{"index": index, "question": question} for index, question in enumerate(questions)]}
    validate_contract("WorldRef", request["world_ref"])
    validate_contract("StoryPoint", request["story_time"])
    for key in ("source_draft_ref", "report_ref", "diagnosis_ref"):
        validate_contract("ArtifactRef", request[key])
    for key in ("writer_task_id", "diagnosis_task_id", "diagnosis_attempt_id"):
        validate_contract("UUID", request[key])
    request["case_hash"] = _case_hash(request)
    _current(board, request)
    return deepcopy(request)


def request_from_fact_report(board, target, facts_node, report_ref, report_payload, extraction_payload, *, attempt_id=None,
                             question_start=0, question_limit=8):
    """Refer unresolved factual checks for inquiry, preserving actual conflicts.

    report_ref/payload identify RECONCILIATION_REPORT, not its rolling wrapper.
    The source attempt must come from the persisted supervised-call ledger.
    """
    writer = board["nodes"][target]
    identities = [key for key, node in board["nodes"].items()
                  if node.get("task_id") == facts_node.get("task_id") and node.get("output_ref") == facts_node.get("output_ref")]
    attempt_id = attempt_id or facts_node.get("attempt_id")
    require(len(identities) == 1 and facts_node.get("stage") == "facts" and target in facts_node.get("parents", [])
            and report_payload["status"] == "BLOCKED" and report_payload["draft_ref"] == writer["output_ref"]
            and extraction_payload["draft_ref"] == writer["output_ref"]
            and facts_node.get("task_id") == extraction_payload["task_id"]
            and facts_node.get("generation") == extraction_payload["generation"]
            and fingerprint(report_payload) == report_ref["sha256"]
            and fingerprint(extraction_payload) == report_payload["extraction_ref"]["sha256"],
            "INVALID_EVIDENCE", "Fact question requires exact current draft, report and extraction")
    validate_contract("UUID", attempt_id)
    wr = report_payload["adopted_world_ref"]
    require(all(wr[key] == board["world_ref"][key] for key in ("work_id", "branch_id")),
            "INVALID_EVIDENCE", "Fact report belongs to another production scope")
    assertions = {item["assertion_id"]: item for item in extraction_payload["assertions"]}
    questions = []
    for index, blocker in enumerate(report_payload["hard_blockers"]):
        if not blocker["code"].startswith("UNRESOLVED_"):
            continue
        assertion_id = blocker["assertion_id"]
        require(assertion_id is None or assertion_id in assertions,
                "INVALID_EVIDENCE", "Fact blocker refers to an absent extracted assertion")
        item = assertions.get(assertion_id, {})
        questions.append({"index": len(questions), "blocker_index": index, "question": blocker["message"],
            "assertion_id": assertion_id, "span": item.get("span"), "summary": item.get("summary"),
            "code": blocker["code"], "world_check": item.get("world_check"), "knowledge_use": item.get("knowledge_use"),
            "source_draft_ref": writer["output_ref"]})
    require(1 <= len(questions) <= 128, "CAPACITY_LIMIT", "Expected one bounded set of unresolved fact checks")
    require(type(question_start) is int and type(question_limit) is int and question_limit == 8
            and 0 <= question_start < len(questions) and question_start % question_limit == 0,
            "INVALID_ARGUMENT", "Fact inquiry uses canonical consecutive windows of eight questions")
    question_total, full_questions_sha256 = len(questions), fingerprint(questions)
    questions = questions[question_start:question_start + question_limit]
    request = {"record_version": 1, "source_kind": "FACT_RECONCILIATION", "world_ref": wr,
        "board_world_ref": board["world_ref"], "story_time": board["story_time"], "target": target,
        "source_draft_ref": writer["output_ref"], "report_ref": report_ref,
        "extraction_ref": report_payload["extraction_ref"], "writer_task_id": writer["task_id"],
        "writer_generation": writer["generation"], "revision_count": writer.get("revision_count", 0),
        "repair_allowance": writer.get("repair_allowance", {}), "diagnosis_round": len(writer.get("reassessments", [])),
        "source_node_id": identities[0], "source_ref": facts_node["output_ref"], "source_task_id": facts_node["task_id"],
        "source_generation": facts_node["generation"], "source_attempt_id": attempt_id, "questions": questions,
        "question_start": question_start, "question_limit": question_limit, "question_total": question_total,
        "full_questions_sha256": full_questions_sha256,
        "report_payload_sha256": fingerprint(report_payload), "extraction_payload_sha256": fingerprint(extraction_payload)}
    for ref in _source_refs(request):
        validate_contract("ArtifactRef", ref)
    validate_contract("WorldRef", wr)
    validate_contract("StoryPoint", request["story_time"])
    request["case_hash"] = _case_hash(request)
    _current(board, request)
    return deepcopy(request)


def _source_refs(request):
    return [request["source_draft_ref"], request["report_ref"], request["source_ref"],
            *([request["extraction_ref"]] if "extraction_ref" in request else [])]


def assignment(request):
    require(request.get("case_hash") == _case_hash(request), "INVALID_EVIDENCE", "Internal question case changed")
    metadata = {key: request[key] for key in ("case_hash", "source_kind", "world_ref", "source_draft_ref",
                 "report_ref", "source_ref", "source_task_id", "source_generation", "source_attempt_id")}
    metadata.update(question_indices=[question["index"] for question in request["questions"]],
                    question_count=len(request["questions"]), question_total=request.get("question_total", len(request["questions"])))
    for key in ("question_start", "full_questions_sha256"):
        if key in request:
            metadata[key] = request[key]
    text = (
        "只核查本次内部生产问题，必须通过实际获准MCP只读工具逐题查阅固定WorldRef、原稿及报告版次。"
        "完整case及问题材料由获准MCP材料端口投递，先通读完整case，不能只凭下列精简元数据猜测问题。"
        "不可改写正文、登记新设定、接受世界事件、清除旧报告、批准完本或续加任何预算。"
        "未登记身份或谓词须明确UNREGISTERED，不能推断矛盾或编造登记；只有实际查到已登记作者保护策略并确实需要作者决定时才用EXTERNAL_DECISION_REQUIRED。"
        "区分文本内算术或表述自洽与受保护世界事实：前者可在现有授权局部核查提出修正步骤，不能捏造canon数字来充当证据。"
        "旧报告若不属于本版原稿，说明版次证据并用STALE_REPORT；仍需独立复审，不能直接宣告正文通过。"
        "按AgentDecisionOutput返回，decision为JSON字符串，严格包含case_hash、answers和可选method。"
        "answers必须按原问题顺序全部回答，每项严格包含index、status、explanation、correction、evidence_refs。"
        "status仅CONFIRMED_CONFLICT、NO_CONFLICT、STALE_REPORT、UNREGISTERED、EXTERNAL_DECISION_REQUIRED。"
        "explanation说明查询所得，最长256字；correction为核查或局部修订步骤字符串或null，最长256字，确认矛盾时必须非空；"
        "同一窗口可把各题所需主体、谓词、at及BELIEF的actor_id/known_at组合放在一次nf_query_batch中；每题引用确实覆盖该题selector的回执，不用重复同一查询。"
        "每题evidence_refs最多3条，引用实际查询回执、原文span或报告的标准EvidenceRef，详细查询保留在可信回执中，外层evidence_refs也须包含这些引用。"
        "method只提出本场下一步方法，最长1000字，不写正文；DIAGNOSIS在内部问题已解决时必须给出method，FACT_RECONCILIATION可省略。UNREGISTERED保留待世界核查。"
        "所有问题必须给结论或明确待核类型，不得用自称查过代替工具回执，不把本结果当作文学验收。\n"
        + json.dumps(metadata, ensure_ascii=False, separators=(",", ":")))
    require(len(text) <= 32000, "INVALID_ARGUMENT", "Internal question instructions exceed the frozen input contract")
    return text


def parse_resolution(request, candidate):
    """Parse bounded answers; uncertain registration never becomes a conflict."""
    require(request.get("case_hash") == _case_hash(request), "INVALID_EVIDENCE", "Internal question case changed")
    validate_contract("AgentDecisionOutput", candidate, "agent")
    try:
        parsed = strict_json_loads(candidate["decision"])
    except (TypeError, ValueError):
        raise DomainError("INVALID_ARGUMENT", "Internal answers must be strict JSON") from None
    require(isinstance(parsed, dict) and {"case_hash", "answers"} <= set(parsed)
            and set(parsed) <= {"case_hash", "answers", "method"}
            and parsed["case_hash"] == request["case_hash"],
            "INVALID_EVIDENCE", "Internal answers belong to another case")
    answers = parsed["answers"]
    require(isinstance(answers, list) and len(answers) == len(request["questions"]),
            "INVALID_EVIDENCE", "Every frozen question requires exactly one answer")
    for question, answer in zip(request["questions"], answers, strict=True):
        require(isinstance(answer, dict) and set(answer) == {"index", "status", "explanation", "correction", "evidence_refs"}
                and type(answer["index"]) is int and answer["index"] == question["index"]
                and isinstance(answer["status"], str) and answer["status"] in STATUSES
                and isinstance(answer["explanation"], str) and 0 < len(answer["explanation"].strip()) <= 256
                and (answer["correction"] is None or isinstance(answer["correction"], str)
                     and 0 < len(answer["correction"].strip()) <= 256)
                and (answer["status"] != "CONFIRMED_CONFLICT" or answer["correction"] is not None),
                "INVALID_ARGUMENT", "Invalid or incomplete internal question answer")
        refs = answer["evidence_refs"]
        require(isinstance(refs, list) and 1 <= len(refs) <= 3, "INVALID_EVIDENCE", "Each answer requires bounded actual evidence")
        for ref in refs:
            validate_contract("EvidenceRef", ref)
            require(ref in candidate["evidence_refs"], "INVALID_EVIDENCE", "Answer evidence is absent from its delivered citations")
    method = parsed.get("method")
    require(method is None or isinstance(method, str) and 0 < len(method.strip()) <= 1000,
            "INVALID_ARGUMENT", "Internal method must be a nonempty instruction of at most 1000 characters")
    statuses = {answer["status"] for answer in answers}
    outcome = (AUTHOR_DECISION if "EXTERNAL_DECISION_REQUIRED" in statuses
               else WORLD_INPUT if "UNREGISTERED" in statuses or candidate["deferred_questions"] else RESOLVED)
    require(request["source_kind"] != "DIAGNOSIS" or outcome != RESOLVED or method is not None,
            "INVALID_ARGUMENT", "A resolved diagnosis requires its concrete continuation method")
    return {"answers": deepcopy(answers), "method": method, "outcome": outcome,
            "deferred_questions": deepcopy(candidate["deferred_questions"])}


def _assignment_case_hash(text):
    """Read the final metadata object, skipping non-JSON schema repair suffixes."""
    for line in reversed(text.splitlines()):
        try:
            parsed = strict_json_loads(line)
        except (DomainError, ValueError, TypeError):
            continue
        if isinstance(parsed, dict) and "case_hash" in parsed:
            return parsed["case_hash"]
    return None


def record_resolution(kernel, repo, owner, board, request, task, evidence, candidate, *, query_receipt_ref, query_gate):
    """Caller verifies native delivery; the mandatory gate verifies actual MCP proof.

    query_gate(kernel, repo, request, task, evidence, query_receipt_ref, parsed)
    must return True after checking every answer's actual fixed-cut query proof.
    """
    wr = request["world_ref"]
    repo.one("works", {"work_id": wr["work_id"]}, lock="update")
    kernel.authorize(repo, owner, wr["work_id"], "ADMIN", wr["branch_id"])
    _current(board, request)
    if request["source_kind"] == "DIAGNOSIS":
        original = kernel.artifact(repo, request["source_ref"], wr["work_id"], kind="PRODUCTION_REASSESSMENT")["payload"]
        current = request_from_blocked(board, request["target"], board["nodes"][request["source_node_id"]], original)
    else:
        wrapper = kernel.artifact(repo, request["source_ref"], wr["work_id"], kind="ROLLING_FACT_REPORT")["payload"]
        require(wrapper["reconciliation_ref"] == request["report_ref"] and wrapper["extraction_ref"] == request["extraction_ref"],
                "INVALID_EVIDENCE", "Selected fact report or extraction changed")
        report = kernel.artifact(repo, request["report_ref"], wr["work_id"], kind="RECONCILIATION_REPORT")["payload"]
        extraction = kernel.artifact(repo, request["extraction_ref"], wr["work_id"], kind="ASSERTION_EXTRACTION")["payload"]
        current = request_from_fact_report(board, request["target"], board["nodes"][request["source_node_id"]],
                                          request["report_ref"], report, extraction, attempt_id=request["source_attempt_id"],
                                          question_start=request["question_start"], question_limit=request["question_limit"])
    require(current == request, "INVALID_EVIDENCE", "Original source or its questions differ")
    source_call = repo.one("call_attempts", {"task_id": request["source_task_id"], "generation": request["source_generation"],
                                            "call_key": "supervised-candidate"})
    require(source_call and source_call["attempt_id"] == request["source_attempt_id"],
            "INVALID_EVIDENCE", "Internal question source attempt differs from its persisted ledger")
    require((task["work_id"], task["branch_id"]) == (wr["work_id"], wr["branch_id"])
            and (task["role_id"], task["task_kind"]) in {("reviewer", "verification"), ("chief_editor", "editorial_decision")}
            and evidence["task_id"] == task["task_id"] and evidence["generation"] == task["generation"]
            and evidence["world_ref"] == wr and evidence["model_configuration"].get("adapter_kind") == "codex-cli",
            "INVALID_EVIDENCE", "Internal resolution requires its own bound Codex editorial task")
    validate_contract("UUID", evidence["attempt_id"])
    spec = repo.one("task_inputs", {"task_id": task["task_id"], "generation": task["generation"]})
    require(spec and spec["spec"]["world_ref"] == wr and spec["spec"]["story_time"] == request["story_time"]
            and all(ref in spec["spec"]["input_artifacts"] for ref in _source_refs(request)),
            "VIEW_DENIED", "Internal resolution sources are outside the frozen task input")
    require(evidence.get("input_ref") in spec["spec"]["input_artifacts"], "VIEW_DENIED", "Internal reviewer input is outside its task")
    frozen = kernel.artifact(repo, evidence["input_ref"], wr["work_id"], kind="AGENT_INPUT")["payload"]
    require((frozen["role_id"], frozen["task_kind"]) == (task["role_id"], task["task_kind"])
            and _assignment_case_hash(frozen["assignment"]) == request["case_hash"],
            "TASK_REBIND_REQUIRED", "Internal reviewer assignment is bound to another case")
    for key in ("writer_task_id", "source_task_id"):
        source = repo.one("tasks", {"task_id": request[key]}, lock="share")
        generation = request["writer_generation"] if key == "writer_task_id" else request["source_generation"]
        require(source and source["work_id"] == wr["work_id"] and source["branch_id"] == wr["branch_id"]
                and source["generation"] == generation,
                "STALE_GENERATION", "Internal question source task changed")
        expected_role = "writer" if key == "writer_task_id" else (
            "chief_editor" if request["source_kind"] == "DIAGNOSIS" else "evidence_maintainer")
        require(source["role_id"] == expected_role, "INVALID_EVIDENCE", "Internal question source role differs")
        require(source["principal_id"] != task["principal_id"] and source["grant_id"] != task["grant_id"],
                "INDEPENDENT_REVIEW_REQUIRED", "Internal query reviewer must differ from source roles")
    stored = kernel.artifact(repo, evidence["output_ref"], wr["work_id"], kind="AGENT_CANDIDATE")["payload"]
    require(stored == candidate, "INVALID_EVIDENCE", "Internal answers differ from the delivered candidate")
    parsed = parse_resolution(request, candidate)
    validate_contract("ArtifactRef", query_receipt_ref)
    kernel.artifact(repo, query_receipt_ref, wr["work_id"])
    require(callable(query_gate) and query_gate(kernel, repo, request, task, evidence, query_receipt_ref, parsed) is True,
            "INVALID_EVIDENCE", "Trusted actual MCP query proof is required for every answer")
    payload = {"record_version": 1, "request": request, **parsed, "reason": candidate["reason"],
               "query_receipt_ref": query_receipt_ref, "task_id": task["task_id"], "generation": task["generation"],
               "attempt_id": evidence["attempt_id"], "candidate_ref": evidence["output_ref"],
               "provider_calls_sent": 0, "world_effects_applied": 0, "reusable": False}
    identity = uuid5(UUID(request["source_task_id"]), "internal-resolution:" + fingerprint(
        [request["source_ref"], request["report_ref"], request.get("question_start")]))
    ref = {"artifact_id": str(identity), "artifact_revision_id": str(uuid5(identity, "revision")), "sha256": fingerprint(payload)}
    old = repo.one("artifacts", {"work_id": wr["work_id"], "artifact_revision_id": ref["artifact_revision_id"]})
    require(not old or old["sha256"] == ref["sha256"], "INTERNAL_REVIEW_ALREADY_RECORDED", "This case already has its immutable internal resolution")
    if old:
        kernel.artifact(repo, ref, wr["work_id"], kind=KIND)
    else:
        kernel.objects.put(canonical_bytes(payload))
        kernel._register_artifact(repo, ref, wr["work_id"], payload, KIND, owner.principal_id)
    return ref, parsed["outcome"]


def consume_resolution(kernel, repo, owner, board, request, resolution_ref):
    """Reserve exactly one continuation of this slot; the caller saves its board atomically.

    consumed_now=False is a historical replay. External/world decisions cannot
    consume a continuation, and neither path clears findings or renews limits.
    """
    require(request.get("case_hash") == _case_hash(request), "INVALID_EVIDENCE", "Internal question case changed")
    wr = request["world_ref"]
    repo.one("works", {"work_id": wr["work_id"]}, lock="update")
    kernel.authorize(repo, owner, wr["work_id"], "ADMIN", wr["branch_id"])
    intent = {"case_hash": request["case_hash"], "resolution_ref": resolution_ref}
    previous = repo.one("receipts", {"work_id": wr["work_id"], "branch_id": wr["branch_id"],
                                     "command_type": COMMAND, "idempotency_key": request["case_hash"]})
    if previous:
        require(previous["request_hash"] == fingerprint(intent), "IDEMPOTENCY_MISMATCH", "Case was consumed with another resolution")
        return {**previous["payload"], "consumed_now": False}
    _current(board, request)
    payload = kernel.artifact(repo, resolution_ref, wr["work_id"], kind=KIND)["payload"]
    require(payload["request"] == request and payload["outcome"] == RESOLVED,
            "INTERNAL_REVIEW_UNRESOLVED", "World or author input is still required; no continuation is authorized")
    result = {"status": "INTERNAL_RESOLUTION_CONSUMED", "case_hash": request["case_hash"],
              "resolution_ref": resolution_ref, "diagnosis_round": request["diagnosis_round"],
              "source_draft_ref": request["source_draft_ref"], "answers": payload["answers"], "method": payload["method"],
              "provider_calls_sent": 0, "world_effects_applied": 0, "allowance_changed": False}
    if request["source_kind"] == "FACT_RECONCILIATION":
        next_start = request["question_start"] + len(request["questions"])
        result.update(question_start=request["question_start"], question_total=request["question_total"],
                      full_questions_sha256=request["full_questions_sha256"],
                      next_question_start=next_start if next_start < request["question_total"] else None)
    kernel._receipt(repo, owner, wr, COMMAND, request["case_hash"], intent, result)
    return {**result, "consumed_now": True}
