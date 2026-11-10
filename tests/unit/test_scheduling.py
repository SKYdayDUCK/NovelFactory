"""P2 S01/S02/S03/S05 pure temporal/whole-bundle counterexamples."""
from itertools import permutations
from uuid import uuid4

import pytest

from novel_factory.application.templates import build
from novel_factory.domain.errors import DomainError
from novel_factory.domain.scheduling import (
    closure_at,
    coverage_blockers,
    empty_state,
    joint_effects,
)
from novel_factory.domain.world import replay
from novel_factory.fixtures import fixture_definition, point

pytestmark = pytest.mark.unit


def entry(pid, scopes, due, *, actor=None, channel=None):
    return {"definition": {"process_id": pid, "scope_ids": scopes, "high_impact": True,
                           "required_actor_ids": [actor] if actor else [],
                           "channels": [channel] if channel else []},
            "next_due": due, "world_stage": "ACTIVE"}


def test_inbound_lower_bound_is_exclusive_and_not_delivery():
    state = empty_state()
    state["processes"]["message"] = entry("message", ["south"], point(31), channel={
        "channel_id": "road", "to_scope_id": "north", "completeness": "COMPLETE",
        "earliest_possible_impact": point(31)})
    assert coverage_blockers(state, ["north"], point(31)) == []
    assert coverage_blockers(state, ["north"], point(31, 1)) == [
        {"process_id": "message", "reason": "INBOUND_BOUNDARY"}]
    assert state["processes"]["message"]["world_stage"] == "ACTIVE"


def test_undeclared_channel_blocks_related_scope_and_not_remote():
    state = empty_state()
    state["processes"]["hidden"] = entry("hidden", ["south"], None, channel={
        "channel_id": "unknown", "to_scope_id": "north", "completeness": "UNDECLARED",
        "earliest_possible_impact": None})
    assert coverage_blockers(state, ["north"], point(10))
    assert coverage_blockers(state, ["remote"], point(100)) == []
    with pytest.raises(DomainError, match="unproven"):
        closure_at(state, ["north"], point(30))


def test_joint_closure_collects_all_same_time_processes_without_remote_barrier():
    state = empty_state()
    state["processes"] = {"east": entry("east", ["east", "bridge"], point(42), actor="a"),
                           "west": entry("west", ["west", "bridge"], point(42), actor="b"),
                           "remote": entry("remote", ["remote"], point(45), actor="c", channel={
                               "channel_id": "remote-road", "to_scope_id": "bridge",
                               "completeness": "COMPLETE", "earliest_possible_impact": point(45)})}
    result = closure_at(state, ["east"], point(42))
    assert result == {"scope_ids": ["bridge", "east", "west"], "process_ids": ["east", "west"], "actor_ids": ["a", "b"]}
    assert coverage_blockers(state, result["scope_ids"], point(42, 1), included_process_ids=result["process_ids"]) == []
    state["processes"]["west"]["next_due"] = point(41)
    with pytest.raises(DomainError) as error:
        closure_at(state, ["east"], point(42))
    assert error.value.code == "ADVANCE_BLOCKED"


def snapshot():
    fixture = fixture_definition("white_bridge")
    # Unit tests use the already-normalized minimal resources and entities;
    # relation provenance is supplied only by the real bootstrap service.
    return fixture, replay({"entities": fixture["genesis"]["entities"],
                             "resources": fixture["genesis"]["resources"]}, [])


def transfer(ids, dst, amount):
    return {"template_id": "RESOURCE_TRANSFER", "args": {"from_account": ids["warehouse"],
        "to_account": ids[dst], "resource_id": ids["grain"], "quantity": amount, "unit": "bag", "at": point(30)}}


def test_joint_resource_allocations_use_whole_common_starting_balance():
    fixture, state = snapshot()
    with pytest.raises(DomainError) as error:
        joint_effects([transfer(fixture["ids"], "relief", "60"), transfer(fixture["ids"], "recipient", "60")],
                      state, str(uuid4()), point(30), build)
    assert error.value.code == "RESOURCE_CONFLICT"


def test_atomic_incoming_credit_does_not_fund_simultaneous_outgoing_transfer():
    fixture, state = snapshot()
    ids = fixture["ids"]
    inbound = transfer(ids, "relief", "40")
    outbound = {"template_id": "RESOURCE_TRANSFER", "args": {"from_account": ids["relief"],
        "to_account": ids["recipient"], "resource_id": ids["grain"], "quantity": "10", "unit": "bag", "at": point(30)}}
    for commands in permutations([inbound, outbound]):
        with pytest.raises(DomainError) as error:
            joint_effects(commands, state, str(uuid4()), point(30), build)
        assert error.value.code == "RESOURCE_CONFLICT"


def test_joint_input_order_does_not_choose_winner_or_change_totals():
    fixture, state = snapshot()
    commands = [transfer(fixture["ids"], "relief", "30"), transfer(fixture["ids"], "recipient", "20")]
    totals = []
    for sequence in permutations(commands):
        result = joint_effects(sequence, state, str(uuid4()), point(30), build)
        totals.append(sorted((e["account_id"], e["quantity"]) for e in result["effects"]))
    assert totals[0] == totals[1]


def test_joint_actions_cannot_invent_phase_from_array_order():
    fixture, state = snapshot()
    command = transfer(fixture["ids"], "relief", "30")
    command["args"]["at"] = point(30, 1)
    with pytest.raises(DomainError) as error:
        joint_effects([command], state, str(uuid4()), point(30), build)
    assert error.value.code == "TIME_AMBIGUOUS"


def test_same_actor_couples_simultaneous_processes_even_with_disjoint_resources():
    state = empty_state()
    east = entry("east", ["stock-east"], point(42), actor="a")
    west = entry("west", ["stock-west"], point(42), actor="a")
    east["definition"]["participant_ids"] = ["a"]
    west["definition"]["participant_ids"] = ["a"]
    state["processes"] = {"east": east, "west": west}
    closure = closure_at(state, ["stock-east"], point(42))
    assert closure["process_ids"] == ["east", "west"]
    assert closure["scope_ids"] == ["a", "stock-east", "stock-west"]
    assert closure["actor_ids"] == ["a"]
