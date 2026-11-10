"""Finite R1 mechanism probes and deliberately broken contrasts.

This is a single-threaded reference model with explicit interleavings, not the
factory, a SQL implementation, a security boundary, or a performance benchmark.
Scope footprints are fixture-authored trusted inputs. Production must derive and
enforce them through registered templates and test them against PostgreSQL.

Run: python -m unittest discover -s experiments -p 'test_r1_mechanisms.py' -v
"""

from dataclasses import asdict, dataclass
import hashlib
import json
import unittest


def pairs(values):
    """Fixtures contain only scalar values; tuples freeze a sampled observation."""
    return tuple(sorted(values.items()))


@dataclass(frozen=True)
class Pin:
    head: int
    facts: tuple
    tokens: tuple


@dataclass(frozen=True)
class Proposal:
    proposal_id: str
    base: int
    generation: int
    original_tokens: tuple
    preconditions: tuple
    changes: tuple
    bumps: tuple


@dataclass(frozen=True)
class Certificate:
    certificate_id: int
    proposal: Proposal
    mode: str
    validation_head: int
    sampled_tokens: tuple


@dataclass(frozen=True)
class Receipt:
    original_base: int
    validation_head: int
    parent: int
    world_ref: int


class ReferenceMachine:
    """One branch, immutable world snapshots, monotone guards, no persistence.

    A pin represents one consistent sampling operation. Python scheduling here
    does not establish that a database adapter actually samples atomically.
    """

    COMMON = {"INTERPRETATION", "AUTHORIZATION", "HISTORY"}

    def __init__(self, facts=None, scopes=(), frontier=0):
        self.head = 0
        self.facts = dict(facts or {})
        self.tokens = {scope: 0 for scope in self.COMMON | set(scopes)}
        self.history = {0: pairs(self.facts)}
        self.frontiers = {0: frontier}  # Exclusive story-time boundary.
        self.generation = 1
        self.authorized = True
        self.interpretation = "I1"
        self.active_plan = "plan-1"
        self.plans = {"plan-1": ("return S at D50",),
                      "plan-2": ("use an alternative at D60",)}
        self.issued = {}
        self.receipts = {}
        self.accepted_proposals = set()

    def pin(self):
        return Pin(self.head, pairs(self.facts), pairs(self.tokens))

    def propose(self, proposal_id, changes, scopes, reads=(), bumps=None):
        # This method represents the trusted fixture template, not Agent input.
        sample = self.pin()
        required = self.COMMON | set(scopes)
        if not required <= self.tokens.keys():
            raise ValueError("UNREGISTERED_SCOPE")
        sampled = dict(sample.tokens)
        sampled_facts = dict(sample.facts)
        return Proposal(proposal_id, sample.head, self.generation,
                        pairs({key: sampled[key] for key in required}),
                        pairs({key: sampled_facts[key] for key in reads}),
                        pairs(changes), tuple(sorted(bumps or scopes)))

    def prepare(self, proposal, mode="DEPENDENCY_GUARDED", sample=None):
        # A previously frozen sample models slow validation after releasing DB.
        sample = sample or self.pin()
        if mode == "STRICT_HEAD" and sample.head != proposal.base:
            raise ValueError("HEAD_CONFLICT")
        observed = dict(sample.tokens)
        if any(observed.get(key) != value
               for key, value in proposal.original_tokens):
            raise ValueError("DEPENDENCY_CHANGED")
        facts = dict(sample.facts)
        if any(facts.get(key) != value for key, value in proposal.preconditions):
            raise ValueError("PRECONDITION_CHANGED")
        cert = Certificate(len(self.issued) + 1, proposal, mode, sample.head,
                           pairs({key: observed[key]
                                  for key, _ in proposal.original_tokens}))
        self.issued[cert.certificate_id] = cert
        return cert

    def accept(self, command_key, cert):
        if not self.authorized:
            raise PermissionError("CURRENT_AUTHORIZATION_REQUIRED")
        command_hash = hashlib.sha256(json.dumps(
            asdict(cert), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if command_key in self.receipts:
            old_hash, receipt = self.receipts[command_key]
            if old_hash != command_hash:
                raise ValueError("IDEMPOTENCY_MISMATCH")
            return receipt
        if self.issued.get(cert.certificate_id) != cert:
            raise ValueError("UNISSUED_CERTIFICATE")
        p = cert.proposal
        if p.generation != self.generation:
            raise ValueError("STALE_GENERATION")
        if p.proposal_id in self.accepted_proposals:
            raise ValueError("PROPOSAL_ALREADY_ACCEPTED")
        if cert.mode == "STRICT_HEAD" and self.head != p.base:
            raise ValueError("HEAD_CONFLICT")
        if not p.base <= cert.validation_head <= self.head:
            raise ValueError("ANCESTRY_CONFLICT")  # One retained linear branch only.
        if any(self.tokens.get(key) != value for key, value in cert.sampled_tokens):
            raise ValueError("SCOPE_CHANGED")
        parent = self.head
        self.advance(dict(p.changes), p.bumps)
        receipt = Receipt(p.base, cert.validation_head, parent, self.head)
        self.accepted_proposals.add(p.proposal_id)
        self.receipts[command_key] = (command_hash, receipt)
        return receipt

    def advance(self, changes, bumps, frontier=None):
        # Controlled trusted transition; no network, reducer, transaction or crash.
        previous_frontier = self.frontiers[self.head]
        for scope in bumps:
            if scope not in self.tokens:
                raise ValueError("UNREGISTERED_SCOPE")
            self.tokens[scope] += 1
        self.facts.update(changes)
        self.head += 1
        self.history[self.head] = pairs(self.facts)
        self.frontiers[self.head] = previous_frontier if frontier is None else frontier

    def control_change(self, scope, value):
        # Selection/interpretation changes may invalidate without a world commit.
        self.tokens[scope] += 1
        if scope == "INTERPRETATION":
            self.interpretation = value
        elif scope == "ARTIFACT_SELECTION:active-plan":
            self.active_plan = value

    def cancel(self):
        self.generation += 1

    def revoke(self):
        self.authorized = False
        self.tokens["AUTHORIZATION"] += 1

    def state_coverage(self, ref, story_point, mode="FIXED", process="ALLOW_PARTIAL"):
        if mode == "FIXED" and process == "WAIT_EXISTING_ADVANCE":
            raise ValueError("FIXED_CANNOT_WAIT_FOR_FUTURE_REVISION")
        selected = ref if mode == "FIXED" else self.head
        return selected, ("EVALUATED" if story_point < self.frontiers[selected]
                          else "PENDING")


def read_pair(machine, one_pin, between_reads):
    sample = machine.pin()
    left = dict(sample.facts)["left"]
    between_reads()
    right = dict((sample if one_pin else machine.pin()).facts)["right"]
    return left, right


def cached_read(machine, cache, key):
    # Cache contents are not an authorization decision; check before every hit.
    if not machine.authorized:
        raise PermissionError("CURRENT_AUTHORIZATION_REQUIRED")
    return cache[key]


def query_batch(profile, grant, plane, operations):
    """Small authorization contrast; production also needs task/actor/time bounds."""
    planes, allowed_operations = profile
    if plane not in planes or plane + "_READ" not in grant:
        raise PermissionError("PLANE_DENIED")
    capability = {"STATE": "VIEW_READ", "TIMELINE": "VIEW_READ",
                  "NEIGHBORHOOD": "GRAPH_READ"}
    for operation in operations:
        if operation not in allowed_operations:
            raise PermissionError("PROFILE_OPERATION_DENIED")
        if capability[operation] not in grant:
            raise PermissionError("OPERATION_CAPABILITY_DENIED")
    return tuple("AUTHORIZED" for _ in operations)


def replay_sword(selected_events, day):
    """Only the new R1 fixture; no general temporal ontology or uncertain times."""
    owner = holder = None
    alive = False
    for when, kind, recipient in selected_events:
        if when > day:
            break
        if kind == "MAKE":
            owner = holder = recipient
            alive = True
        elif kind == "GIFT":
            owner = holder = recipient
        elif kind in {"LOAN", "STEAL"}:
            holder = recipient
        elif kind == "DESTROY":
            holder, alive = None, False
    return owner, holder, alive


class R1MechanismProbes(unittest.TestCase):
    def independent_commands(self, mode):
        m = ReferenceMachine({"north": 0, "south": 0}, ("region:north", "region:south"))
        north = m.propose("north", {"north": 1}, ("region:north",), ("north",))
        south = m.propose("south", {"south": 1}, ("region:south",), ("south",))
        return m, m.prepare(north, mode), m.prepare(south, mode)

    def test_01_guarded_independent_commands_share_base_and_certificate_cut(self):
        m, north, south = self.independent_commands("DEPENDENCY_GUARDED")
        first = m.accept("north-attempt", north)
        second = m.accept("south-attempt", south)
        self.assertEqual(m.facts, {"north": 1, "south": 1})
        self.assertEqual((first.original_base, second.original_base), (0, 0))
        self.assertEqual((second.validation_head, second.parent, second.world_ref), (0, 1, 2))

    def test_02_strict_head_rejects_the_same_independent_second_command(self):
        m, north, south = self.independent_commands("STRICT_HEAD")
        m.accept("north-attempt", north)
        with self.assertRaisesRegex(ValueError, "HEAD_CONFLICT"):
            m.accept("south-attempt", south)
        self.assertEqual(m.facts["south"], 0)

    def test_03_same_resource_invalidates_old_qualification(self):
        m = ReferenceMachine({"holder": "P"}, ("resource:S",))
        a = m.prepare(m.propose("a", {"holder": "F"}, ("resource:S",), ("holder",)))
        b = m.prepare(m.propose("b", {"holder": "V"}, ("resource:S",), ("holder",)))
        m.accept("a", a)
        with self.assertRaisesRegex(ValueError, "SCOPE_CHANGED"):
            m.accept("b", b)
        self.assertEqual((m.facts["holder"], m.head), ("F", 1))
        # This checks stale qualification, not which story action deserves to win.

    def test_04_cross_entity_constraint_stops_write_skew(self):
        m = ReferenceMachine({"A_on_duty": True, "B_on_duty": True},
                             ("entity:A", "entity:B", "constraint:at-least-one"))
        certs = []
        for actor in ("A", "B"):
            scopes = ("entity:" + actor, "constraint:at-least-one")
            certs.append(m.prepare(m.propose(actor, {actor + "_on_duty": False},
                                            scopes, ("A_on_duty", "B_on_duty"))))
        naive = dict(m.facts)
        naive_versions = {"A": 0, "B": 0}
        naive_accepts = 0
        for actor, cert in zip(("A", "B"), certs):
            # Broken validator checks only the entity it will write.
            if naive_versions[actor] == dict(cert.sampled_tokens)["entity:" + actor]:
                naive.update(dict(cert.proposal.changes))
                naive_versions[actor] += 1
                naive_accepts += 1
        self.assertEqual(naive_accepts, 2)
        self.assertFalse(any(naive.values()))
        m.accept("A", certs[0])
        with self.assertRaisesRegex(ValueError, "SCOPE_CHANGED"):
            m.accept("B", certs[1])
        self.assertTrue(any(m.facts.values()))

    def test_05_empty_read_needs_a_predicate_range_guard(self):
        m = ReferenceMachine({"bridge_guard_count": 0, "crossed": False},
                             ("range:bridge-guards", "entity:traveller"))
        p = m.propose("cross", {"crossed": True},
                      ("range:bridge-guards", "entity:traveller"),
                      ("bridge_guard_count",), ("entity:traveller",))
        cert = m.prepare(p)
        matched_row_versions = {}  # Naive empty SELECT has no returned row to track.
        m.advance({"bridge_guard_count": 1}, ("range:bridge-guards",))
        self.assertTrue(all(m.tokens.get(k) == v for k, v in matched_row_versions.items()))
        with self.assertRaisesRegex(ValueError, "SCOPE_CHANGED"):
            m.accept("cross", cert)
        self.assertFalse(m.facts["crossed"])

    def test_06_registry_tombstone_prevents_delete_recreate_aba(self):
        m = ReferenceMachine({"route": "open"}, ("registry:route",))
        cert = m.prepare(m.propose("use-route", {"route": "used"},
                                   ("registry:route",), ("route",)))
        old_naive_row_version = 1
        m.advance({"route": "absent"}, ("registry:route",))
        m.advance({"route": "open"}, ("registry:route",))
        recreated_naive_row_version = 1
        self.assertEqual(old_naive_row_version, recreated_naive_row_version)
        self.assertEqual(m.tokens["registry:route"], 2)
        with self.assertRaisesRegex(ValueError, "SCOPE_CHANGED"):
            m.accept("use-route", cert)

    def test_07_cancelled_generation_cannot_create_a_late_commit(self):
        m = ReferenceMachine({"count": 0}, ("counter",))
        cert = m.prepare(m.propose("late", {"count": 1}, ("counter",)))
        m.cancel()
        with self.assertRaisesRegex(ValueError, "STALE_GENERATION"):
            m.accept("late", cert)
        self.assertEqual((m.head, m.facts["count"]), (0, 0))

    def test_08_lost_receipt_retry_survives_cancel_without_repeating_effect(self):
        m = ReferenceMachine({"count": 0}, ("counter",))
        cert = m.prepare(m.propose("once", {"count": 1}, ("counter",)))
        receipt = m.accept("original-command", cert)
        m.cancel()
        for _ in range(5):
            self.assertEqual(m.accept("original-command", cert), receipt)
        self.assertEqual((m.head, m.facts["count"], len(m.receipts)), (1, 1, 1))

    def test_09_certificate_uses_facts_and_tokens_from_the_same_pin(self):
        m = ReferenceMachine({"bridge_guard_count": 0, "crossed": False},
                             ("range:bridge",))
        p = m.propose("cross", {"crossed": True}, ("range:bridge",),
                      ("bridge_guard_count",))
        old_sample = m.pin()
        old_validation_passed = dict(old_sample.facts)["bridge_guard_count"] == 0
        m.advance({"bridge_guard_count": 1}, ("range:bridge",))
        newer_sample = m.pin()
        # Broken design: old semantic answer plus freshly sampled guards.
        naive_would_accept = old_validation_passed and all(
            m.tokens[k] == v for k, v in newer_sample.tokens)
        self.assertTrue(naive_would_accept)
        self.assertEqual(m.facts["bridge_guard_count"], 1)
        safe = m.prepare(p, sample=old_sample)
        with self.assertRaisesRegex(ValueError, "SCOPE_CHANGED"):
            m.accept("cross", safe)
        with self.assertRaisesRegex(ValueError, "DEPENDENCY_CHANGED"):
            m.prepare(p)  # Current resampling cannot silently relabel old reasoning.

    def test_10_interpretation_change_blocks_certificate_without_world_change(self):
        m = ReferenceMachine({"balance": 5}, ("resource",))
        cert = m.prepare(m.propose("spend", {"balance": 4}, ("resource",)))
        m.control_change("INTERPRETATION", "I2")
        self.assertEqual(m.head, cert.validation_head)
        with self.assertRaisesRegex(ValueError, "SCOPE_CHANGED"):
            m.accept("spend", cert)

    def test_11_active_plan_guard_does_not_delete_the_fixed_old_plan(self):
        m = ReferenceMachine({"scene_ready": False}, ("ARTIFACT_SELECTION:active-plan",))
        old_ref, old_content = m.active_plan, m.plans[m.active_plan]
        cert = m.prepare(m.propose("scene", {"scene_ready": True},
                                   ("ARTIFACT_SELECTION:active-plan",)))
        m.control_change("ARTIFACT_SELECTION:active-plan", "plan-2")
        self.assertEqual(m.plans[old_ref], old_content)
        self.assertNotEqual(m.active_plan, old_ref)
        with self.assertRaisesRegex(ValueError, "SCOPE_CHANGED"):
            m.accept("scene", cert)

    def test_12_one_pin_batch_avoids_a_state_that_never_existed(self):
        def scenario(one_pin):
            m = ReferenceMachine({"left": 1, "right": 1}, ("pair",))
            answer = read_pair(m, one_pin,
                               lambda: m.advance({"left": 2, "right": 2}, ("pair",)))
            real_states = {(dict(state)["left"], dict(state)["right"])
                           for state in m.history.values()}
            return answer, real_states
        coherent, real = scenario(True)
        mixed, _ = scenario(False)
        self.assertIn(coherent, real)
        self.assertEqual(mixed, (1, 2))
        self.assertNotIn(mixed, real)

    def test_13_fixed_revision_cannot_wait_itself_into_future_coverage(self):
        m = ReferenceMachine(frontier=10)
        old = m.head
        self.assertEqual(m.state_coverage(old, 10), (old, "PENDING"))
        m.advance({}, (), frontier=20)
        self.assertEqual(m.state_coverage(old, 15), (old, "PENDING"))
        self.assertEqual(m.state_coverage(old, 15, "LATEST_AT_START"), (1, "EVALUATED"))
        with self.assertRaisesRegex(ValueError, "FIXED_CANNOT_WAIT"):
            m.state_coverage(old, 15, process="WAIT_EXISTING_ADVANCE")

    def test_14_revocation_is_checked_on_an_existing_cache_hit(self):
        m = ReferenceMachine()
        cache = {(0, "secret", "I1", "WORLD", 0): "the hidden thief is V"}
        key = next(iter(cache))
        self.assertEqual(cached_read(m, cache, key), cache[key])
        m.revoke()
        self.assertIn(key, cache)  # Deletion lag does not excuse disclosing the hit.
        with self.assertRaisesRegex(PermissionError, "CURRENT_AUTHORIZATION_REQUIRED"):
            cached_read(m, cache, key)

    def test_15_batch_enforces_profile_and_graph_capability_per_operation(self):
        maintainer = ({"WORLD"}, {"STATE", "TIMELINE"})
        reviewer = ({"WORLD"}, {"STATE", "TIMELINE", "NEIGHBORHOOD"})
        with self.assertRaisesRegex(PermissionError, "PROFILE_OPERATION_DENIED"):
            query_batch(maintainer, {"WORLD_READ", "VIEW_READ", "GRAPH_READ", "QUERY_BATCH"},
                        "WORLD", ("STATE", "NEIGHBORHOOD"))
        with self.assertRaisesRegex(PermissionError, "OPERATION_CAPABILITY_DENIED"):
            query_batch(reviewer, {"WORLD_READ", "VIEW_READ", "QUERY_BATCH"},
                        "WORLD", ("STATE", "NEIGHBORHOOD"))

    def test_16_actor_cannot_turn_a_batch_into_a_world_read(self):
        actor = ({"BELIEF"}, {"STATE", "TIMELINE"})
        overbroad_grant = {"WORLD_READ", "BELIEF_READ", "VIEW_READ", "QUERY_BATCH"}
        with self.assertRaisesRegex(PermissionError, "PLANE_DENIED"):
            query_batch(actor, overbroad_grant, "WORLD", ("STATE",))
        self.assertEqual(query_batch(actor, overbroad_grant, "BELIEF", ("STATE",)),
                         ("AUTHORIZED",))

    def test_17_retcon_preserves_old_day25_and_does_not_invent_day35_knowledge(self):
        old = ((1, "MAKE", "maker"), (10, "GIFT", "P"), (20, "LOAN", "F"),
               (30, "STEAL", "V"), (40, "DESTROY", None))
        revised = tuple((day, "GIFT", recipient) if day == 20 else (day, kind, recipient)
                        for day, kind, recipient in old)
        revisions = {"old": old, "revised": revised}
        self.assertEqual(replay_sword(revisions["old"], 25), ("P", "F", True))
        self.assertEqual(replay_sword(revisions["revised"], 25), ("F", "F", True))
        observations = ((20, "P", "holder", "F"),)  # No theft message before D35.
        belief = [value for day, actor, predicate, value in observations
                  if day <= 35 and actor == "P" and predicate == "holder"][-1]
        for events in revisions.values():
            self.assertEqual(replay_sword(events, 35)[1], "V")
            self.assertEqual(belief, "F")
        self.assertEqual(replay_sword(revisions["old"], 25), ("P", "F", True))


if __name__ == "__main__":
    unittest.main()
