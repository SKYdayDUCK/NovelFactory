"""Full-manuscript blind reading, Unicode evidence locations and frozen P6 arithmetic."""

from hashlib import sha256

from novel_factory.domain.canonical import fingerprint
from novel_factory.domain.errors import DomainError

# Public scoring criteria. Pass/target policy is intentionally absent from reader packets.
RUBRIC = {
    "故事结构": 9,
    "情节因果": 10,
    "人物塑造": 9,
    "角色动机": 8,
    "叙述声音": 8,
    "语言表达": 8,
    "章节节奏": 7,
    "情绪与阅读吸引力": 7,
    "伏笔及兑现": 7,
    "类型文学适配": 6,
    "世界规则一致性": 6,
    "人物认知与信息披露": 5,
    "AI风格与重复模板": 5,
    "整体阅读体验": 5,
}
VETOES = (
    "作品不完整",
    "主线因果失效",
    "核心人物选择无依据",
    "大面积重复填充",
    "主要人物声线无法区分",
    "关键设定矛盾破坏主线",
)
PROTOCOL = {
    "version": "P6-rubric-v1",
    "weights": RUBRIC,
    "vetoes": list(VETOES),
    "instructions": "按整数给分，每项引原文、注明位置、解释判断。扣分项给出修改方向。否决项须单独举证。",
}
PROTOCOL_HASH = fingerprint(PROTOCOL)


def demand(test, reason):
    if not test:
        raise DomainError("INVALID_REVIEW_EVIDENCE", reason)


def digest(text):
    return sha256(text.encode("utf-8")).hexdigest()


def manuscript_hash(parts):
    return digest("\n\n".join(parts))


def reading_packets(parts, limit=6000):
    """Place every original code point in exactly one passage, including long single scenes."""
    demand(bool(parts) and type(limit) is int and limit > 0, "An ordered manuscript is required")
    packets, packet, remaining = [], [], limit
    for scene, prose in enumerate(parts, 1):
        demand(isinstance(prose, str) and prose.strip(), "Empty manuscript scene")
        offset = 0
        while offset < len(prose):
            end = min(len(prose), offset + remaining)
            packet.append({"scene": scene, "start": offset, "end": end, "text": prose[offset:end]})
            remaining -= end - offset
            offset = end
            if not remaining:
                packets.append(packet)
                packet, remaining = [], limit
    if packet:
        packets.append(packet)
    return packets


def citation(note, parts):
    demand(isinstance(note, dict), "Missing evidence")
    scene, start, quote = (note.get(k) for k in ("scene", "start", "quote"))
    demand(
        type(scene) is int
        and 1 <= scene <= len(parts)
        and type(start) is int
        and start >= 0
        and isinstance(quote, str)
        and quote.strip()
        and parts[scene - 1][start : start + len(quote)] == quote,
        "Quotation differs from the manuscript at its Unicode offset",
    )
    return scene, start, start + len(quote)


def check_notes(notes, packet):
    demand(isinstance(notes, list) and len(notes) == len(packet), "Missing passage observation")
    for note, passage in zip(notes, packet, strict=True):
        demand(isinstance(note, dict), "Missing observation")
        scene, start, quote = (note.get(k) for k in ("scene", "start", "quote"))
        demand(
            type(scene) is int
            and scene == passage["scene"]
            and type(start) is int
            and isinstance(quote, str)
            and quote.strip()
            and passage["start"] <= start < start + len(quote) <= passage["end"]
            and passage["text"][start - passage["start"] : start - passage["start"] + len(quote)]
            == quote
            and isinstance(note.get("observation"), str)
            and note["observation"].strip(),
            "Each delivered passage needs a located observation",
        )


def check_score(report, parts):
    demand(
        isinstance(report, dict) and set(report) == {"scores", "vetoes"},
        "Unexpected scorecard format",
    )
    scores = report["scores"]
    demand(isinstance(scores, dict) and set(scores) == set(RUBRIC), "Incomplete rubric")
    total = 0
    for name, maximum in RUBRIC.items():
        item = scores[name]
        citation(item, parts)
        score = item.get("score")
        demand(
            type(score) is int
            and 0 <= score <= maximum
            and isinstance(item.get("reason"), str)
            and item["reason"].strip(),
            "Invalid criterion score or missing rationale",
        )
        if score < maximum:
            demand(
                isinstance(item.get("improvement"), str) and item["improvement"].strip(),
                "Missing revision advice",
            )
        total += score
    vetoes = report["vetoes"]
    demand(isinstance(vetoes, list), "Missing veto decision")
    for veto in vetoes:
        citation(veto, parts)
        demand(
            veto.get("kind") in VETOES
            and isinstance(veto.get("reason"), str)
            and veto["reason"].strip(),
            "Unsupported or unexplained veto",
        )
    return total


def assess(reports, parts):
    demand(len(reports) == 2, "Exactly two assigned readers must submit")
    scores = [check_score(review, parts) for review in reports]
    total = sum(scores)
    status = (
        "P6_QUALITY_NOT_MET"
        if total < 170 or any(r["vetoes"] for r in reports)
        else "QUALITY_PASS_TARGET_NOT_MET"
        if total < 190
        else "QUALITY_TARGET_MET"
    )
    return {
        "scores": scores,
        "average": str(total // 2) + (".5" if total % 2 else ".0"),
        "difference": abs(scores[0] - scores[1]),
        "status": status,
    }
