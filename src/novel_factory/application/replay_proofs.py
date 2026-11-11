"""Bounded process-local proofs of a pure reducer result, independent of grants."""
from collections import OrderedDict
from threading import Lock

from novel_factory.domain import reducer, world
from novel_factory.domain.canonical import fingerprint
from novel_factory.domain.errors import DomainError
from novel_factory.domain.temporal import point_key


def _fallback_reason(events, candidates, template, parent, work):
    if template == "REPAIR_EVENT":
        return "historical_replacement"
    if len(candidates) != 1 or not events or events[-1] != candidates[0]:
        return "not_one_append"
    event = candidates[0]
    prefix = events[:-1]
    if (parent["interpretation_ref"] != work["interpretation_ref"]
            or parent["snapshot"]["projection_version"] != world.PROJECTION_VERSION
            or any(e["engine_version"] != world.INTERPRETER_VERSION or e["ruleset_ref"] != work["rules_ref"]
                   for e in events)):
        return "interpretation_or_rules"
    selection = {e["occurrence_id"]: e["event_version_id"] for e in prefix}
    if len(selection) != len(prefix) or selection != parent["selection"]:
        return "selection_mismatch"
    if any(e["occurrence_id"] == event["occurrence_id"] or e["event_version_id"] == event["event_version_id"]
           or e["effect_group_id"] == event["effect_group_id"] for e in prefix):
        return "historical_identity_overlap"
    if any(e["causal_parents"] or e["occurrence_time"]["kind"] != "EXACT" for e in events):
        return "complex_causality_or_time"
    at = point_key(event["occurrence_time"]["at"])
    for old in prefix:
        if point_key(old["occurrence_time"]["at"]) >= at:
            return "not_strictly_later"
        if any(effect["kind"] == "RELATION_TRANSITION" and point_key(effect["at"]) >= at
               for effect in old["effects"]):
            return "prior_effect_reaches_suffix"
    return None


class ReplayProofs:
    """Remember only fixed-size content digests; every miss uses full replay.

    An entry proves a result for a genesis, selected event bodies, interpreter
    and rule context. The kernel independently loads and checks the complete
    authoritative revision. No world, permission, candidate or SQL row is
    served from this table. Eviction and restart only remove opportunities.
    """
    def __init__(self, capacity=256):
        self._proofs = OrderedDict()
        self._capacity = capacity
        self._lock = Lock()

    def evaluate(self, options, genesis, events, candidates, template, parent, work):
        try:
            reason = _fallback_reason(events, candidates, template, parent, work)
        except (KeyError, TypeError, AttributeError, DomainError):
            # The complete reducer owns schema/error precedence for corrupt rows.
            reason = "unrecognized_input"
        if reason is not None:
            options.record("replay", reason)
            return reducer.replay(genesis, events)

        # Genesis entity iteration order affects lifecycle_history. The only
        # event-input-order-dependent projection field is the summary list,
        # restored explicitly by the suffix reducer. Temporal order (including
        # ties) is determined by the reducer's deterministic topological sort.
        try:
            context = fingerprint([parent["work_id"], parent["branch_id"], work["interpretation_ref"],
                                   work["rules_ref"], work["interpretation_epoch"],
                                   parent["provenance"].get("history_epoch", 0),
                                   world.INTERPRETER_VERSION, world.PROJECTION_VERSION,
                                   {k: list(v) for k, v in world.PREDICATES.items()}, sorted(world._TERMINAL),
                                   genesis, list(genesis.get("entities", {}))])
            prefix = events[:-1]
            hashes = [fingerprint(e) for e in prefix]
            prefix_hash = fingerprint(sorted(hashes))
        except (KeyError, TypeError, AttributeError, DomainError):
            options.record("replay", "unrecognized_input")
            return reducer.replay(genesis, events)
        with self._lock:
            known = (context, prefix_hash, parent["state_hash"]) in self._proofs
        if known:
            options.record("replay", "hit")
            result = world._append_verified_suffix(parent["snapshot"], prefix, events[-1])
        else:
            options.record("replay", "unverified_parent")
            result = reducer.replay(genesis, events)

        hashes.append(fingerprint(events[-1]))
        proof = (context, fingerprint(sorted(hashes)), fingerprint(result))
        with self._lock:
            self._proofs[proof] = None
            self._proofs.move_to_end(proof)
            if len(self._proofs) > self._capacity:
                self._proofs.popitem(last=False)
        return result
