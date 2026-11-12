"""Literary observations are scoped to real codepoint ranges and remain defeasible."""
from hashlib import sha256
from uuid import uuid4

import pytest

from novel_factory.contracts.registry import validate_contract
from novel_factory.domain.creative_review import evaluate, native_dimensions
from novel_factory.domain.errors import DomainError
from novel_factory.domain.reconciliation import span_for


pytestmark = pytest.mark.unit


def document(text):
    return {"text_ref": {"artifact_id": str(uuid4()), "artifact_revision_id": str(uuid4()),
                         "sha256": sha256(text.encode()).hexdigest()}, "text": text}


def passage(doc, words):
    start = doc["text"].index(words)
    return {"text_ref": doc["text_ref"], "start": start, "end": start + len(words)}


def observation(doc, words, pattern, *other, **extra):
    cite = passage(doc, words)
    return {"start": cite["start"], "end": cite["end"], "pattern": pattern,
            "reason": "The later passage changes the reading established by the earlier one.",
            "counterinterpretation": "The change may be intentional and become legible at the next turn.",
            "narrative_function": "Consider whether this detail changes what the character can do.",
            "requested_change": "Clarify the single change of perspective in the cited passage.",
            "related_spans": list(other), **extra}


@pytest.mark.parametrize(("category", "pattern", "first", "later", "field", "roles"), [
    ("WORLD_SETTING", "UNUSED_EXPOSITION", "水还没漫过台阶。", "她接着背了三代祭司的名册。", "OTHER", ["world_designer"]),
    ("NAMING", "REPEATED_NAME_FORM", "归潮司", "归潮部", "STYLE", ["world_designer"]),
    ("PLOT", "MOTIVATION_GAP", "他藏起钥匙，不许人靠近。", "他把门敞开，让访客进来。", "MOTIVATION", ["volume_director"]),
    ("STAGING", "UNBRIDGED_MOVEMENT", "他还在拴船。", "他从塔顶往下看。", "CONTINUITY", ["environment_director"]),
    ("CHARACTER", "SHARED_VOICE", "阿箬说：“雨怎么还没停呢？”", "秦生道：“雨怎么还没停呢？”", "VOICE", ["character_actor"]),
    ("PROSE", "REPEATED_IMAGE", "她的名字像石子落进碗里。", "故乡又像石子落进碗里。", "STYLE", ["writer"]),
    ("CHAPTER_SEAM", "UNBRIDGED_SEAM", "灯还在渡口摇着。", "天亮后，他攀上雪峰。", "CONTINUITY", ["volume_director"]),
    ("VOLUME", "DROPPED_PROMISE", "她说年关前会来赎船。", "渡口被拆成一片浅滩。", "CAUSALITY", ["chief_editor"]),
    ("FACT_KNOWLEDGE", "POV_LEAK", "她一直在渡口。", "她知道密室里放着钥匙。", "KNOWLEDGE", ["reviewer"]),
])
def test_nine_distinct_scales_preserve_readers_evidence(category, pattern, first, later, field, roles):
    if category in {"CHAPTER_SEAM", "VOLUME"}:
        earlier, target = document(first), document(later)
        related = [earlier, target]
    else:
        target = earlier = document("🌙" + first + "\n" + later)
        related = []
    options = {"speakers": ["秦生", "阿箬"]} if category == "CHARACTER" else {}
    obs = observation(target, later, pattern, passage(earlier, first), **options)
    item, = evaluate(category, **target, observations=[obs, obs], related_texts=related)
    finding = validate_contract("Finding", item["finding"], "harness")
    assert finding["dimension"] == field and set(roles) <= set(item["upstream_roles"])
    assert finding["target_ref"] == target["text_ref"] and finding["judgment"] == "UNCERTAIN"
    assert finding["text_span"] == span_for(target["text"], obs["start"], obs["end"])
    assert item["quote"] == later and item["counterinterpretation"] and item["narrative_function"]


def test_legitimate_words_attributed_belief_and_deliberate_motif():
    text = document("🌧️掌柜把账簿推来。她写了名字，盖上印章。她以为父亲已经死了。")
    assert not evaluate("WORLD_SETTING", **text)
    assert not evaluate("NAMING", **text)
    assert not evaluate("FACT_KNOWLEDGE", **text)
    obs = observation(text, "她以为父亲已经死了。", "WORLD_CONTRADICTION", disposition="PRESERVE")
    assert not evaluate("FACT_KNOWLEDGE", **text, observations=[obs])
    echoed = document("我们会照样伸手接住院里落下来的雨。" * 3)
    item, = evaluate("PROSE", **echoed)
    assert item["finding"]["judgment"] == "UNCERTAIN" and item["finding"]["severity"] == "NOTE"
    positions = item["finding"]["evidence_refs"]
    assert len({e["span"]["start"] for e in positions}) == 3
    assert not evaluate("PROSE", **echoed, observations=[
        {**observation(echoed, "我们会", "MECHANICAL_CADENCE"), "disposition": "PRESERVE",
         "narrative_function": "Each voice joins a communal refrain.", "requested_change": None}])


def test_wire_record_and_similar_words_are_distinct():
    ref = str(uuid4())
    text = document('柜上的账簿记着三条船。{"task_id":"' + ref + '","generation":2}她蹲下来收绳。')
    item, = evaluate("WORLD_SETTING", **text)
    assert item["quote"].startswith('{"task_id"') and item["finding"]["judgment"] == "UNCERTAIN"
    assert not evaluate("WORLD_SETTING", **document("这家船厂先做任务，再由儿子接班。这一代人只认老船。"))


def test_names_and_speakers_can_be_screened_without_a_forbidden_word_list():
    names = document("临海一署名为“潮城司”，另一署名为“潮城厅”，第三署称作“潮城台”。")
    name, = evaluate("NAMING", **names)
    assert name["quote"] == "潮城台" and len(name["related_evidence"]) == 2
    speakers = document("阿箬说：“船上总该留一盏照人的灯。”\n秦生道：“船上总该留一盏照人的灯。”")
    voice, = evaluate("CHARACTER", **speakers)
    assert voice["finding"]["dimension"] == "VOICE" and "秦生" in voice["quote"]


def test_fact_hardness_requires_a_verified_evidence_binding_on_the_exact_plane():
    target = document("她认得船底藏着的铜匣。")
    packet = observation(target, target["text"], "POV_LEAK")
    claimed = {**packet, "severity": "BLOCKER"}
    with pytest.raises(DomainError):
        evaluate("FACT_KNOWLEDGE", **target, observations=[claimed])
    evidence = {"kind": "ARTIFACT", "artifact_ref": document("Independent NF check")["text_ref"]}
    verified = {"plane": "WORLD", "text_ref": target["text_ref"], "text_span": span_for(target["text"]),
                "evidence_refs": [evidence]}
    still_uncertain, = evaluate("FACT_KNOWLEDGE", **target, observations=[packet], verified_evidence=[verified])
    assert still_uncertain["finding"]["judgment"] == "UNCERTAIN"
    confirmed, = evaluate("FACT_KNOWLEDGE", **target, observations=[claimed],
                           verified_evidence=[{**verified, "plane": "BELIEF"}])
    assert confirmed["finding"]["judgment"] == "CONTRADICTION" and evidence in confirmed["finding"]["evidence_refs"]


def test_seam_requires_adjacent_boundaries_and_exact_version():
    a, unrelated, b = document("第一场景。\n她在雨里等。"), document("另一个场景。"), document("台阶忽然结冰。\n尾声。")
    obs = observation(b, "台阶忽然结冰。", "UNBRIDGED_SEAM", passage(a, "她在雨里等。"))
    with pytest.raises(DomainError):
        evaluate("CHAPTER_SEAM", **b, observations=[obs], related_texts=[a, unrelated, b])
    with pytest.raises(DomainError):
        evaluate("CHAPTER_SEAM", b["text_ref"], "篡改的正文。", [obs], related_texts=[a, b])
    with pytest.raises(DomainError):
        evaluate("CHAPTER_SEAM", **b, observations=[obs])
    assert native_dimensions("CHAPTER_SEAM") == ("CONTINUITY",)


def test_one_passage_cannot_establish_a_comparison_or_world_contradiction():
    target = document("我会在这里等你。")
    for category, pattern in (("NAMING", "REPEATED_NAME_FORM"), ("PLOT", "MOTIVATION_GAP"),
                              ("STAGING", "UNBRIDGED_MOVEMENT"), ("PROSE", "REPEATED_IMAGE"),
                              ("VOLUME", "REPEATED_BEAT")):
        with pytest.raises(DomainError):
            evaluate(category, **target, observations=[observation(target, target["text"], pattern)])
    with pytest.raises(DomainError):
        evaluate("PLOT", **target, observations=[observation(target, target["text"], "MISSING_CONSEQUENCE",
                                                             passage(target, target["text"]))])
    opinion, = evaluate("FACT_KNOWLEDGE", **target, observations=[observation(target, target["text"], "POV_LEAK",
                                                                             disposition="PREFERENCE")])
    assert opinion["finding"]["judgment"] == "PREFERENCE"


def test_automatic_seam_and_volume_compare_exact_boundaries_in_order():
    line = "那艘小船总在落潮前靠上最后一级石阶。"
    chapter_a = document("她在渡口等了一个月。\r\n" + line + "\r\n")
    chapter_b = document(line + "\n街上有人点灯。\n" + line)
    order = [chapter_a, chapter_b]
    seam, = evaluate("CHAPTER_SEAM", **chapter_b, related_texts=order)
    structure, = evaluate("VOLUME", **chapter_b, related_texts=order)
    assert seam["pattern"] == "REPEATED_SEAM" and seam["finding"]["text_span"]["start"] == 0
    assert structure["pattern"] == "REPEATED_BEAT" and structure["finding"]["text_span"]["start"] > 0
    altered = document("街上有人点灯。\n他一直等到落潮。")
    assert not evaluate("CHAPTER_SEAM", **altered, related_texts=[chapter_a, altered])
