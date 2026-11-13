"""Map an independent full-text assertion reading into the native reconciliation service."""

from uuid import NAMESPACE_URL, uuid5

from ..domain.canonical import strict_json_loads
from ..domain.reconciliation import span_for
from .authorization import Principal
from .kernel import require
from .narrative import NarrativeService


def assignment():
    # Full schema is supplied outside the task assignment's bounded text field.
    return (
        "逐句独立审读获准正文，找出人物认知、事实变化、事件、披露、重要资源以及后果。"
        "在 AgentTextOutput.text 返回一个 JSON 对象，唯一键 assertions 为数组。"
        "每项包含 quote（正文中逐字且唯一的片段）、classification、summary、event_ref、world_check、"
        "knowledge_use、proposed_command、disclosure_id、declared_delta_index。无依据的引用填 null。"
        "分类仅限 EXISTING_EVENT_PRESENTATION、SURFACE_DETAIL、SPEECH_OR_BELIEF、NEW_WORLD_EFFECT、"
        "HISTORICAL_CONFLICT、READER_DISCLOSURE_CHANGE、UNCERTAIN。涵盖全文，可将连续表层描写分为一段。"
        "只标识确有文本依据的非持久感官细节为 SURFACE_DETAIL。缺乏事实证据时标为 UNCERTAIN。"
        "不要自行创造正式事件或更改世界。"
    )


def audit(kernel, repo, owner, board, node, task, candidate):
    source = board["nodes"][node["parents"][0]]
    draft_ref = source["output_ref"]
    actor = Principal(task["grant_id"], task["principal_id"])
    service = NarrativeService(kernel)
    key = "rolling-facts:" + task["task_id"] + ":" + str(task["generation"])
    delivered = service.read_text(actor, task["task_id"], task["generation"], draft_ref)
    parsed = strict_json_loads(candidate["text"])
    require(
        isinstance(parsed, dict)
        and set(parsed) == {"assertions"}
        and isinstance(parsed["assertions"], list)
        and 1 <= len(parsed["assertions"]) <= 512,
        "INVALID_EVIDENCE",
        "An assertion reading needs 1..512 entries",
    )
    body = delivered["text"]
    assertions = []
    coverage = []
    for index, raw in enumerate(parsed["assertions"]):
        require(
            isinstance(raw, dict)
            and set(raw)
            == {
                "classification",
                "summary",
                "quote",
                "event_ref",
                "world_check",
                "knowledge_use",
                "proposed_command",
                "disclosure_id",
                "declared_delta_index",
            },
            "INVALID_EVIDENCE",
            "Unexpected assertion fields",
        )
        quote = raw["quote"]
        require(
            isinstance(quote, str) and bool(quote.strip()) and body.count(quote) == 1,
            "INVALID_EVIDENCE",
            "Every quotation must identify exactly one passage",
        )
        begin = body.index(quote)
        end = begin + len(quote)
        coverage.append((begin, end))
        assertions.append(
            {
                **{k: v for k, v in raw.items() if k != "quote"},
                "assertion_id": str(uuid5(NAMESPACE_URL, key + ":" + str(index))),
                "span": span_for(body, begin, end),
            }
        )
    cursor = 0
    for begin, end in sorted(coverage):
        require(not body[cursor:begin].strip(), "INVALID_EVIDENCE", "Reading missed a passage")
        cursor = max(cursor, end)
    require(not body[cursor:].strip(), "INVALID_EVIDENCE", "Reading missed the ending")
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
