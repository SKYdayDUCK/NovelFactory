"""Map an independent full-text assertion reading into the native reconciliation service."""

from uuid import NAMESPACE_URL, uuid5
from functools import lru_cache
import json
import re

from ..contracts.registry import contract_diagnostic
from ..domain.errors import DomainError

from ..domain.canonical import strict_json_loads
from ..domain.reconciliation import span_for
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
        "SPEECH_OR_BELIEF 需要完整 knowledge_use，区分有来源的知识、推测与外部发言。"
        "若所需来源缺失，保留未决事实并说明缺少的证据，勿伪造。declared_delta_index 每项只可映射一次。"
        "对照材料的 required_event_refs 与 declared_important_deltas。已表现的事件引用对应 EventRef；"
        "写作方声明的重要变化用零基 declared_delta_index 对应。正文缺少所需事件或证据不足时如实标出。"
        "不要自行创造正式事件或更改世界。NEW_WORLD_EFFECT 必须有非空 proposed_command，按 TemplateCommand 中一个正式模板填写 template_id 和 args。不得自编模板名、参数键或实体编号。需要正式变化但授权材料不足以构造命令，应如实标记 UNCERTAIN 交给核查，不能降格为表面描写。"
        "\nAgentTextOutput.text 内层 JSON Schema：" + json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
    )


def epistemic_blockers(issues):
    """Retain the complete reading. Gate advancement on active mandatory findings."""
    return [issue for issue in issues if issue["disposition"] != "RESOLVED"
            and (issue["category"] == "HARD_CONSISTENCY" or issue["issue_kind"] == "HARD_CONFLICT"
                 or issue["severity"] == "BLOCKER" or
                 (issue["issue_kind"] == "CHANGE_REQUEST" and issue["disposition"] in {"OPEN", "ACCEPT"}))]


def record_knowledge(kernel, repo, owner, board, node, task, evidence, candidate):
    from .production_pipeline import _artifact

    parent = board["nodes"][node["parents"][0]]
    draft_ref = parent["output_ref"]
    reader = Principal(task["grant_id"], task["principal_id"])
    delivered = NarrativeService(kernel).read_text(
        reader, task["task_id"], task["generation"], draft_ref
    )
    clear = not epistemic_blockers(candidate["issues"])
    receipt = {
        "world_ref": board["world_ref"],
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


def audit(kernel, repo, owner, board, node, task, candidate):
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
    if result["status"] == "CLEAR":
        evidence["acceptance_ref"] = service.accept_narrative(
            owner,
            draft_ref,
            result["reconciliation_ref"],
            wr,
            key + ":accept",
            alignment_ref=result["alignment_ref"],
        )
    return evidence
