"""Finite design probes, not the production kernel or a PostgreSQL substitute.
Run: python -m unittest discover -s experiments -p 'test_*.py'
"""
import copy
import hashlib
import itertools
import json
import unittest
from decimal import Decimal


def canonical(value):
    def check(v):
        if isinstance(v, bool) or v is None:
            return
        if isinstance(v, int):
            if abs(v) > 9007199254740991:
                raise ValueError('UNSAFE_INTEGER')
        elif isinstance(v, float):
            raise ValueError('FLOAT_NOT_ALLOWED')
        elif isinstance(v, str):
            v.encode('utf-8', 'strict')
        elif isinstance(v, list):
            for item in v:
                check(item)
        elif isinstance(v, dict):
            for key, item in v.items():
                if not isinstance(key, str):
                    raise ValueError('NONSTRING_KEY')
                check(key)
                check(item)
        else:
            raise ValueError('UNSUPPORTED_JSON_TYPE')
    check(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


class TinyLedger:
    """Copy-on-commit model. No locks, database, network, or durable storage."""
    def __init__(self, balance='10.0'):
        self.head = 0
        self.generation = 1
        self.state = {'balance': Decimal(balance), 'effects': set()}
        self.history = {0: copy.deepcopy(self.state)}
        self.receipts = {}
        self.scope_versions = {'bridge': 0}
        self.outbox = []
        self.releases = []

    def accept(self, key, payload, expected_head, generation, fail_before_commit=False):
        fingerprint = digest(payload)
        if key in self.receipts:
            old_hash, receipt = self.receipts[key]
            if old_hash != fingerprint:
                raise ValueError('IDEMPOTENCY_MISMATCH')
            return receipt
        if expected_head != self.head:
            raise ValueError('HEAD_CONFLICT')
        if generation != self.generation:
            raise ValueError('STALE_GENERATION')
        candidate = copy.deepcopy(self.state)
        for effect_key, amount in payload['effects']:
            if effect_key not in candidate['effects']:
                candidate['balance'] += Decimal(amount)
                candidate['effects'].add(effect_key)
        if candidate['balance'] < 0:
            raise ValueError('RESOURCE_CONFLICT')
        if fail_before_commit:
            raise RuntimeError('injected pre-commit interruption')
        self.head += 1
        self.state = candidate
        self.history[self.head] = copy.deepcopy(candidate)
        self.scope_versions['bridge'] += 1
        self.outbox.append(('invalidate', self.head))
        self.receipts[key] = (fingerprint, self.head)
        return self.head

    def fork(self):
        branch = TinyLedger()
        branch.state = copy.deepcopy(self.state)
        branch.history = {0: copy.deepcopy(self.state)}
        return branch

    def cancel(self):
        self.generation += 1

    def release(self, text_hash):
        self.releases.append((text_hash, self.head))


def sealed_bundle(arrivals, expected):
    received = {}
    for actor, action in arrivals:
        if actor not in expected or actor in received:
            raise ValueError('INVALID_PARTICIPANT_SET')
        received[actor] = action
    if set(received) != set(expected):
        return None
    return tuple(sorted(received.items()))


def can_advance(target, channel_lower_bounds, registry_complete):
    # Exclusive frontier may stop at an inbound lower bound; that point remains unresolved.
    return registry_complete and all(target <= bound for bound in channel_lower_bounds)


def compatible_refinement(coarse_trace, detailed_trace, required_points):
    # Fixture-defined observable signatures include resources AND cognition/messages.
    return all(coarse_trace[p] == detailed_trace[p] for p in required_points)


def select_versions(selection, immutable_versions, operations):
    result = dict(selection)
    for record, expected, chosen in operations:
        if result.get(record) != expected or chosen not in immutable_versions:
            raise ValueError('VERSION_CONFLICT')
        result[record] = chosen
    return result


def actor_unknown(_world_secret):
    # A fixture for output noninterference, not a universal authorization proof.
    return {'truth_status': 'UNDETERMINED', 'knowledge_status': 'UNKNOWN',
            'evaluation_status': 'NOT_APPLICABLE', 'freshness': 'NOT_USED',
            'coverage': 'UNDECLARED', 'value_status': 'UNRESOLVED',
            'data': None, 'evidence_refs': []}


class MechanismProbes(unittest.TestCase):
    def test_receipt_loss_retries_same_effect_once(self):
        k = TinyLedger()
        p = {'effects': [['transport', '-2.5']]}
        r = k.accept('command', p, 0, 1)
        for _ in range(20):
            self.assertEqual(k.accept('command', p, 0, 1), r)
        self.assertEqual((k.head, k.state['balance'], len(k.outbox)), (1, Decimal('7.5'), 1))

    def test_same_key_changed_payload_is_rejected(self):
        k = TinyLedger()
        k.accept('command', {'effects': [['a', '-1']]}, 0, 1)
        with self.assertRaisesRegex(ValueError, 'IDEMPOTENCY_MISMATCH'):
            k.accept('command', {'effects': [['a', '-2']]}, 0, 1)

    def test_precommit_failure_does_not_advance_any_component(self):
        k = TinyLedger()
        with self.assertRaises(RuntimeError):
            k.accept('x', {'effects': [['a', '-1']]}, 0, 1, True)
        self.assertEqual((k.head, k.state['balance'], k.outbox, k.receipts), (0, Decimal('10'), [], {}))
        k.accept('x', {'effects': [['a', '-1']]}, 0, 1)
        self.assertEqual(k.state['balance'], Decimal('9'))

    def test_resource_violation_is_not_partial_commit(self):
        k = TinyLedger()
        with self.assertRaisesRegex(ValueError, 'RESOURCE_CONFLICT'):
            k.accept('x', {'effects': [['a', '-3'], ['b', '-20']]}, 0, 1)
        self.assertEqual(k.state, {'balance': Decimal('10'), 'effects': set()})

    def test_late_generation_cannot_make_new_commit(self):
        k = TinyLedger()
        k.cancel()
        with self.assertRaisesRegex(ValueError, 'STALE_GENERATION'):
            k.accept('x', {'effects': [['a', '-1']]}, 0, 1)
        self.assertEqual(k.head, 0)

    def test_prior_success_receipt_survives_later_cancellation(self):
        k = TinyLedger()
        p = {'effects': [['a', '-1']]}
        r = k.accept('x', p, 0, 1)
        k.cancel()
        self.assertEqual(k.accept('x', p, 0, 1), r)
        self.assertEqual(k.head, 1)

    def test_two_commands_pinned_to_same_head_need_revalidation(self):
        k = TinyLedger()
        k.accept('a', {'effects': [['a', '-1']]}, 0, 1)
        with self.assertRaisesRegex(ValueError, 'HEAD_CONFLICT'):
            k.accept('b', {'effects': [['b', '-1']]}, 0, 1)

    def test_fork_inherits_effect_coverage_without_reapplying(self):
        k = TinyLedger()
        k.accept('a', {'effects': [['a', '-2']]}, 0, 1)
        branch = k.fork()
        branch.accept('b', {'effects': [['a', '-2'], ['b', '-1']]}, 0, 1)
        self.assertEqual(branch.state['balance'], Decimal('7'))
        self.assertEqual(k.state['balance'], Decimal('8'))
        self.assertEqual(k.history[0]['balance'], Decimal('10'))

    def test_arrival_permutations_produce_same_sealed_bundle(self):
        intents = [('A', 'request'), ('B', 'block'), ('C', 'observe')]
        bundles = {sealed_bundle(order, {'A', 'B', 'C'}) for order in itertools.permutations(intents)}
        self.assertEqual(len(bundles), 1)
        # Negative control: first-response winner changes with arrival order.
        naive_winners = {order[0][0] for order in itertools.permutations(intents)}
        self.assertEqual(len(naive_winners), 3)
        self.assertIsNone(sealed_bundle(intents[:2], {'A', 'B', 'C'}))

    def test_unregistered_channel_and_exclusive_frontier_boundary(self):
        self.assertTrue(can_advance((30, 0), [(31, 0)], True))
        self.assertTrue(can_advance((31, 0), [(31, 0)], True))
        self.assertFalse(can_advance((31, 1), [(31, 0)], True))
        self.assertFalse(can_advance((30, 0), [(31, 0)], False))
        self.assertFalse(can_advance((30, 0), [(31, 0), (29, 1)], True))

    def test_same_final_balance_does_not_prove_refinement(self):
        coarse = {1: (10, frozenset()), 2: (8, frozenset({'A knows'}))}
        detail = {1: (10, frozenset({'B knows'})), 2: (8, frozenset({'A knows'}))}
        self.assertEqual(coarse[2], detail[2])
        self.assertFalse(compatible_refinement(coarse, detail, [1, 2]))

    def test_release_and_flashback_do_not_apply_world_effects(self):
        k = TinyLedger()
        k.accept('a', {'effects': [['a', '-2']]}, 0, 1)
        old = copy.deepcopy(k.state)
        for text in ['first account', 'flashback', 'edited release']:
            k.release(digest(text))
        self.assertEqual(k.state, old)
        self.assertEqual(k.head, 1)

    def test_negative_query_dependency_is_invalidated(self):
        k = TinyLedger()
        empty_result, observed_scope_version = [], k.scope_versions['bridge']
        k.accept('a', {'effects': [['new action', '-1']]}, 0, 1)
        self.assertEqual(empty_result, [])
        self.assertNotEqual(k.scope_versions['bridge'], observed_scope_version)
        self.assertEqual(k.outbox, [('invalidate', 1)])

    def test_retcon_selects_new_history_preserving_old_speech(self):
        versions = {'loan-v1': {'owner': 'master'}, 'gift-v2': {'owner': 'hero'},
                    'speech-v1': {'utterance': 'I will give the sword'}}
        old = {'D12': 'loan-v1', 'D30': 'speech-v1'}
        repaired = select_versions(old, versions, [('D12', 'loan-v1', 'gift-v2')])
        self.assertEqual(versions[old['D12']]['owner'], 'master')
        self.assertEqual(versions[repaired['D12']]['owner'], 'hero')
        self.assertEqual(repaired['D30'], old['D30'])
        self.assertNotEqual(digest(old), digest(repaired))
        with self.assertRaisesRegex(ValueError, 'VERSION_CONFLICT'):
            select_versions(old, versions, [('D12', 'missing-v0', 'gift-v2')])

    def test_hidden_and_absent_have_same_actor_visible_output(self):
        self.assertEqual(actor_unknown({'secret_owner': 'villain'}), actor_unknown(None))

    def test_bounded_occurrence_does_not_invent_exact_time(self):
        allowed_times = range(22, 26)
        def answers(day):
            return {'new owner' if day >= t else 'old owner' for t in allowed_times}
        self.assertEqual(answers(21), {'old owner'})
        self.assertEqual(answers(26), {'new owner'})
        self.assertEqual(len(answers(23)), 2)  # Correct answer here is undetermined.

    def test_unicode_spans_preserve_crlf_and_combining_marks(self):
        text = '剑🦊\r\ne\u0301'
        self.assertEqual(len(text), 6)
        span = text[1:4]
        self.assertEqual(span, '🦊\r\n')
        self.assertNotEqual(hashlib.sha256(span.encode()).hexdigest(), hashlib.sha256('🦊\n'.encode()).hexdigest())
        self.assertEqual(len('e\u0301'), 2)

    def test_canonicalization_keeps_decimal_strings_and_key_order(self):
        self.assertEqual(digest({'b': 2, 'a': '剑'}), digest({'a': '剑', 'b': 2}))
        for invalid in [float('nan'), 0.1, 9007199254740992]:
            with self.assertRaises(ValueError):
                canonical(invalid)


if __name__ == '__main__':
    unittest.main(verbosity=2)
