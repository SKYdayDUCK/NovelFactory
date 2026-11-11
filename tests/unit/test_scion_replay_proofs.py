from collections import Counter
from copy import deepcopy

import pytest
from test_domain import artifact, genesis, point, sword_history

from novel_factory.application.replay_proofs import ReplayProofs
from novel_factory.domain.canonical import fingerprint
from novel_factory.domain.errors import DomainError
from novel_factory.domain.world import INTERPRETER_VERSION, replay


class Counts(Counter):
    def record(self, name, outcome):
        self[name, outcome] += 1


def _parent(snapshot, history):
    return {"work_id": "work", "branch_id": "branch", "provenance": {"history_epoch": 0},
            "state_hash": fingerprint(snapshot), "snapshot": snapshot, "interpretation_ref": artifact("interpreter"),
            "selection": {e["occurrence_id"]: e["event_version_id"] for e in history}}


def _world():
    raw = genesis()
    events = sword_history()
    for event in events:
        event["engine_version"] = INTERPRETER_VERSION
    work = {"interpretation_ref": artifact("interpreter"), "rules_ref": artifact("rules"), "interpretation_epoch": 0}
    return raw, events, work


def test_ordered_projection_and_mutable_snapshots_cannot_change_proof():
    raw, history, work = _world()
    cache, tokens = ReplayProofs(), Counts()
    base = _parent(replay(raw, []), [])
    for end in range(1, len(history) + 1):
        prefix = list(reversed(history[:end - 1]))
        candidate = history[end - 1]
        trial = cache.evaluate(tokens, raw, prefix + [candidate], [candidate], "append", base, work)
        assert trial == replay(raw, prefix + [candidate])
        base = _parent(deepcopy(trial), history[:end])
        trial["events"].clear()
    assert tokens["replay", "hit"] == 4
    assert tokens["replay", "unverified_parent"] == 1


@pytest.mark.parametrize("case,reason", [
    ("tampered_event", "unverified_parent"), ("tampered_projection", "unverified_parent"),
    ("changed_epoch", "unverified_parent"), ("same_time", "not_strictly_later"),
    ("out_of_order", "not_strictly_later"), ("duplicate_group", "historical_identity_overlap"),
    ("causal_parent", "complex_causality_or_time"), ("upgrade", "interpretation_or_rules"),
    ("retcon", "historical_replacement"), ("invalid_event", "unrecognized_input"),
    ("genesis_entity_order", "unverified_parent"),
])
def test_unproved_prefixes_use_original_result_or_exact_error(case, reason):
    raw, history, work = _world()
    first, second = history[:2]
    cache, tokens = ReplayProofs(), Counts()
    snapshot = cache.evaluate(tokens, raw, [first], [first], "append", _parent(replay(raw, []), []), work)
    base = _parent(snapshot, [first])
    template = "append"
    if case == "tampered_event":
        first["participants"].reverse()
    elif case == "tampered_projection":
        base["snapshot"]["events"].clear()
        base["state_hash"] = fingerprint(base["snapshot"])
    elif case == "changed_epoch":
        work["interpretation_epoch"] += 1
    elif case == "same_time":
        second["occurrence_time"]["at"] = first["occurrence_time"]["at"]
    elif case == "out_of_order":
        second["occurrence_time"]["at"] = point(0)
    elif case == "duplicate_group":
        second["effect_group_id"] = first["effect_group_id"]
    elif case == "causal_parent":
        second["causal_parents"] = [{"occurrence_id": first["occurrence_id"], "event_version_id": first["event_version_id"]}]
    elif case == "upgrade":
        second["engine_version"] = "new-version"
    elif case == "retcon":
        template = "REPAIR_EVENT"
    elif case == "invalid_event":
        del first["effects"]
    elif case == "genesis_entity_order":
        raw["entities"] = dict(reversed(raw["entities"].items()))
    selected = [first, second]
    tokens.clear()
    try:
        expected = replay(raw, selected)
    except DomainError as original:
        with pytest.raises(DomainError) as trial:
            cache.evaluate(tokens, raw, selected, [second], template, base, work)
        assert (trial.value.code, trial.value.safe_message) == (original.code, original.safe_message)
    else:
        assert cache.evaluate(tokens, raw, selected, [second], template, base, work) == expected
    assert tokens == {("replay", reason): 1}
