"""Map an independent full-text assertion reading into the native reconciliation service."""

import json
import re
from functools import lru_cache
from uuid import NAMESPACE_URL, uuid5

from ..contracts.registry import contract_diagnostic
from ..domain.canonical import fingerprint, strict_json_loads
from ..domain.errors import DomainError
from ..domain.reconciliation import span_for
from ..domain.review_gate import blocking_review_issues
from .authorization import Principal
from .kernel import require
from .narrative import NarrativeService
from .prompt_schema import prompt_schema


@lru_cache(maxsize=1)
def assignment():
    # The kernel derives the native span and UUID from a unique exact quotation.
    # Keep all semantic fields, conditional constraints and their reachable definitions.
    item = json.loads(prompt_schema("AssertionCandidate", "narrative"))
    definitions = item.pop("$defs", {})
    for derived in ("assertion_id", "span"):
        del item["properties"][derived]
        item["required"].remove(derived)
    item["properties"]["quote"] = {"type": "string", "minLength": 1}
    item["properties"]["passage_ids"] = {"type": "array", "minItems": 1, "uniqueItems": True,
                                        "items": {"type": "integer", "minimum": 1}}
    item.setdefault("allOf", []).append({"oneOf": [{"required": ["quote"]}, {"required": ["passage_ids"]}]})
    schema = {"type": "object", "additionalProperties": False, "required": ["assertions"],
              "properties": {"assertions": {"type": "array", "minItems": 1, "maxItems": 512,
                                             "items": item}}, "$defs": definitions}
    return (
        "逐句独立审读获准正文，找出人物认知、事实变化、事件、披露、重要资源以及后果。"
        "在 AgentTextOutput.text 字符串内编码一个 JSON 对象，唯一键 assertions 为数组。"
        "每项用 passage_ids 指出材料给出的连续段号（从 1 起）；也可改用 quote 逐字唯一摘引。另含 classification、summary、event_ref、world_check、"
        "knowledge_use、proposed_command、disclosure_id、declared_delta_index。无依据的引用填 null。"
        "按原文顺序引用，覆盖整场正文的每个非空白字符，包括标点。摘引须逐字复制对应正文，"
        "不可用省略号或摘要替代；可将连续同类描写合并为一段。有台词的段落区分说法与已证事实。"
        "只标识确有文本依据的非持久感官细节为 SURFACE_DETAIL。缺乏事实证据时标为 UNCERTAIN。"
        "普通台词、人物推测或设备显示仅作为本稿有归属的说法、并不主张其内容为世界真值时，可用ATTRIBUTED_STATEMENT。"
        "该类event_ref、world_check、knowledge_use、proposed_command、disclosure_id、declared_delta_index必须全部null，"
        "须由本版独立认知审读与实际MCP查询证明；重要资源、持久状态、必需事件或披露变化不得这样降格。"
        "SPEECH_OR_BELIEF 需要完整 knowledge_use，区分有来源的知识、推测与外部发言。"
        "若所需来源缺失，保留未决事实并说明缺少的证据，勿伪造。declared_delta_index 每项只可映射一次。"
        "FACT_READING_SCOPE 的实体名称仅是固定版本登记映射，不证明状态已成立。"
        "正文材料的scene_story_interval给出真实发生区间；查询world_check必须使用该区间内原文对应时点，"
        "不能把任务story_time或board时间上限当成场景发生点。"
        "其中 UNKNOWN 或 PENDING 是明确的核查结果，按原义保留为 UNCERTAIN，不能推成真或假。"
        "本任务交付完整核查报告，不要求先解决所有未决剧情事实；未决问题写在相应断言的 summary，"
        "仍可完成报告。只有缺少正文、任务身份或契约等使报告本身无法形成时才用 NEEDS_INPUT。"
        "对照材料的 required_event_refs 与 declared_important_deltas。已表现的事件引用对应 EventRef；"
        "写作方声明的重要变化用零基 declared_delta_index 对应。正文缺少所需事件或证据不足时如实标出。"
        "不要自行创造正式事件或更改世界。NEW_WORLD_EFFECT 必须有非空 proposed_command，按 TemplateCommand 中一个正式模板填写 template_id 和 args。不得自编模板名、参数键或实体编号。需要正式变化但授权材料不足以构造命令，应如实标记 UNCERTAIN 交给核查，不能降格为表面描写。"
        "\nAgentTextOutput.text 内层 JSON Schema：" + json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
    )


def epistemic_blockers(issues):
    """Retain the complete reading. Gate advancement on active mandatory findings."""
    return blocking_review_issues(issues)


def record_knowledge(kernel, repo, owner, board, node, task, evidence, candidate):
    from .production_pipeline import _artifact

    parent = board["nodes"][node["parents"][0]]
    draft_ref = parent["output_ref"]
    reader = Principal(task["grant_id"], task["principal_id"])
    delivered = NarrativeService(kernel).read_text(
        reader, task["task_id"], task["generation"], draft_ref
    )
    clear = not epistemic_blockers(candidate["issues"])
    wr = board["world_ref"]
    if node.get("reading_world_ref") is not None:
        from .world_coverage_recovery import validate_reading_world
        wr = validate_reading_world(kernel, repo, board, node)
    spec = repo.one("task_inputs", {"task_id": task["task_id"], "generation": task["generation"]})["spec"]
    require(spec["world_ref"] == wr, "TASK_REBIND_REQUIRED", "Epistemic reading must use its explicit world cut")
    receipt = {
        "world_ref": wr,
        "draft_ref": draft_ref,
        "text_ref": delivered["text_ref"],
        "delivery_ref": delivered["delivery_ref"],
        "reader_task": task["task_id"],
        "generation": task["generation"],
        "attempt_id": evidence["attempt_id"],
        "candidate_ref": evidence["output_ref"],
        "status": "CLEAR" if clear else "BLOCKED",
    }
    ref = _artifact(kernel, repo, task["work_id"], owner, "EPISTEMIC_READING", receipt)
    job = repo.one("supervisor_jobs", {"task_id": task["task_id"]})
    if clear and job["config"].get("required_review_mcp"):
        from .planning import PlanningService
        PlanningService(kernel)._select(repo, wr,
            "MCP_EPISTEMIC:" + draft_ref["artifact_revision_id"], ref)
    return ref, "KNOWLEDGE_CLEAR" if clear else "STOPPED"


def check_knowledge(kernel, repo, board, node, draft_ref):
    from .semantic_dependencies import check_task_dependencies

    reading = board["nodes"][node["knowledge_node"]]
    require(
        reading["state"] == "KNOWLEDGE_CLEAR",
        "EPISTEMIC_REVIEW_REQUIRED",
        "No clear epistemic reading",
    )
    wr = board["world_ref"]
    if node.get("reading_world_ref") is not None or reading.get("reading_world_ref") is not None:
        from .world_coverage_recovery import validate_reading_world
        wr = validate_reading_world(kernel, repo, board, node)
        require(validate_reading_world(kernel, repo, board, reading) == wr,
                "EPISTEMIC_REVIEW_REQUIRED", "Fact and epistemic readings must share the adopted coverage cut")
    record = kernel.artifact(repo, reading["output_ref"], wr["work_id"], kind="EPISTEMIC_READING")[
        "payload"
    ]
    task = repo.one(
        "tasks", {"task_id": reading["task_id"], "work_id": wr["work_id"]}, lock="share"
    )
    require(
        task
        and task["role_id"] == "reviewer"
        and task["task_kind"] == "epistemic"
        and task["state"] == "ACCEPTED"
        and task["task_id"] == record["reader_task"]
        and task["generation"] == record["generation"]
        and record["status"] == "CLEAR"
        and record["draft_ref"] == draft_ref
        and record["world_ref"] == wr,
        "EPISTEMIC_REVIEW_REQUIRED",
        "The review no longer binds this manuscript",
    )
    check_task_dependencies(repo, task["task_id"], task["generation"])
    reader = Principal(task["grant_id"], task["principal_id"])
    _, _, _, draft = NarrativeService(kernel)._reader_binding(
        repo, reader, task["task_id"], task["generation"], draft_ref, allow_accepted=True
    )
    require(
        record["text_ref"] == draft["text_ref"],
        "EPISTEMIC_REVIEW_REQUIRED",
        "Reviewed text changed",
    )
    return reading["output_ref"]


def passage_index(body):
    """Only the assigned immutable text determines the public passage numbers."""
    entries = []
    for match in re.finditer(r"[^。！？\n]{1,160}[。！？]?|[。！？]", body):
        if match.group().strip():
            entries.append({"id": len(entries) + 1, "start": match.start(), "end": match.end(),
                            "text": match.group()})
    return entries


class ReadingFormatError(DomainError):
    def __init__(self, diagnostic):
        super().__init__("INVALID_EVIDENCE", "Invalid assertion reading representation")
        self.diagnostic = diagnostic


def extract_assertions(body, value, key, *, declared_count=None):
    try:
        parsed = strict_json_loads(value)
    except DomainError as error:
        raise ReadingFormatError({"keyword": "jsonSyntax"}) from error
    if (not isinstance(parsed, dict) or set(parsed) != {"assertions"} or
            not isinstance(parsed["assertions"], list) or not 1 <= len(parsed["assertions"]) <= 512):
        raise ReadingFormatError({"keyword": "assertionsArray"})
    passages = passage_index(body)
    assertions, coverage, declared = [], [], set()
    for index, raw in enumerate(parsed["assertions"]):
        where = {"assertion_index": index}
        if not isinstance(raw, dict) or ("quote" in raw) == ("passage_ids" in raw):
            raise ReadingFormatError({**where, "keyword": "chooseQuoteOrPassageIds"})
        if "passage_ids" in raw:
            ids = raw["passage_ids"]
            if (not isinstance(ids, list) or not ids or len(ids) > len(passages)
                    or any(type(n) is not int or n < 1 or n > len(passages) for n in ids)
                    or any(n != ids[0] + i for i, n in enumerate(ids))):
                raise ReadingFormatError({**where, "keyword": "contiguousPassageIds"})
            begin, end = passages[ids[0] - 1]["start"], passages[ids[-1] - 1]["end"]
        else:
            quote = raw["quote"]
            if not isinstance(quote, str) or not quote.strip() or body.count(quote) != 1:
                raise ReadingFormatError({**where, "keyword": "uniqueExactQuote"})
            begin, end = body.index(quote), body.index(quote) + len(quote)
        if "assertion_id" in raw or "span" in raw:
            raise ReadingFormatError({**where, "keyword": "kernelDerivedFields"})
        assertion = {k: v for k, v in raw.items() if k not in {"quote", "passage_ids"}}
        assertion.update(assertion_id=str(uuid5(NAMESPACE_URL, key + ":" + str(index))),
                         span=span_for(body, begin, end))
        if diagnostic := contract_diagnostic("AssertionCandidate", assertion, "narrative"):
            raise ReadingFormatError({**where, **diagnostic})
        if assertion["classification"] == "SPEECH_OR_BELIEF" and assertion["knowledge_use"] is None:
            raise ReadingFormatError({**where, "keyword": "missingSpeechOrBeliefBinding"})
        delta = assertion["declared_delta_index"]
        if delta is not None:
            if delta in declared or (declared_count is not None and delta >= declared_count):
                raise ReadingFormatError({**where, "keyword": "uniqueDeclaredDeltaIndex", "declared_count": declared_count})
            declared.add(delta)
        assertions.append(assertion)
        coverage.append((begin, end))
    cursor = 0
    for begin, end in sorted(coverage) + [(len(body), len(body))]:
        if body[cursor:begin].strip():
            raise ReadingFormatError({"keyword": "unreadPassage", "start": cursor, "end": begin})
        cursor = max(cursor, end)
    return assertions


def audit(kernel, repo, owner, board, node, task, candidate, *, defer_accept=False):
    require(type(defer_accept) is bool, "INVALID_ARGUMENT", "Invalid internal acceptance mode")
    source = board["nodes"][node["parents"][0]]
    draft_ref = source["output_ref"]
    knowledge_ref = None
    if "knowledge_node" in node:
        knowledge = board["nodes"][node["knowledge_node"]]
        reader = repo.one("tasks", {"task_id": knowledge["task_id"]})
        require(
            reader["principal_id"] != task["principal_id"]
            and reader["grant_id"] != task["grant_id"],
            "INDEPENDENT_REVIEW_REQUIRED",
            "Fact and epistemic readers require independent identities",
        )
        knowledge_ref = check_knowledge(kernel, repo, board, node, draft_ref)
    actor = Principal(task["grant_id"], task["principal_id"])
    service = NarrativeService(kernel)
    key = "rolling-facts:" + task["task_id"] + ":" + str(task["generation"])
    delivered = service.read_text(actor, task["task_id"], task["generation"], draft_ref)
    _, _, _, draft = service._reader_binding(repo, actor, task["task_id"], task["generation"], draft_ref)
    assertions = extract_assertions(delivered["text"], candidate["text"], key,
                                    declared_count=len(draft["declared_important_deltas"]))
    extraction = service.submit_extraction(
        actor,
        task["task_id"],
        task["generation"],
        draft_ref,
        delivered["delivery_ref"],
        assertions,
        key + ":extract",
    )
    wr = kernel.head(repo, board["world_ref"]["work_id"], board["world_ref"]["branch_id"])
    result = service.reconcile(owner, draft_ref, extraction, wr, key + ":reconcile")
    evidence = {
        "extraction_ref": extraction,
        "text_delivery_ref": delivered["delivery_ref"],
        **({"knowledge_ref": knowledge_ref} if knowledge_ref else {}),
        **result,
    }
    if result["status"] == "CLEAR" and not defer_accept:
        evidence["acceptance_ref"] = service.accept_narrative(
            owner,
            draft_ref,
            result["reconciliation_ref"],
            wr,
            key + ":accept",
            alignment_ref=result["alignment_ref"],
        )
    return evidence


def validate_native_fact_source(kernel, repo, task, extraction, draft):
    """Bind actual native fact-reader output to the exact stored assertions."""
    from .agent_gateway import AgentGateway
    from .candidate_delivery import CandidateDelivery

    job = repo.one("supervisor_jobs", {"task_id": task["task_id"]})
    require(task["state"] == "ACCEPTED"
            and (task["role_id"], task["task_kind"]) == ("evidence_maintainer", "assertion_extract")
            and task["task_id"] == extraction["task_id"] and task["generation"] == extraction["generation"]
            and job and job["config"].get("required_review_mcp"),
            "REVIEW_QUERY_REQUIRED", "An exact completed native fact-reading task is required")
    evidence, candidate = CandidateDelivery(kernel, AgentGateway(kernel, {})).audited_candidate(
        repo, task, production_accounting=True)
    body = kernel.objects.read(draft["text_ref"]["sha256"]).decode("utf-8")
    expected = extract_assertions(body, candidate["text"],
        "rolling-facts:" + task["task_id"] + ":" + str(task["generation"]),
        declared_count=len(draft["declared_important_deltas"]))
    require(extraction["text_ref"] == draft["text_ref"] and extraction["assertions"] == expected,
            "INVALID_EVIDENCE", "Stored factual assertions differ from the actual native reader's candidate")
    return evidence, candidate


def _compatible_reader(kernel, repo, task, target_world_ref, *, extraction=None, draft=None):
    """Revalidate a hosted reader's real evidence; never invent a new model cut."""
    from .agent_gateway import AgentGateway
    from .candidate_delivery import CandidateDelivery
    from .semantic_dependencies import check_task_dependencies
    from .world_coverage_recovery import prove_review_compatibility

    check_task_dependencies(repo, task["task_id"], task["generation"])
    job = repo.one("supervisor_jobs", {"task_id": task["task_id"]})
    if job is None:
        # Public narrative services also accept manually supplied engineering
        # extractions. Their normal source/delivery checks still run below;
        # they are never presented as a model or MCP review.
        return {"task_id": task["task_id"], "generation": task["generation"],
                "semantics": "UNHOSTED_NARRATIVE_EXTRACTION_NO_MODEL_REVIEW"}, None
    require(task["state"] == "ACCEPTED" and job["config"].get("required_review_mcp"),
            "REVIEW_QUERY_REQUIRED", "Hosted production reading needs its original completed MCP evidence")
    if extraction is not None:
        evidence, candidate = validate_native_fact_source(kernel, repo, task, extraction, draft)
    else:
        evidence, candidate = CandidateDelivery(kernel, AgentGateway(kernel, {})).audited_candidate(
            repo, task, production_accounting=True)
    if (task["role_id"], task["task_kind"]) == ("reviewer", "epistemic"):
        require(not epistemic_blockers(candidate["issues"]), "EPISTEMIC_REVIEW_REQUIRED",
                "The original epistemic candidate still has mandatory findings")
    return prove_review_compatibility(kernel, repo, task, evidence, target_world_ref), evidence


def revalidate_fact_reading(kernel, repo, owner, report_ref, target_world_ref):
    """Adopt the same accepted text at a later, formally proved coverage cut.

    Original reader tasks, query receipts, text and checkpoints remain immutable.
    The caller promotes the returned report in a new checkpoint, in this same
    transaction. No model call, world event, literary decision or quota reset is
    performed here.
    """
    from .production_pipeline import _artifact
    from .release import _load_acceptance
    from .world_coverage_recovery import prove_pure_coverage

    work, branch = target_world_ref["work_id"], target_world_ref["branch_id"]
    kernel.authorize(repo, owner, work, "ARTIFACT_WRITE", branch)
    require(kernel.head(repo, work, branch) == target_world_ref,
            "BRANCH_CHANGED", "Reading revalidation must adopt the locked current head")
    source = kernel.artifact(repo, report_ref, work, kind="ROLLING_FACT_REPORT")["payload"]
    require(source.get("status") == "CLEAR" and source.get("acceptance_ref"),
            "NARRATIVE_BLOCKED", "Only an accepted clear factual reading can be revalidated")
    acceptance = kernel.artifact(repo, source["acceptance_ref"], work, kind="NARRATIVE_ACCEPTANCE")["payload"]
    _, checked = _load_acceptance(repo, kernel, owner, source["acceptance_ref"], acceptance["world_ref"])
    if acceptance["world_ref"] == target_world_ref:
        return report_ref, source
    key = "coverage-facts:" + fingerprint([report_ref, target_world_ref])
    request = {"source_report_ref": report_ref, "target_world_ref": target_world_ref}
    prior = kernel._prior(repo, owner, work, "FACT_READING_REVALIDATION", key, request)
    if prior:
        return prior, kernel.artifact(repo, prior, work, kind="ROLLING_FACT_REPORT")["payload"]
    coverage = prove_pure_coverage(kernel, repo, acceptance["world_ref"], target_world_ref)
    service = NarrativeService(kernel.in_transaction(repo))
    draft_ref, draft = acceptance["draft_ref"], checked["draft"]
    report = kernel.artifact(repo, acceptance["reconciliation_ref"], work, kind="RECONCILIATION_REPORT")["payload"]
    extraction_ref = report["extraction_ref"]
    require(source.get("extraction_ref", extraction_ref) == extraction_ref
            and source.get("reconciliation_ref", acceptance["reconciliation_ref"]) == acceptance["reconciliation_ref"]
            and source.get("alignment_ref", acceptance["alignment_ref"]) == acceptance["alignment_ref"],
            "INVALID_EVIDENCE", "Factual report differs from its formally accepted extraction")
    extraction, reader, _ = service._extraction(repo, extraction_ref, draft_ref, draft, allow_accepted=True)
    require(source.get("text_delivery_ref", extraction["delivery_ref"]) == extraction["delivery_ref"],
            "INVALID_EVIDENCE", "Factual report uses another text delivery")
    facts_proof, facts_evidence = _compatible_reader(kernel, repo, reader, target_world_ref,
                                                    extraction=extraction, draft=draft)
    knowledge_ref, knowledge_proof = source.get("knowledge_ref"), None
    if facts_evidence is not None:
        require(knowledge_ref is not None, "EPISTEMIC_REVIEW_REQUIRED",
                "Production coverage adoption needs the original independent epistemic reading")
    if knowledge_ref is not None:
        knowledge = kernel.artifact(repo, knowledge_ref, work, kind="EPISTEMIC_READING")["payload"]
        task = repo.one("tasks", {"task_id": knowledge["reader_task"]})
        origin = repo.one("tasks", {"task_id": draft["task_id"]})
        require(task and task["state"] == "ACCEPTED"
                and task["generation"] == knowledge["generation"]
                and (task["role_id"], task["task_kind"]) == ("reviewer", "epistemic")
                and knowledge["status"] == "CLEAR" and knowledge["draft_ref"] == draft_ref
                and knowledge["text_ref"] == draft["text_ref"]
                and task["principal_id"] not in {reader["principal_id"], origin["principal_id"]}
                and task["grant_id"] not in {reader["grant_id"], origin["grant_id"]}
                and (facts_evidence is None or knowledge["world_ref"] == facts_evidence["world_ref"]),
                "EPISTEMIC_REVIEW_REQUIRED", "Coverage adoption requires independent exact-text readers")
        delivery = kernel.artifact(repo, knowledge["delivery_ref"], work, kind="TEXT_DELIVERY")["payload"]
        require(delivery["task_id"] == task["task_id"] and delivery["generation"] == task["generation"]
                and delivery["world_ref"] == knowledge["world_ref"] and delivery["draft_ref"] == draft_ref
                and delivery["text_ref"] == draft["text_ref"] and delivery["read_mode"] == "FULL_TEXT",
                "EPISTEMIC_REVIEW_REQUIRED", "Epistemic revalidation needs its exact original full delivery")
        knowledge_proof, evidence = _compatible_reader(kernel, repo, task, target_world_ref)
        require(evidence and evidence["attempt_id"] == knowledge["attempt_id"]
                and evidence["output_ref"] == knowledge["candidate_ref"]
                and evidence["world_ref"] == knowledge["world_ref"],
                "REVIEW_QUERY_REQUIRED", "Epistemic source differs from its original MCP turn")
    compatibility_ref = _artifact(kernel, repo, work, owner, "ROLLING_READING_COMPATIBILITY", {
        **request, "draft_ref": draft_ref, "text_ref": draft["text_ref"],
        "source_acceptance_ref": source["acceptance_ref"], "coverage": coverage,
        "facts": facts_proof, "knowledge_ref": knowledge_ref, "knowledge": knowledge_proof,
        "semantics": "SERVER_REVALIDATION_WITH_ORIGINAL_MODEL_CUTS"})
    result = service.build_alignment(owner, draft_ref, extraction_ref, target_world_ref, key + ":align",
                                     resolution_event_refs=report["resolutions"])
    require(result["status"] == "CLEAR", "NARRATIVE_ALIGNMENT_REQUIRED",
            "The unchanged manuscript must pass current authoritative reconciliation")
    accepted = service.accept_narrative(owner, draft_ref, result["reconciliation_ref"], target_world_ref,
                                         key + ":accept", alignment_ref=result["alignment_ref"])
    renewed = {**source, **result, "extraction_ref": extraction_ref,
               "text_delivery_ref": extraction["delivery_ref"], "acceptance_ref": accepted,
               "source_report_ref": report_ref, "coverage_revalidation_ref": compatibility_ref,
               "adopted_world_ref": target_world_ref}
    ref = _artifact(kernel, repo, work, owner, "ROLLING_FACT_REPORT", renewed)
    kernel._receipt(repo, owner, target_world_ref, "FACT_READING_REVALIDATION", key, request, ref)
    return ref, renewed
