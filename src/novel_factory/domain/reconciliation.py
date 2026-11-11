"""Pure deterministic checks for literary claims against one immutable world.

An assertion is a candidate interpretation of text. Neither this module nor its
caller applies effects; selected occurrence identities are only linked. These
checks cannot prove semantic extraction completeness, which needs independent
full-text review and explicit critical scene boundaries.
"""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256

from novel_factory.domain.errors import DomainError
from novel_factory.domain.temporal import point_key
from novel_factory.domain.world import state_at


def span_for(text, start=0, end=None):
    end = len(text) if end is None else end
    if isinstance(start, bool) or isinstance(end, bool) or not isinstance(start, int) or not isinstance(end, int) or not 0 <= start < end <= len(text):
        raise DomainError("INVALID_EVIDENCE", "Text span is outside the exact immutable text")
    return {"start": start, "end": end, "unit": "UNICODE_CODEPOINT",
            "quote_sha256": sha256(text[start:end].encode("utf-8")).hexdigest()}


def verify_span(text, span):
    if span != span_for(text, span["start"], span["end"]):
        raise DomainError("INVALID_EVIDENCE", "Text span does not match immutable Unicode text")


def selected(revision, event_ref):
    return revision["selection"].get(event_ref["occurrence_id"]) == event_ref["event_version_id"]


def state_check(revision, check):
    """A failed or unadvanced check never becomes a negative world fact."""
    frontier = revision["frontiers"].get(check["subject_id"])
    if frontier is None or point_key(frontier) <= point_key(check["at"]):
        return "UNRESOLVED"
    if check["plane"] == "BELIEF":
        frontier = revision["frontiers"].get(check["actor_id"])
        if frontier is None or point_key(frontier) <= point_key(check["known_at"]):
            return "UNRESOLVED"
    answer = state_at(revision["snapshot"], check["subject_id"], check["predicate"], check["at"],
                      actor_id=check["actor_id"], known_at=check["known_at"])
    if answer["value_status"] == "UNRESOLVED":
        return "UNRESOLVED"
    return "PASS" if answer["value_status"] == check["expected_status"] and answer["data"] == check["expected_value"] else "FAIL"


def actor_evidence(snapshot, actor_id, known_at):
    """Only sources actually acquired by this actor by the declared moment."""
    refs = []
    for record in snapshot["cognitions"]:
        if record["actor_id"] == actor_id and point_key(record["acquired_at"]) <= point_key(known_at):
            refs.extend(record["evidence_refs"])
    for relation in snapshot["relations"]:
        if relation.get("plane") == "BELIEF" and relation.get("actor_id") == actor_id and point_key(relation["valid_time"]["start"]) <= point_key(known_at):
            refs.extend(relation["evidence_refs"])
    return refs


def knowledge_check(revision, use):
    # There is no registered SPEECH occurrence type in the current kernel.
    # OBSERVATION records reception, not a proven utterance by a speaker.
    if use["mode"] == "SAYS":
        return "UNRESOLVED"
    if use["mode"] == "GUESSES":
        allowed = actor_evidence(revision["snapshot"], use["actor_id"], use["at"])
        return "PASS" if all(e in allowed for e in use["evidence_refs"]) else "FAIL"
    check = {"plane": "BELIEF", "actor_id": use["actor_id"], "known_at": use["at"],
             "subject_id": use["subject_id"], "predicate": use["predicate"], "at": use["at"],
             "expected_status": "VALUE", "expected_value": use["value"]}
    status = state_check(revision, check)
    if status != "PASS":
        return status
    answer = state_at(revision["snapshot"], use["subject_id"], use["predicate"], use["at"],
                      actor_id=use["actor_id"], known_at=use["at"])
    if not use["evidence_refs"] or not all(ref in answer["evidence_refs"] for ref in use["evidence_refs"]):
        return "FAIL"
    if use["mode"] == "KNOWS":
        check.update(plane="WORLD", actor_id=None, known_at=None)
        return state_check(revision, check)
    return "PASS"


def evaluate(revision, scene, draft, assertions, text, resolved_assertions=()):
    """Return deterministic checks, blockers, and links; never world mutations.

    Resolution identities are supplied only after the application has matched
    the accepted event's command closure to the exact proposed command.
    """
    checks, blockers, events = [], [], {}
    resolved = set(resolved_assertions)
    seen, declared = set(), set()

    def record(key, category, status, message, assertion=None, evidence=None):
        checks.append({"check_id": key, "category": category, "status": status,
                       "evidence_refs": evidence or [], "explanation": message})
        if status != "PASS":
            blockers.append({"code": "UNRESOLVED_" + category if status == "UNRESOLVED" else category + "_CONFLICT",
                             "assertion_id": assertion, "message": message})

    for check in [*scene["entry_checks"], *scene["exit_checks"]]:
        record(check["check_id"], "STATE", state_check(revision, check), "Scene boundary must match the accepted world")
    for assertion in assertions:
        aid, category = assertion["assertion_id"], assertion["classification"]
        if aid in seen:
            raise DomainError("INVALID_ARGUMENT", "Assertion identities must be unique")
        seen.add(aid)
        verify_span(text, assertion["span"])
        record(aid + ":span", "TEXT_SPAN", "PASS", "Exact text span hash verified", aid)
        if assertion["declared_delta_index"] is not None:
            index = assertion["declared_delta_index"]
            if index >= len(draft["declared_important_deltas"]) or index in declared:
                raise DomainError("INVALID_ARGUMENT", "Declared delta mapping is invalid or ambiguous")
            declared.add(index)
            record(aid + ":declared", "DECLARED_DELTA_COVERAGE", "FAIL" if category == "SURFACE_DETAIL" else "PASS",
                   "Declared important changes cannot be classified as incidental decoration", aid)
        event = assertion["event_ref"]
        if event is not None:
            ok = selected(revision, event)
            record(aid + ":event", "EVENT_SELECTION", "PASS" if ok else "FAIL",
                   "Narrated occurrence must be selected at the adopted world cut", aid)
            if ok:
                events[(event["occurrence_id"], event["event_version_id"])] = deepcopy(event)
        if assertion["world_check"] is not None:
            record(aid + ":world", "STATE", state_check(revision, assertion["world_check"]),
                   "Text's persistent factual assertion must match world state", aid)
        if assertion["knowledge_use"] is not None:
            use = assertion["knowledge_use"]
            status = knowledge_check(revision, use)
            contract = scene.get("scene_contract")
            if contract is not None and point_key(use["at"]) >= point_key(contract["story_interval"]["end"]):
                status = "FAIL"
            record(aid + ":knowledge", "KNOWLEDGE", status,
                   "Character knowledge must be acquired legally before this scene's exclusive end", aid)
        if category in {"HISTORICAL_CONFLICT", "UNCERTAIN"}:
            record(aid + ":classification", "STATE", "FAIL" if category == "HISTORICAL_CONFLICT" else "UNRESOLVED",
                   "Historical conflict or uncertain important text requires revision or review", aid)
        if assertion["proposed_command"] is not None and aid not in resolved:
            record(aid + ":new", "IMPORTANT_EFFECT_COVERAGE", "FAIL", "New world effect remains pending formal acceptance", aid)
        if category == "SPEECH_OR_BELIEF":
            use = assertion["knowledge_use"]
            legal = use is not None and (use["mode"] != "SAYS" or event is not None or aid in resolved)
            record(aid + ":speech", "KNOWLEDGE", "PASS" if legal else "UNRESOLVED",
                   "A new utterance needs a speech event; its proposition is not promoted to truth", aid)
        if category == "READER_DISCLOSURE_CHANGE":
            disclosure = next((x for x in scene["allowed_disclosures"] if x["disclosure_id"] == assertion["disclosure_id"]), None)
            status = "PASS" if disclosure is not None else "FAIL"
            if disclosure is not None and disclosure["truth_status"] == "WORLD_FACT":
                status = state_check(revision, disclosure["world_check"]) if disclosure["world_check"] is not None else "FAIL"
                if not disclosure["source_event_refs"] or not all(selected(revision, e) for e in disclosure["source_event_refs"]):
                    status = "FAIL"
            record(aid + ":disclosure", "DISCOURSE", status,
                   "Reader disclosure must match this arrangement and its declared factual evidence", aid)
    record("declared-delta-coverage", "DECLARED_DELTA_COVERAGE",
           "PASS" if declared == set(range(len(draft["declared_important_deltas"]))) else "FAIL",
           "Every writer-declared important change requires a classified span")
    required = {(e["occurrence_id"], e["event_version_id"]) for e in scene["required_event_refs"]}
    record("required-event-coverage", "IMPORTANT_EFFECT_COVERAGE", "PASS" if required <= events.keys() else "FAIL",
           "Every event required for this scene must have an exact narrated occurrence link")
    return {"checks": checks, "blockers": blockers, "event_refs": [events[k] for k in sorted(events)]}
