"""Finite process coverage and simultaneous effects; no scheduler wall clock.

The registry is a versioned record selection stored with each WorldRef. A due
trigger is a question to evaluate, never an already completed delivery. Returned
actor intents are sorted for reproducible input bytes, not to choose a winner.
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
from uuid import UUID, uuid5

from novel_factory.contracts.registry import validate_contract
from novel_factory.domain.canonical import fingerprint
from novel_factory.domain.errors import DomainError
from novel_factory.domain.temporal import point_key

SIMULATION_TEMPLATES = frozenset({"REGISTER_PROCESS", "SCHEDULE_ADVANCE", "JOINT_INTERACTION"})
TERMINAL = frozenset({"COMPLETED", "TERMINATED"})


def require(value, code, message):
    if not value:
        raise DomainError(code, message)


def after(at):
    return ({"tick": at["tick"], "phase": at["phase"] + 1}
            if at["phase"] < 2147483647 else {"tick": at["tick"] + 1, "phase": 0})


def empty_state():
    return {"schema_version": "2.2", "processes": {}, "closed_interactions": {}}


def state_from_revision(revision):
    provenance = revision.get("provenance", {})
    state = provenance.get("simulation_state")
    if state is None:
        require("simulation_sha256" not in provenance, "PROJECTION_CORRUPT", "Schedule state is absent")
        return empty_state()
    validate_contract("SimulationState", state, "simulation")
    require(fingerprint(state) == provenance.get("simulation_sha256"),
            "PROJECTION_CORRUPT", "Schedule integrity failed")
    require(all(pid == entry["definition"]["process_id"] for pid, entry in state["processes"].items()),
            "PROJECTION_CORRUPT", "Process selection identity differs from its payload")
    return deepcopy(state)


def registry_fingerprint(state):
    # This includes evaluated triggers and their next windows: completion and
    # new registrations must both invalidate a prepared interaction closure.
    return fingerprint(state)


def validate_registration(process, snapshot, frontiers, state, at):
    validate_contract("ProcessRegistration", process, "simulation")
    require(process["start_at"] == at, "INVALID_ARGUMENT", "Registration time differs from start")
    require(process["world_stage"] in {"ACTIVE", "NOT_STARTED"},
            "INVALID_ARGUMENT", "A registration cannot assert an unevaluated completion or suspension")
    pid = process["process_id"]
    require(pid not in state["processes"], "ALREADY_EXISTS", "Process identity is already selected")
    if process["evaluator_kind"] == "DETERMINISTIC":
        require(process["due_at"] is not None and not process["required_actor_ids"],
                "INVALID_ARGUMENT", "A deterministic recipe needs an exact trigger and no semantic actor slots")
        require(all(command["args"].get("at") == process["due_at"]
                    for command in process["deterministic_commands"]),
                "TIME_AMBIGUOUS", "Recipe commands must use the registered due phase")
    scope_ids = set(process["scope_ids"])
    participants = set(process["participant_ids"])
    require(set(process["required_actor_ids"]) <= participants,
            "INVALID_ARGUMENT", "Required actors must participate in the process")
    for identifier in scope_ids | participants:
        require(identifier in snapshot["entities"], "HARD_RULE_VIOLATION", "Process uses an unregistered entity")
        require(point_key(snapshot["entities"][identifier]["created_at"]) <= point_key(at),
                "HARD_RULE_VIOLATION", "Process precedes an entity")
    if process["due_at"] is not None:
        require(point_key(process["due_at"]) >= point_key(at),
                "TIME_AMBIGUOUS", "A process cannot be due before it starts")
    require(all(point_key(at) >= point_key(frontiers.get(scope, {"tick": 0, "phase": 0}))
                for scope in scope_ids), "HISTORY_REPAIR_REQUIRED", "Process starts in closed history")
    for channel in process["channels"]:
        require(channel["from_scope_id"] in scope_ids and channel["to_scope_id"] in snapshot["entities"],
                "INVALID_ARGUMENT", "Channel endpoints must be registered scopes")
        bound, delay = channel["earliest_possible_impact"], channel["min_delay_ticks"]
        if delay is None:
            require(bound is None, "INVALID_ARGUMENT", "Unknown propagation delay cannot prove a bound")
        elif bound is not None:
            lower = {"tick": at["tick"] + delay, "phase": at["phase"] if delay == 0 else 0}
            require(point_key(bound) >= point_key(lower), "HARD_RULE_VIOLATION", "Channel violates minimum delay")
        if process["process_kind"] == "MESSAGE" and process["due_at"] is not None and bound is not None:
            require(point_key(process["due_at"]) >= point_key(bound),
                    "HARD_RULE_VIOLATION", "Message cannot be evaluated as arriving before its channel bound")
        if channel["completeness"] == "COMPLETE":
            require(bound is not None and channel["evidence_refs"],
                    "UNPROVEN_FOOTPRINT", "A complete channel requires a justified bound")
        if bound is not None:
            require(point_key(bound) >= point_key(frontiers.get(channel["to_scope_id"], {"tick": 0, "phase": 0})),
                    "HISTORY_REPAIR_REQUIRED", "New inbound impact reaches closed history")


def _dormant(entry):
    return (entry["world_stage"] in TERMINAL or
            (entry["world_stage"] == "SUSPENDED" and entry.get("evaluation_status") == "EVALUATED"
             and entry["next_due"] is None))


def coverage_blockers(state, scope_ids, target, *, included_process_ids=()):
    """Return all declared unresolved influences strictly before target H.

    An inbound bound exactly equal to H permits the half-open prefix ending H,
    but not H's interaction. Unproven/unknown channels never provide lookahead.
    """
    scopes = set(scope_ids)
    included = set(included_process_ids)
    blockers = []
    for pid, entry in sorted(state["processes"].items()):
        if pid in included or _dormant(entry):
            continue
        process = entry["definition"]
        relevant = bool(scopes.intersection(process["scope_ids"])) or pid in scopes
        due = entry["next_due"]
        if relevant and process["high_impact"] and (due is None or point_key(due) < point_key(target)):
            blockers.append({"process_id": pid, "reason": "UNRESOLVED_TRIGGER"})
        for channel in process["channels"]:
            if channel["to_scope_id"] not in scopes:
                continue
            lower = channel["earliest_possible_impact"]
            if channel["completeness"] != "COMPLETE" or lower is None:
                blockers.append({"process_id": pid, "reason": "UNPROVEN_INBOUND"})
            elif point_key(lower) < point_key(target):
                blockers.append({"process_id": pid, "reason": "INBOUND_BOUNDARY"})
    return blockers


def closure_at(state, requested_scopes, at):
    """Expand the registered coupling closure at an exact interaction point."""
    scopes = set(requested_scopes)
    process_ids = set()
    changed = True
    while changed:
        changed = False
        for pid, entry in sorted(state["processes"].items()):
            if _dormant(entry) or pid in process_ids:
                continue
            process = entry["definition"]
            due = entry["next_due"]
            directly_due = bool(scopes.intersection(set(process["scope_ids"]) | set(process.get("participant_ids", [])))) and (
                due is None or point_key(due) <= point_key(at))
            inbound_due = False
            for channel in process["channels"]:
                if channel["to_scope_id"] not in scopes:
                    continue
                bound = channel["earliest_possible_impact"]
                require(channel["completeness"] == "COMPLETE" and bound is not None,
                        "ADVANCE_BLOCKED", "Interaction has an unproven inbound influence")
                inbound_due |= point_key(bound) <= point_key(at)
            if not directly_due and not inbound_due:
                continue
            require(due is not None, "TIME_AMBIGUOUS", "Required process needs a resolved interaction time")
            # A missing earlier interaction must be evaluated first. Creating a
            # later joint group is not permission to silently skip its trigger.
            require(point_key(due) == point_key(at),
                    "ADVANCE_BLOCKED", "An earlier or differently timed trigger must resolve first")
            process_ids.add(pid)
            scopes.update(process["scope_ids"])
            scopes.update(process.get("participant_ids", []))
            changed = True
    require(process_ids, "INVALID_ARGUMENT", "No registered process is due in this interaction")
    actors = sorted({actor for pid in process_ids
                     for actor in state["processes"][pid]["definition"]["required_actor_ids"]})
    return {"scope_ids": sorted(scopes), "process_ids": sorted(process_ids), "actor_ids": actors}


def joint_effects(commands, snapshot, proposal_id, at, build_command):
    """Evaluate every chosen atomic command against the same input state.

    The selected result is explicit and audited. No priority is inferred from
    arrival order, UUID ordering, or array position. A shared resource cannot be
    funded by another simultaneous receipt without a declared later phase.
    """
    from novel_factory.domain.world import resource_at

    effects, reads, writes, bumps, scopes = [], set(), set(), set(), set()
    total_debits = {}
    relation_targets = set()
    unique_items = set()
    for i, command in enumerate(commands):
        validate_contract("AtomicCommand", command, "simulation")
        require(command["args"].get("at") == at, "TIME_AMBIGUOUS", "Joint actions must share an exact phase")
        inner_id = str(uuid5(UUID(proposal_id), "atomic:" + fingerprint(command)))
        inner = build_command(command["template_id"], command["args"], snapshot, inner_id)
        for effect in inner["effects"]:
            effect = deepcopy(effect)
            effect["effect_key"] = f"joint:{i}:" + effect["effect_key"]
            if effect["kind"] == "RESOURCE_DELTA":
                amount = Decimal(effect["quantity"])
                if amount < 0:
                    key = (effect["account_id"], effect["resource_id"], effect["unit"])
                    total_debits[key] = total_debits.get(key, Decimal(0)) - amount
            if effect["kind"] in {"RELATION_SET", "RELATION_TRANSITION"}:
                key = effect["subject_id"], effect["predicate"]
                # OPEN+CLOSE from one command is legitimate. Two commands
                # selecting the same exclusive relation are not.
                if effect["kind"] == "RELATION_SET" or effect["operation"] == "OPEN":
                    require(key not in relation_targets, "RESOURCE_CONFLICT", "Joint result assigns one relation twice")
                    relation_targets.add(key)
            effects.append(effect)
        if command["template_id"] in {"TRANSFER_ITEM", "DESTROY_ITEM"}:
            item = command["args"]["item_id"]
            require(item not in unique_items, "RESOURCE_CONFLICT", "Unique item has multiple joint outcomes")
            unique_items.add(item)
        reads.update(inner["reads"])
        writes.update(inner["writes"])
        bumps.update(inner["bumps"])
        scopes.update(inner["process_scopes"])
    for (account, resource, unit), amount in total_debits.items():
        balance = resource_at(snapshot, account, resource, unit, at)
        require(amount <= Decimal(balance["quantity"]),
                "RESOURCE_CONFLICT", "Joint resource allocations exceed the common starting balance")
    return {"effects": effects, "reads": reads, "writes": writes, "bumps": bumps, "scopes": scopes}


def apply_schedule_change(state, template_id, args, event_refs):
    result = deepcopy(state)

    def register(process, record_ref, at):
        result["processes"][process["process_id"]] = {
            "definition": deepcopy(process), "record_ref": deepcopy(record_ref),
            "registered_at": deepcopy(at), "next_due": deepcopy(process["due_at"]),
            "world_stage": process["world_stage"], "evaluation_status": "PENDING",
            "evaluated_frontier": deepcopy(at), "trigger_version_id": process["trigger_version_id"],
            "evaluations": [],
        }

    if template_id == "REGISTER_PROCESS":
        register(args["process"], args["record_ref"], args["at"])
    elif template_id == "JOINT_INTERACTION":
        interaction_id = args["interaction_id"]
        require(interaction_id not in result["closed_interactions"], "ALREADY_ACCEPTED", "Interaction is already closed")
        for update in args["process_outcomes"]:
            entry = result["processes"][update["process_id"]]
            entry.update(next_due=deepcopy(update["next_due"]), world_stage=update["world_stage"],
                         evaluation_status="EVALUATED", evaluated_frontier=after(args["at"]),
                         trigger_version_id=str(uuid5(UUID(interaction_id), "next:" + update["process_id"])))
            entry["evaluations"].append({"interaction_id": interaction_id, "outcome": update["outcome"],
                                         "at": deepcopy(args["at"]), "event_refs": deepcopy(event_refs)})
        for outgoing in args["outgoing_processes"]:
            register(outgoing["process"], outgoing["record_ref"], args["at"])
        result["closed_interactions"][interaction_id] = {
            "outcome_ref": deepcopy(args["outcome_ref"]), "at": deepcopy(args["at"]),
            "event_refs": deepcopy(event_refs),
        }
    validate_contract("SimulationState", result, "simulation")
    return result


def deterministic_result(state, process_ids, snapshot, at, build_command):
    """Evaluate registered recipes without treating due plans as success.

    Failed physical preconditions produce an explicit false trigger. Joint
    allocations are subsequently checked together; competing successful recipes
    are never resolved by process order or by silently dropping one side.
    """
    commands, outcomes = [], []
    for pid in sorted(process_ids):
        entry = state["processes"][pid]
        definition = entry["definition"]
        if definition["evaluator_kind"] != "DETERMINISTIC":
            continue
        recipe = definition["deterministic_commands"]
        require(entry["next_due"] == at, "TIME_AMBIGUOUS", "Recipe is not due at the interaction phase")
        try:
            effects = joint_effects(recipe, snapshot, pid, at, build_command)
            require(effects["scopes"] <= set(definition["scope_ids"]),
                    "UNPROVEN_FOOTPRINT", "Recipe writes outside its registered scopes")
        except DomainError as error:
            if error.code not in {"RESOURCE_CONFLICT", "HARD_RULE_VIOLATION"}:
                raise
            outcomes.append({"process_id": pid, "outcome": "CONDITION_FALSE", "world_stage": "SUSPENDED", "next_due": None})
        else:
            commands.extend(deepcopy(recipe))
            outcomes.append({"process_id": pid, "outcome": "FIRED", "world_stage": "COMPLETED", "next_due": None})
    return sorted(commands, key=fingerprint), outcomes
