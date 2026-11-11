"""Finite proof of an accepted event's alternative atomic representation.

This is intentionally not a solver for open-ended narrative equivalence. Each
micro step contains only registered effects. A proof compares every temporal
transition and every piecewise-constant observable cut of the complete selected
history. Resource partitions may change; rights, observations, private decisions,
belief evidence and causal identities may not. Simultaneous allocations use the
common starting balance, never another participant's simultaneous credit.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from decimal import Decimal, localcontext
from uuid import UUID, uuid5

from novel_factory.contracts.registry import validate_contract
from novel_factory.domain.canonical import fingerprint
from novel_factory.domain.errors import DomainError
from novel_factory.domain.temporal import PHASE_RADIX, contains, point_key
from novel_factory.domain.world import (
    INTERPRETER_VERSION,
    decimal_string,
    lifecycle_at,
    replay,
    resource_at,
)

PROOF_VERSION = "nf-finite-refinement-1"
MAX_SELECTED_EVENTS = 200
MAX_EFFECTS = 256
MAX_BOUNDARIES = 1024


def _require(condition, code, message):
    if not condition:
        raise DomainError(code, message)


def _at(key):
    return {"tick": key[0], "phase": key[1]}


def _before(key):
    if key == (-9007199254740991, 0):
        return key  # The calendar has no representable point before its minimum.
    return (key[0], key[1] - 1) if key[1] else (key[0] - 1, PHASE_RADIX - 1)


def _event_ref(event):
    return {key: event[key] for key in ("occurrence_id", "event_version_id")}


def _frames(effects):
    """Keep gross debit/credit vectors: net-zero circulation is observable use."""
    grouped = defaultdict(lambda: {"debits": defaultdict(Decimal),
                                   "credits": defaultdict(Decimal), "other": []})
    keys = set()
    with localcontext() as context:
        context.prec = 200
        for at, effect in effects:
            identity = effect["effect_key"]
            _require(identity not in keys, "DUPLICATE_EFFECT", "A micro effect identity repeats")
            keys.add(identity)
            frame = grouped[point_key(at)]
            if effect["kind"] == "RESOURCE_DELTA":
                quantity = effect["quantity"]
                _require(len(quantity) <= 80, "CAPACITY_LIMIT", "Resource precision is outside the proof budget")
                value = Decimal(quantity)
                _require(value != 0, "UNPROVEN_EQUIVALENCE", "Zero resource atoms are not a registered refinement")
                slot = (effect["account_id"], effect["resource_id"], effect["unit"])
                frame["debits" if value < 0 else "credits"][slot] += abs(value)
            else:
                frame["other"].append(deepcopy(effect))
    result = {}
    for at, frame in grouped.items():
        result[at] = {
            name: [{"account_id": key[0], "resource_id": key[1], "unit": key[2],
                    "quantity": decimal_string(value)}
                   for key, value in sorted(frame[name].items())]
            for name in ("debits", "credits")
        }
        result[at]["other"] = sorted(frame["other"], key=fingerprint)
    return result


def _all_boundaries(snapshot, events, schedule, extra):
    points = set(extra)
    for entity in snapshot["entities"].values():
        points.add(point_key(entity["created_at"]))
    for relation in snapshot["relations"]:
        points.add(point_key(relation["valid_time"]["start"]))
        if relation["valid_time"]["end"] is not None:
            points.add(point_key(relation["valid_time"]["end"]))
    for collection, field in (("resource_history", "at"), ("lifecycle_history", "at"),
                              ("state_overrides", "at"), ("cognitions", "acquired_at")):
        points.update(point_key(value[field]) for value in snapshot[collection])
    for event in events:
        _require(event["occurrence_time"]["kind"] == "EXACT", "UNPROVEN_EQUIVALENCE",
                 "Finite refinement requires resolved times throughout selected history")
        points.add(point_key(event["occurrence_time"]["at"]))
        points.update(point_key(e["at"]) for e in event["effects"] if "at" in e)
    # Registered messages/processes keep their complete immutable schedule.
    # Their time boundaries are still sampled, even if no world effect occurs.
    for entry in schedule.get("processes", {}).values():
        definition = entry["definition"]
        for value in (definition.get("start_at"), definition.get("due_at"),
                      entry.get("next_due"), entry.get("evaluated_frontier")):
            if value is not None:
                points.add(point_key(value))
        for channel in definition.get("channels", []):
            if channel["earliest_possible_impact"] is not None:
                points.add(point_key(channel["earliest_possible_impact"]))
        for evaluation in entry.get("evaluations", []):
            points.add(point_key(evaluation["at"]))
    points |= {_before(p) for p in tuple(points)}
    _require(len(points) <= MAX_BOUNDARIES, "CAPACITY_LIMIT", "Observable proof boundary budget exceeded")
    return sorted(points)


def _normalize(value, old, new):
    """Only explicitly mapped representation provenance may differ."""
    versions = {old["event_version_id"], new["event_version_id"]}
    def visit(item):
        if isinstance(item, dict):
            result = {key: visit(v) for key, v in item.items()}
            if item.get("occurrence_id") == old["occurrence_id"] and item.get("event_version_id") in versions:
                result["event_version_id"] = old["event_version_id"]
            # Both newly opened and previously existing, now closed relation
            # rows derive a new version. Map only this exact reducer derivation;
            # never replace arbitrary UUID-shaped statement or entity strings.
            if "relation_id" in item and "relation_version_id" in item:
                relation_id = item["relation_id"]
                for operation in ("OPEN", "CLOSE"):
                    for version in versions:
                        derived = str(uuid5(UUID(version), f"relation:{relation_id}:{operation}"))
                        if item["relation_version_id"] == derived:
                            result["relation_version_id"] = str(uuid5(
                                UUID(old["event_version_id"]), f"relation:{relation_id}:{operation}"))
            return result
        if isinstance(item, list):
            return sorted((visit(v) for v in item), key=fingerprint)
        return item
    return visit(value)


def _observable_cut(snapshot, at):
    entities = {eid: entity for eid, entity in snapshot["entities"].items()
                if point_key(entity["created_at"]) <= point_key(at)}
    resources = {(row["account_id"], row["resource_id"], row["unit"])
                 for row in snapshot["resource_history"]
                 if row["account_id"] in entities and row["resource_id"] in entities}
    return {
        "entities": entities,
        "relations": [row for row in snapshot["relations"] if contains(row["valid_time"], at)],
        "resources": [resource_at(snapshot, *key, at) for key in sorted(resources)],
        "lifecycle": {eid: lifecycle_at(snapshot, eid, at) for eid in entities},
        "cognitions": [row for row in snapshot["cognitions"]
                       if point_key(row["acquired_at"]) <= point_key(at)],
        "state_overrides": [row for row in snapshot["state_overrides"]
                            if point_key(row["at"]) <= point_key(at)],
        "events": [row for row in snapshot["events"]
                   if point_key(row["occurrence_time"]["at"]) <= point_key(at)],
    }


def prove_refinement(genesis, selected_events, macro_event_ref, steps, *, schedule=None):
    """Return replacement bytes and a finite proof, or fail without side effects.

    Exact registered transition equality is stronger than final-state equality.
    All history is rebuilt from genesis, then all observable cuts on both sides
    of every relevant boundary are compared. Unknown temporal schedules and
    semantic claims outside the registered effect language are rejected.
    """
    _require(len(selected_events) <= MAX_SELECTED_EVENTS, "CAPACITY_LIMIT",
             "Finite refinement exceeds the reviewed history budget")
    _require(all(event["engine_version"] == INTERPRETER_VERSION for event in selected_events),
             "UNPROVEN_EQUIVALENCE", "Historical interpreter is unsupported by this proof version")
    validate_contract("EventRef", macro_event_ref)
    _require(0 < len(steps) <= 64, "CAPACITY_LIMIT", "Micro step budget exceeded")
    steps = [validate_contract("MicroStep", value, "refinement") for value in steps]
    macros = [e for e in selected_events if _event_ref(e) == macro_event_ref]
    _require(len(macros) == 1, "INVALID_EVIDENCE", "Macro event is not the uniquely selected occurrence")
    macro = deepcopy(macros[0])
    _require(macro["occurrence_time"]["kind"] == "EXACT", "UNPROVEN_EQUIVALENCE",
             "Macro occurrence needs one resolved atomic phase")
    at = macro["occurrence_time"]["at"]
    _require(all(e.get("at", at) == at for e in macro["effects"]), "UNPROVEN_EQUIVALENCE",
             "Delayed-effect repair is outside this finite protocol")
    by_id = {step["step_id"]: step for step in steps}
    _require(len(by_id) == len(steps), "DUPLICATE_EFFECT", "Micro step identity repeats")
    participants = set()
    micro = []
    for step in steps:
        participants.update(step["participant_ids"])
        _require(bool(step["effects"]), "UNPROVEN_EQUIVALENCE",
                 "Surface-only text is not an authoritative micro effect step")
        for previous in step["depends_on"]:
            _require(previous in by_id and point_key(by_id[previous]["at"]) < point_key(step["at"]),
                     "UNPROVEN_EQUIVALENCE", "Micro causal edges require strictly earlier phases")
        for effect in step["effects"]:
            _require(effect.get("at", step["at"]) == step["at"], "UNPROVEN_EQUIVALENCE",
                     "An effect boundary differs from its micro step")
            if effect["kind"] == "COGNITIVE_RECORD_ADD":
                _require(effect["actor_id"] in step["participant_ids"], "UNPROVEN_EQUIVALENCE",
                         "Observation or decision actor is absent from its micro step")
            micro.append((step["at"], effect))
    _require(participants == set(macro["participants"]), "UNPROVEN_EQUIVALENCE",
             "The accepted participant boundary cannot gain or lose observers")
    _require(len(micro) <= MAX_EFFECTS, "CAPACITY_LIMIT", "Micro effect budget exceeded")
    before_frames = _frames([(effect.get("at", at), effect) for effect in macro["effects"]])
    after_frames = _frames(micro)
    _require(before_frames == after_frames, "REFINEMENT_NOT_EQUIVALENT",
             "Micro transitions differ in time, resource use, rights, cognition, evidence or messages")

    snapshot_before = replay(genesis, selected_events)
    # Shared resources are checked against the common state strictly before
    # the atomic phase. Positive deltas cannot fund a simultaneous withdrawal.
    for moment, frame in after_frames.items():
        for debit in frame["debits"]:
            available = resource_at(snapshot_before, debit["account_id"], debit["resource_id"],
                                    debit["unit"], _at(_before(moment)))
            _require(Decimal(debit["quantity"]) <= Decimal(available["quantity"]),
                     "RESOURCE_CONFLICT", "Simultaneous micro allocations exceed common starting resources")

    basis = fingerprint({"macro": macro, "steps": sorted(steps, key=lambda s: s["step_id"]),
                         "proof_version": PROOF_VERSION})
    namespace = UUID(macro["occurrence_id"])
    replacement = deepcopy(macro)
    replacement["event_version_id"] = str(uuid5(namespace, "refinement-event:" + basis))
    replacement["representation_id"] = str(uuid5(namespace, "refinement-representation:" + basis))
    replacement["effects"] = [deepcopy(effect) for step in sorted(steps, key=lambda s: s["step_id"])
                               for effect in step["effects"]]
    history_after = [replacement if _event_ref(e) == macro_event_ref else e for e in selected_events]
    # Exact causal event-version dependencies that cannot be preserved block
    # replacement; they never get silently rebound to the new representation.
    snapshot_after = replay(genesis, history_after)
    schedule = schedule or {"processes": {}, "closed_interactions": {}}
    boundaries = _all_boundaries(snapshot_before, selected_events, schedule,
                                 {point_key(step["at"]) for step in steps})
    boundaries = sorted(set(boundaries) | set(_all_boundaries(snapshot_after, history_after,
                                                            schedule, set())))
    _require(len(boundaries) <= MAX_BOUNDARIES, "CAPACITY_LIMIT", "Observable proof boundary budget exceeded")
    cuts = []
    for boundary in boundaries:
        moment = _at(boundary)
        before = _normalize(_observable_cut(snapshot_before, moment), macro, replacement)
        after = _normalize(_observable_cut(snapshot_after, moment), macro, replacement)
        before_hash, after_hash = fingerprint(before), fingerprint(after)
        _require(before_hash == after_hash, "REFINEMENT_NOT_EQUIVALENT",
                 "An intermediate observable cut differs after representation replacement")
        cuts.append({"at": moment, "before_sha256": before_hash, "after_sha256": after_hash})
    return replacement, {
        "proof_version": PROOF_VERSION, "status": "PASS", "scope": "FINITE_REGISTERED_EFFECTS",
        "source_event_ref": macro_event_ref, "replacement_event_ref": _event_ref(replacement),
        "source_event_sha256": fingerprint(macro), "replacement_event_sha256": fingerprint(replacement),
        "source_history_sha256": fingerprint(sorted(selected_events, key=lambda e: e["occurrence_id"])),
        "source_projection_sha256": fingerprint(snapshot_before),
        "replacement_projection_sha256": fingerprint(snapshot_after),
        "schedule_sha256": fingerprint(schedule), "boundaries": cuts,
        "event_count_before": len(selected_events), "event_count_after": len(history_after),
        "causal_parents_preserved": replacement["causal_parents"] == macro["causal_parents"],
        "common_start_resources": "PASS", "open_semantic_equivalence": "NOT_PROVEN",
        "literary_review": "REQUIRED_SEPARATELY",
    }
