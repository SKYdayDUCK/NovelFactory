from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Event

import pytest

from novel_factory.application import replay_proofs
from novel_factory.application.replay_proofs import ReplayProofs
from novel_factory.domain.canonical import fingerprint
from novel_factory.domain.errors import DomainError
from novel_factory.domain.world import replay
from test_scion_replay_proofs import Counts, _parent, _world


def request():
    genesis, history, work = _world()
    prefix, event = history[:3], history[3]
    parent = _parent(replay(genesis, prefix), prefix)
    options = Counts()
    options.replay_lazy = options.replay_prime = True
    # Stored JSONB selection may have a different order than the order
    # originally used to generate parent.snapshot.events.
    sequence = [prefix[1], prefix[0], prefix[2], event]
    return options, genesis, sequence, [event], "append", parent, work


def test_paid_parent_replay_then_true_reuse_have_distinct_costs(monkeypatch):
    args = request()
    options, genesis, events = args[:3]
    expected = replay(genesis, events)
    calls = []
    original = replay_proofs.reducer.replay

    def observe(raw, selected):
        calls.append(len(selected))
        return original(raw, selected)

    monkeypatch.setattr(replay_proofs.reducer, "replay", observe)
    cache = ReplayProofs(capacity=1)
    for _ in range(3):
        assert cache.evaluate(*args, proposal=True) == expected
    assert calls == [len(events), len(events) - 1]
    assert options["replay_proof", "prime_first"] == options["replay_proof", "prime_attempt"] == 1
    assert options["replay_proof", "primed_hit"] == options["replay_proof", "reuse_hit"] == 1
    assert ReplayProofs(capacity=1).evaluate(*args, proposal=True) == expected
    assert len(calls) == 3 and calls[-1] == len(events)


@pytest.mark.parametrize("damage", ["snapshot", "body", "summary", "invalid_prefix"])
def test_failed_parent_proof_returns_exact_full_result_or_error(damage):
    options, raw, events, candidates, template, parent, work = request()
    if damage == "snapshot":
        parent["snapshot"]["resources"].clear()
    elif damage == "body":
        events[1]["effects"][0]["object_id"] = events[0]["effects"][0]["object_id"]
    elif damage == "summary":
        parent["snapshot"]["events"][1] = deepcopy(parent["snapshot"]["events"][0])
    else:
        del events[0]["participants"]
    parent["state_hash"] = fingerprint(parent["snapshot"])
    cache = ReplayProofs()
    for _ in range(3):
        try:
            original = replay(raw, events)
        except DomainError as expected:
            with pytest.raises(DomainError) as failure:
                cache.evaluate(options, raw, events, candidates, template, parent, work, proposal=True)
            assert (expected.code, expected.safe_message) == (failure.value.code, failure.value.safe_message)
        else:
            assert cache.evaluate(options, raw, events, candidates, template, parent, work, proposal=True) == original
    assert options["replay", "hit"] == 0
    assert options["replay_proof", "prime_attempt"] == 1
    assert options["replay_proof", "prime_failed"] + options["replay_proof", "prime_mismatch"] == 1


def test_simultaneous_miss_pays_at_most_one_prefix_replay_and_does_not_block_other_proposals(monkeypatch):
    args = request()
    expected = replay(args[1], args[2])
    cache = ReplayProofs(capacity=1)
    assert cache.evaluate(*args, proposal=True) == expected
    started, release = Event(), Event()
    original = replay_proofs.reducer.replay

    def pause(raw, selected):
        if len(selected) == len(args[2])-1:
            started.set()
            assert release.wait(5)
        return original(raw, selected)

    monkeypatch.setattr(replay_proofs.reducer, "replay", pause)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(cache.evaluate, *args, proposal=True)
        assert started.wait(5)
        try:
            second = pool.submit(cache.evaluate, *args, proposal=True)
            assert second.result(timeout=5) == expected
        finally:
            release.set()
        assert first.result(timeout=5) == expected
    assert args[0]["replay_proof", "prime_attempt"] == 1
    assert args[0]["replay_proof", "prime_busy"] == 1
    assert args[0]["replay_proof", "primed_hit"] == 1


def test_hint_eviction_cannot_admit_duplicate_or_unbounded_inflight_parent_work():
    cache = ReplayProofs(capacity=2)
    for key in ("a", "b"):
        assert cache._reserve_prime(key) == "first"
        assert cache._reserve_prime(key) == "attempt"
    for key in ("c", "d", "e", "f"):
        assert cache._reserve_prime(key) == "first"
        assert cache._reserve_prime(key) == "busy"
        assert len(cache._prime_hints) == 2 and len(cache._priming) == 2
    assert cache._reserve_prime("a") == cache._reserve_prime("b") == "busy"


def test_prime_releases_reservation_if_proof_stack_raises_unexpected_error(monkeypatch):
    args = request()
    cache = ReplayProofs()
    cache.evaluate(*args, proposal=True)
    original = replay_proofs.reducer.replay
    def fault(raw, rows):
        if len(rows) == len(args[2]) - 1:
            raise RuntimeError("simulated infrastructure interruption")
        return original(raw, rows)
    monkeypatch.setattr(replay_proofs.reducer, "replay", fault)
    assert cache.evaluate(*args, proposal=True) == replay(args[1], args[2])
    assert args[0]["replay_proof", "prime_failed"] == 1
    assert not cache._priming
    assert cache.evaluate(*args, proposal=True) == replay(args[1], args[2])
