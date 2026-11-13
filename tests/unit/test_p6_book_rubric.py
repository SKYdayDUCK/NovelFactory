import pytest

from novel_factory.domain.book_review import (
    RUBRIC,
    VETOES,
    assess,
    check_notes,
    check_score,
    manuscript_hash,
    reading_packets,
)
from novel_factory.domain.errors import DomainError


def scorecard(parts, total):
    remaining, scores = total, {}
    for name, maximum in RUBRIC.items():
        points = min(maximum, remaining)
        remaining -= points
        scores[name] = dict(
            score=points,
            scene=1,
            start=0,
            quote=parts[0][:2],
            reason="用于验证评分规则的判断",
            improvement="指出需要重写的具体句子",
        )
    return {"scores": scores, "vetoes": []}


def test_order_unicode_offsets_coverage_and_missing_packet():
    parts = ["甲🌿乙。", "门响了。", "雪" * 6001]
    packets = reading_packets(parts, 5)
    assert "".join(p["text"] for packet in packets for p in packet) == "".join(parts)
    assert manuscript_hash(parts) != manuscript_hash(parts[::-1])
    for packet in packets:
        notes = [
            dict(scene=p["scene"], start=p["start"], quote=p["text"], observation="本段原文")
            for p in packet
        ]
        check_notes(notes, packet)
        with pytest.raises(DomainError):
            check_notes(notes[:-1], packet)
    with pytest.raises(DomainError):
        check_notes([dict(scene=1, start=2, quote="🌿乙", observation="偏移错误")], [packets[0][0]])


@pytest.mark.parametrize(
    ("low", "high", "expected", "mean"),
    [
        (84, 85, "P6_QUALITY_NOT_MET", "84.5"),
        (81, 89, "QUALITY_PASS_TARGET_NOT_MET", "85.0"),
        (94, 95, "QUALITY_PASS_TARGET_NOT_MET", "94.5"),
        (90, 100, "QUALITY_TARGET_MET", "95.0"),
    ],
)
def test_frozen_arithmetic(low, high, expected, mean):
    parts = ["量表回归样例"]
    result = assess([scorecard(parts, low), scorecard(parts, high)], parts)
    assert result == dict(scores=[low, high], average=mean, difference=high - low, status=expected)


def test_veto_and_unsupported_deductions():
    parts = ["门响。"]
    first = scorecard(parts, 100)
    second = scorecard(parts, 86)
    first["vetoes"] = [dict(kind=VETOES[0], scene=1, start=0, quote="门响", reason="回归反例")]
    assert assess([first, second], parts)["status"] == "P6_QUALITY_NOT_MET"
    for key, value in [("quote", "门开"), ("score", True), ("reason", "")]:
        item = first["scores"]["故事结构"].copy()
        first["scores"]["故事结构"][key] = value
        with pytest.raises(DomainError):
            check_score(first, parts)
        first["scores"]["故事结构"] = item
    second["scores"]["整体阅读体验"].pop("improvement")
    with pytest.raises(DomainError):
        check_score(second, parts)
    with pytest.raises(DomainError):
        assess([first, first, first], parts)


@pytest.mark.parametrize(("scores", "status"), [
    ((89, 90), "P6_QUALITY_NOT_MET"),
    ((89, 91), "QUALITY_PASS_TARGET_NOT_MET"),
    ((94, 96), "QUALITY_TARGET_MET"),
])
def test_live_quality_floor(scores, status):
    parts = ["量表回归样例"]
    reports = [scorecard(parts, value) for value in scores]
    assert assess(reports, parts, minimum_average=90)["status"] == status
