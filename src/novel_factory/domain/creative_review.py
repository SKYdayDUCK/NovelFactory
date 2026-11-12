"""Evidence-anchored literary rubrics for independent readers and controlled reviewer tasks.

Every source is an exact, already authorized reading. Subjective and ambiguous
judgments remain advisory. Only a service-verified contradiction can use a
hard factual classification. No rubric obtains text or world facts itself.
"""
from __future__ import annotations

from collections import defaultdict
from difflib import SequenceMatcher
from hashlib import sha256
import json
import re

from novel_factory.contracts.registry import validate_contract
from novel_factory.domain.canonical import fingerprint
from novel_factory.domain.errors import DomainError
from novel_factory.domain.reconciliation import span_for


NATIVE_MODES = {
    "WORLD_SETTING": ("LITERARY",), "NAMING": ("LITERARY",), "PLOT": ("LITERARY",),
    "STAGING": ("LITERARY",), "CHARACTER": ("LITERARY",), "PROSE": ("LITERARY",),
    "CHAPTER_SEAM": ("CONTINUITY",), "VOLUME": ("LITERARY",),
    "FACT_KNOWLEDGE": ("FACT", "EPISTEMIC"),
}

# Distinct routes identify an upstream hypothesis to investigate. They never
# attribute blame or change an instruction merely because a finding was filed.
RUBRICS = {
    "WORLD_SETTING": {"UNUSED_EXPOSITION": ("OTHER", ("world_designer", "volume_director")),
                      "ENGINEERING_SERIALIZATION": ("STYLE", ("world_designer", "writer"))},
    "NAMING": {"REPEATED_NAME_FORM": ("STYLE", ("world_designer",))},
    "PLOT": {"MOTIVATION_GAP": ("MOTIVATION", ("volume_director", "character_actor")),
             "MISSING_CONSEQUENCE": ("CAUSALITY", ("volume_director",))},
    "STAGING": {"UNBRIDGED_MOVEMENT": ("CONTINUITY", ("environment_director", "volume_director"))},
    "CHARACTER": {"SHARED_VOICE": ("VOICE", ("character_actor", "writer")),
                  "EMOTION_AS_RULE": ("MOTIVATION", ("character_actor", "writer"))},
    "PROSE": {"REPEATED_IMAGE": ("STYLE", ("writer", "literary_editor")),
              "MECHANICAL_CADENCE": ("PACING", ("writer", "literary_editor")),
              "EXCESS_SUMMARY": ("STYLE", ("writer", "volume_director"))},
    "CHAPTER_SEAM": {"UNBRIDGED_SEAM": ("CONTINUITY", ("volume_director", "literary_editor")),
                     "REPEATED_SEAM": ("CONTINUITY", ("volume_director", "literary_editor"))},
    "VOLUME": {"DROPPED_PROMISE": ("CAUSALITY", ("volume_director", "chief_editor")),
               "REPEATED_BEAT": ("PACING", ("volume_director", "literary_editor"))},
    "FACT_KNOWLEDGE": {"WORLD_CONTRADICTION": ("FACT", ("world_designer", "reviewer")),
                       "POV_LEAK": ("KNOWLEDGE", ("character_actor", "reviewer"))},
}


def _need(condition, message, code="INVALID_EVIDENCE"):
    if not condition:
        raise DomainError(code, message)


def native_dimensions(category):
    _need(isinstance(category, str) and category in NATIVE_MODES, "Unknown review category", "INVALID_ARGUMENT")
    return NATIVE_MODES[category]


def _digest(text):
    return sha256(text.encode("utf-8")).hexdigest()


def _source(text_ref, text):
    validate_contract("ArtifactRef", text_ref)
    _need(isinstance(text, str) and _digest(text) == text_ref["sha256"],
          "Reading does not match its exact text revision")
    return text_ref["artifact_revision_id"]


def _citation(ref, text, start, end):
    return {"kind": "ARTIFACT", "artifact_ref": ref, "span": span_for(text, start, end)}


def _common_form(left, right):
    normalized = [re.sub(r"[^\w]", "", t.casefold())[:256] for t in (left, right)]
    if not all(normalized) or normalized[0] == normalized[1]:
        return False
    left, right = normalized
    match = SequenceMatcher(None, left, right, autojunk=False).find_longest_match()
    return match.size >= 2 and match.size * 3 >= min(len(left), len(right))


# Recognize a serialized object with multiple reserved fields and a typed
# identity. The same vocabulary or punctuation in ordinary prose is harmless.
_OBJECT = re.compile(r'\{\s*"(?:schema_version|task_id|generation|world_ref|branch_id|work_id|context_bundle_id)"\s*:')
_UUID = re.compile(r"^[a-fA-F0-9]{8}-(?:[a-fA-F0-9]{4}-){3}[a-fA-F0-9]{12}$")
_DECODER = json.JSONDecoder()


def _serialized(text):
    consumed = 0
    for match in _OBJECT.finditer(text):
        if match.start() < consumed:
            continue
        try:
            doc, length = _DECODER.raw_decode(text[match.start():match.start() + 8192])
        except (ValueError, RecursionError):
            continue
        consumed = match.start() + length
        if isinstance(doc, dict):
            identities = ("task_id", "context_bundle_id", "work_id", "branch_id", "world_revision_id")
            has_id = any(isinstance(doc.get(k), str) and _UUID.fullmatch(doc[k]) for k in identities)
            controls = set(doc) & {*identities, "world_ref", "generation", "schema_version"}
            if has_id and len(controls) >= 2:
                yield match.start(), consumed


_SENTENCE = re.compile(r'[^。！？!?.\r\n]+[。！？!?.]+')
_NAMED = re.compile(r'(?:名为|人称|称作|号为|取名为)[“「《]([^”」》\r\n]{2,32})[”」》]')
_SPEECH = re.compile(r'(?:^|(?<=[。！？\n”]))\s*([\u3400-\u9fff]{1,8})(?:说|道|答)[：:]?'
                     r'[“「]([^”」\r\n]{8,280})[”」]')


def _edge_line(text, *, first):
    if first:
        start = len(text) - len(text.lstrip())
        breaks = [i for i in (text.find("\r", start), text.find("\n", start)) if i >= 0]
        end = min(breaks, default=len(text))
    else:
        end = len(text.rstrip())
        start = max(text.rfind("\r", 0, end), text.rfind("\n", 0, end)) + 1
    return start, end, text[start:end]


def _suggestions(category, text_ref, text, sources, verified_evidence):
    if category == "WORLD_SETTING":
        for start, end in _serialized(text):
            yield {"pattern": "ENGINEERING_SERIALIZATION", "start": start, "end": end, "related_spans": [],
                   "reason": "The passage includes an exact serialized control record with a system identity.",
                   "counterinterpretation": "A character might be looking at a terminal or reading the record as evidence.",
                   "narrative_function": "Check who perceives the record and what decision depends on its contents.",
                   "requested_change": "If nobody reads the record, give only the information that changes the scene."}
    if category == "NAMING":
        names = []
        for m in _NAMED.finditer(text):
            if len(names) >= 64:
                break
            name = m.group(1)
            matches = [span for old, span in names if _common_form(name, old)]
            if len(matches) >= 2:
                yield {"pattern": "REPEATED_NAME_FORM", "start": m.start(1), "end": m.end(1),
                       "related_spans": matches[:2], "reason": "Three explicitly introduced names use a similar form.",
                       "counterinterpretation": "A family, rank system, or shared cultural history may explain the resemblance.",
                       "narrative_function": "Can a reader distinguish these entities and see why their names belong together?",
                       "requested_change": "If the resemblance serves no purpose, vary only this name."}
            if all(name != old for old, _ in names):
                names.append((name, {"text_ref": text_ref, "start": m.start(1), "end": m.end(1)}))
    if category == "CHARACTER":
        voices = {}
        for m in _SPEECH.finditer(text):
            name, line = m.group(1), m.group(2)
            if line in voices and voices[line][0] != name:
                first, earlier = voices[line]
                yield {"pattern": "SHARED_VOICE", "start": m.start(1), "end": m.end(),
                       "speakers": [name, first], "related_spans": [earlier],
                       "reason": "Distinct attributed speakers deliver the same complete line.",
                       "counterinterpretation": "The second speaker may be quoting, mocking or answering the first.",
                       "narrative_function": "Check whether the repetition expresses an interaction between these speakers.",
                       "requested_change": "If the voices accidentally coincide, adjust only the reply's register."}
            elif len(voices) < 2048:
                voices[line] = name, {"text_ref": text_ref, "start": m.start(1), "end": m.end()}
    if category in {"CHAPTER_SEAM", "VOLUME"}:
        order = list(sources)
        index = order.index(text_ref["artifact_revision_id"])
        if index:
            ref, before = sources[order[index - 1 if category == "CHAPTER_SEAM" else 0]]
            prev_start, prev_end, close = _edge_line(before, first=False)
            start, end, opening = _edge_line(text, first=category == "CHAPTER_SEAM")
            if 12 <= len(close) <= 1024 and close == opening:
                yield {"pattern": "REPEATED_SEAM" if category == "CHAPTER_SEAM" else "REPEATED_BEAT",
                       "start": start, "end": end,
                       "related_spans": [{"text_ref": ref, "start": prev_start, "end": prev_end}],
                       "reason": "The two grounded narrative boundaries repeat the same complete paragraph.",
                       "counterinterpretation": "The repetition may intentionally link chapters or frame the volume.",
                       "narrative_function": "Does the later occurrence change the reader's understanding of the earlier one?",
                       "requested_change": "If the second occurrence simply restates the first, shorten that occurrence."}
    if category == "FACT_KNOWLEDGE":
        for check in verified_evidence:
            if check["text_ref"] == text_ref and check["evidence_refs"] and check["plane"] in {"WORLD", "BELIEF"}:
                span = check["text_span"]
                yield {"pattern": "POV_LEAK" if check["plane"] == "BELIEF" else "WORLD_CONTRADICTION",
                       "start": span["start"], "end": span["end"], "related_spans": [],
                       "reason": "This exact assertion conflicts with its independently verified, fixed-cut evidence.",
                       "counterinterpretation": "Quotation, mistaken belief or a different story time could change its interpretation.",
                       "narrative_function": "Check how the narrator attributes the assertion at this point in the scene.",
                       "requested_change": "Correct the assertion or clarify its attribution after checking the cited record."}
    if category == "PROSE":
        seen = defaultdict(list)
        for m in _SENTENCE.finditer(text):
            phrase = m.group().strip()
            if not (12 <= len(phrase) <= 512):
                continue
            start = m.start() + len(m.group()) - len(m.group().lstrip())
            key = phrase.casefold()
            earlier = seen[key]
            if len(earlier) < 2:
                earlier.append({"text_ref": text_ref, "start": start, "end": m.end()})
            elif len(earlier) == 2:
                yield {"pattern": "MECHANICAL_CADENCE", "start": start, "end": m.end(),
                       "related_spans": list(earlier), "reason": "The same full sentence occurs in three distinct places.",
                       "counterinterpretation": "A refrain, ritual, or recurring character voice may need the exact repetition.",
                       "narrative_function": "Check whether this return changes the refrain's meaning in context.",
                       "requested_change": "Consider varying this occurrence only if it adds no new emphasis."}
                earlier.append(None)


def _span_doc(source, reading):
    ref = reading["artifact_ref"]
    return source[ref["artifact_revision_id"]][1]


def _excerpt(source, reading):
    span = reading["span"]
    return _span_doc(source, reading)[span["start"]:span["end"]]


def _at_edge(text, span, side):
    content_start = len(text) - len(text.lstrip())
    content_end = len(text.rstrip())
    if side == "start":
        line = text.find("\n", content_start)
        return span["start"] < min(content_start + 600, line if line >= 0 else content_end)
    line = text.rfind("\n", content_start, content_end)
    return span["end"] > max(content_end - 600, line + 1)


def _seam(source, primary, other):
    a, b = (v["artifact_ref"]["artifact_revision_id"] for v in (primary, other))
    order = list(source)
    if abs(order.index(a) - order.index(b)) != 1:
        return False
    previous, following = (primary, other) if order.index(a) < order.index(b) else (other, primary)
    return (_at_edge(_span_doc(source, previous), previous["span"], "end")
            and _at_edge(_span_doc(source, following), following["span"], "start"))


def _distinct_spans(evidence):
    intervals = defaultdict(list)
    for e in evidence:
        span = e["span"]
        intervals[e["artifact_ref"]["artifact_revision_id"]].append((span["start"], span["end"]))
    for locations in intervals.values():
        ordered = sorted(locations)
        _need(all(left[1] <= right[0] for left, right in zip(ordered, ordered[1:])),
              "Comparative citations must identify distinct passages")


def _support(category, obs, source, primary, related):
    """Use the geometry, subjects and scale required by this particular claim."""
    pattern = obs["pattern"]
    text = _excerpt(source, primary)
    others = [_excerpt(source, e) for e in related]
    if pattern == "ENGINEERING_SERIALIZATION":
        _need(any(_serialized(text)), "Control-record claim needs a typed serialized object")
    elif category == "NAMING":
        _need(any(_common_form(text, other) for other in others),
              "Cite distinct names which share a visible form")
    elif pattern == "SHARED_VOICE":
        speakers = obs.get("speakers")
        _need(isinstance(speakers, list) and len(speakers) == 2 and all(isinstance(s, str) and s.strip() for s in speakers)
              and speakers[0] != speakers[1] and related, "Voice comparison needs two distinct attributed speakers")
        passages = [primary, *related]
        # The name may immediately introduce a quote; inspect its local
        # attribution instead of requiring the name to appear in the dialogue.
        labels = []
        for e in passages:
            s = e["span"]
            labels.append(_span_doc(source, e)[max(s["start"] - 32, 0):s["end"] + 16])
        _need(any(speakers[0] in a and speakers[1] in b for i, a in enumerate(labels)
                  for j, b in enumerate(labels) if i != j), "The cited passages do not locate both speakers")
    elif category == "CHAPTER_SEAM":
        _need(any(_seam(source, primary, e) for e in related),
              "A seam needs the closing and opening passages of adjacent assigned texts")
    elif category == "VOLUME":
        _need(any(e["artifact_ref"] != primary["artifact_ref"] for e in related),
              "A volume claim must compare passages from separate assigned sections")
    elif pattern == "MECHANICAL_CADENCE":
        _need(len(related) >= 2, "A cadence pattern needs three distinct passages")
    elif category == "STAGING":
        _need(any(e["artifact_ref"] == primary["artifact_ref"] for e in related),
              "Stage continuity needs two positions within the same scene")
    elif category == "PLOT":
        _need(related, "A causal or motivation claim needs its setup and its consequence")
        order = {ref: i for i, ref in enumerate(source)}
        def location(e):
            return order[e["artifact_ref"]["artifact_revision_id"]], e["span"]["start"]
        _need(any(location(e) < location(primary) for e in related),
              "A proposed narrative consequence needs an earlier cited setup")
    elif category != "FACT_KNOWLEDGE":
        _need(related, "This reading needs another passage to establish narrative context")


def evaluate(category, text_ref, text, observations=(), *, related_texts=(), verified_evidence=()):
    """Return S0 findings paired with a reader's counter-reading and attribution.

    Pass related texts in the order of the authorized volume manifest, including
    the primary if its ordinal matters. If absent, the primary precedes related
    texts. Verified contradictions are supplied by the calling service only.
    """
    native_dimensions(category)
    target = _source(text_ref, text)
    _need(isinstance(related_texts, (list, tuple)) and isinstance(observations, (list, tuple))
          and len(observations) <= 64, "Invalid review inputs", "INVALID_ARGUMENT")
    sources = {}
    for item in related_texts:
        _need(isinstance(item, dict) and {"text_ref", "text"} <= item.keys(),
              "Related text requires a version and content", "INVALID_ARGUMENT")
        key = _source(item["text_ref"], item["text"])
        _need(key not in sources, "A reading manifest contains duplicate text revisions")
        sources[key] = item["text_ref"], item["text"]
    if target in sources:
        _need(sources[target] == (text_ref, text), "Target differs from manifest text")
    else:
        sources = {target: (text_ref, text), **sources}
    for check in verified_evidence:
        if check["text_ref"] == text_ref:
            span = check["text_span"]
            _need(span_for(text, span["start"], span["end"]) == span,
                  "Verified assertion does not cover these exact text bytes")
            for ref in check["evidence_refs"]:
                validate_contract("EvidenceRef", ref)
    result, seen = [], set()
    candidates = observations or _suggestions(category, text_ref, text, sources, verified_evidence)
    for obs in candidates:
        if len(result) == 64:
            break
        required = {"pattern", "start", "end", "reason", "requested_change", "counterinterpretation",
                    "narrative_function", "related_spans"}
        _need(isinstance(obs, dict) and required <= obs.keys() <= required | {"severity", "disposition", "speakers"},
              "Invalid literary observation", "INVALID_ARGUMENT")
        pattern, directive = obs["pattern"], obs.get("disposition", "QUESTION")
        _need(isinstance(pattern, str) and pattern in RUBRICS[category]
              and directive in ("QUESTION", "PREFERENCE", "PRESERVE"),
              "Observation does not belong to the selected rubric", "INVALID_ARGUMENT")
        for key, budget in (("reason", 2000), ("counterinterpretation", 2000), ("narrative_function", 1000)):
            _need(isinstance(obs[key], str) and obs[key].strip() and len(obs[key]) <= budget,
                  "Reading requires a bounded reason, narrative function and counterinterpretation", "INVALID_ARGUMENT")
        edit = obs["requested_change"]
        _need((edit is None and directive == "PRESERVE") or isinstance(edit, str) and edit.strip() and len(edit) <= 1000,
              "Give a minimal edit or explicitly preserve the passage", "INVALID_ARGUMENT")
        primary = _citation(text_ref, text, obs["start"], obs["end"])
        related = []
        _need(isinstance(obs["related_spans"], list) and len(obs["related_spans"]) <= 32, "Invalid comparison evidence")
        for item in obs["related_spans"]:
            _need(isinstance(item, dict) and set(item) == {"text_ref", "start", "end"}, "Invalid comparison span")
            ref = item["text_ref"]
            validate_contract("ArtifactRef", ref)
            key = ref["artifact_revision_id"]
            _need(key in sources and sources[key][0] == ref, "Comparison text is outside the reading manifest")
            related.append(_citation(ref, sources[key][1], item["start"], item["end"]))
        _distinct_spans([primary, *related])
        if directive == "PRESERVE":
            continue
        _support(category, obs, sources, primary, related)
        dimension, roles = RUBRICS[category][pattern]
        judge = "PREFERENCE" if directive == "PREFERENCE" else "UNCERTAIN"
        evidence = [primary, *related]
        for check in verified_evidence:
            plane = {"WORLD_CONTRADICTION": "WORLD", "POV_LEAK": "BELIEF"}.get(pattern)
            if (plane and check["plane"] == plane and check["text_ref"] == text_ref
                    and check["text_span"] == primary["span"] and check["evidence_refs"]):
                judge = "CONTRADICTION"
                evidence.extend(check["evidence_refs"])
                break
        severity = obs.get("severity", "MAJOR" if judge == "CONTRADICTION" else "NOTE")
        _need(severity in ("NOTE", "MINOR", "MAJOR", "BLOCKER") and
              (severity != "BLOCKER" or judge == "CONTRADICTION"),
              "A hard blocker requires a verified factual contradiction", "INVALID_ARGUMENT")
        evidence = list({fingerprint(e): e for e in evidence}.values())
        _need(len(evidence) <= 64, "Too many distinct sources for a single finding", "CAPACITY_LIMIT")
        finding = {"dimension": dimension, "target_ref": text_ref, "text_span": primary["span"],
                   "severity": severity, "judgment": judge, "reason": obs["reason"],
                   "requested_change": edit, "evidence_refs": evidence}
        identity = fingerprint([pattern, primary, sorted(related, key=fingerprint), judge])
        if identity not in seen:
            seen.add(identity)
            result.append({"finding": finding, "category": category, "pattern": pattern,
                           "quote": _excerpt(sources, primary)[:512], "counterinterpretation": obs["counterinterpretation"],
                           "narrative_function": obs["narrative_function"], "upstream_roles": list(roles),
                           "related_evidence": related})
    return result
