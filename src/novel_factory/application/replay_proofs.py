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


def _context(genesis, parent, work):
    # Genesis entity iteration order is visible in lifecycle_history.
    return fingerprint([parent["work_id"], parent["branch_id"], work["interpretation_ref"],
                        work["rules_ref"], work["interpretation_epoch"],
                        parent["provenance"].get("history_epoch", 0),
                        world.INTERPRETER_VERSION, world.PROJECTION_VERSION,
                        {k: list(v) for k, v in world.PREDICATES.items()}, sorted(world.TERMINAL_LIFECYCLES),
                        genesis, list(genesis.get("entities", {}))])


class ReplayProofs:
    """Remember only fixed-size content digests; every miss uses full replay.

    An entry proves a result for a genesis, selected event bodies, interpreter
    and rule context. The kernel independently loads and checks the complete
    authoritative revision. No world, permission, candidate or SQL row is
    served from this table. Eviction and restart only remove opportunities.
    """
    def __init__(self, capacity=256):
        self._proofs = OrderedDict()
        self._prime_hints = OrderedDict()
        self._priming = set()
        self._capacity = capacity
        self._lock = Lock()

    def _remember(self, context, event_hashes, snapshot):
        self._store_proof((context, fingerprint(sorted(event_hashes)), fingerprint(snapshot)))

    def _store_proof(self, proof):
        with self._lock:
            self._proofs[proof] = None
            self._proofs.move_to_end(proof)
            if len(self._proofs) > self._capacity:
                self._proofs.popitem(last=False)

    def _reserve_prime(self, key):
        # Repeated cold proposals are a workload hint, never a proof. Cap the
        # backlog and allow at most two independent parent checks per process.
        with self._lock:
            if self._capacity <= 0 or key in self._priming:
                return "busy"
            if key not in self._prime_hints:
                self._prime_hints[key] = False
                if len(self._prime_hints) > self._capacity:
                    self._prime_hints.popitem(last=False)
                return "first"
            if self._prime_hints[key]:
                return "already_tried"
            if len(self._priming) >= 2:
                return "busy"
            self._prime_hints[key] = True
            self._prime_hints.move_to_end(key)
            self._priming.add(key)
            return "attempt"

    def _prime_parent(self, options, genesis, prefix, parent, work):
        key = (parent["work_id"], parent["branch_id"], parent.get("world_revision_id"), parent["state_hash"])
        reservation = self._reserve_prime(key)
        if reservation != "attempt":
            options.record("replay_proof", "prime_" + reservation)
            return False
        try:
            options.record("replay_proof", "prime_attempt")
            # The parent was originally reduced in the order visible in its
            # event-summary list. PostgreSQL may reorder keys in selection
            # JSONB. Use the recorded order solely to *try* a full replay of
            # authoritative event bodies; demand exact whole-state equality.
            selected = {e["event_version_id"]: e for e in prefix}
            order = [e["event_ref"]["event_version_id"] for e in parent["snapshot"]["events"]]
            if len(order) != len(prefix) or len(selected) != len(prefix) or set(order) != selected.keys():
                options.record("replay_proof", "prime_mismatch")
                return False
            original = reducer.replay(genesis, [selected[eid] for eid in order])
            if fingerprint(original) != parent["state_hash"] or original != parent["snapshot"]:
                options.record("replay_proof", "prime_mismatch")
                return False
            context = _context(genesis, parent, work)
            prefix_hash = fingerprint(sorted(fingerprint(e) for e in prefix))
            self._store_proof((context, prefix_hash, parent["state_hash"]))
            return True
        except Exception:
            # Optional proof work must not change the ordinary full replay error.
            options.record("replay_proof", "prime_failed")
            return False
        finally:
            with self._lock:
                self._priming.discard(key)

    def evaluate(self, options, genesis, events, candidates, template, parent, work, *, proposal=False):
        lazy = getattr(options, "replay_lazy", False)
        try:
            reason = _fallback_reason(events, candidates, template, parent, work)
        except (KeyError, TypeError, AttributeError, DomainError):
            # The complete reducer owns schema/error precedence for corrupt rows.
            reason = "unrecognized_input"
        if reason is not None:
            options.record("replay", reason)
            result = reducer.replay(genesis, events)
            # Full replay also proves a *new* parent after a valid same-time or
            # earlier append. That append itself must never take the suffix path.
            if lazy and not proposal and reason == "not_strictly_later":
                try:
                    self._remember(_context(genesis, parent, work), [fingerprint(e) for e in events], result)
                except (KeyError, TypeError, AttributeError, DomainError):
                    # Optional admission cannot change a successful full replay.
                    pass
                else:
                    options.record("replay_proof", "full_replay_seed")
            return result

        if lazy and proposal:
            with self._lock:
                possible = any(proof[2] == parent["state_hash"] for proof in self._proofs)
            if not possible:
                if getattr(options, "replay_prime", False) and self._prime_parent(
                        options, genesis, events[:-1], parent, work):
                    # A successful prime paid a complete parent replay on this
                    # request. Keep its hit separate from later proof reuse.
                    try:
                        result = world._append_verified_suffix(parent["snapshot"], events[:-1], events[-1])
                    except Exception:
                        options.record("replay_proof", "prime_suffix_failed")
                    else:
                        options.record("replay_proof", "primed_hit")
                        options.record("replay", "hit")
                        return result
                # A negative prefilter can only select the complete reducer.
                options.record("replay", "unverified_parent")
                options.record("replay_proof", "cold_proposal_skipped")
                return reducer.replay(genesis, events)

        # JSONB selection order can change. The suffix reducer restores the
        # caller's event-summary order; the proof binds the entire body multiset.
        try:
            context = _context(genesis, parent, work)
            prefix = events[:-1]
            hashes = [fingerprint(e) for e in prefix]
            prefix_hash = fingerprint(sorted(hashes))
        except (KeyError, TypeError, AttributeError, DomainError):
            options.record("replay", "unrecognized_input")
            return reducer.replay(genesis, events)
        with self._lock:
            known = (context, prefix_hash, parent["state_hash"]) in self._proofs
        if known:
            priming = lazy and getattr(options, "replay_prime", False)
            try:
                result = world._append_verified_suffix(parent["snapshot"], prefix, events[-1])
            except Exception:
                if not priming:
                    options.record("replay", "hit")
                    raise
                options.record("replay", "suffix_recheck")
                return reducer.replay(genesis, events)
            options.record("replay", "hit")
            if priming:
                options.record("replay_proof", "reuse_hit")
        else:
            options.record("replay", "unverified_parent")
            result = reducer.replay(genesis, events)

        if not (lazy and proposal):
            hashes.append(fingerprint(events[-1]))
            self._remember(context, hashes, result)
        return result
