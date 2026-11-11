"""P2 process registration, sealed actor inputs and explicit joint decisions.

All methods save candidates. Only Kernel.accept establishes process registration,
trigger outcomes, world effects and exclusive coverage. This module's acceptance
hooks run inside that same transaction and never call a model or a network.
"""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
from uuid import UUID, uuid5

from novel_factory.application.authorization import Principal
from novel_factory.application.kernel import require, world_ref
from novel_factory.contracts.registry import validate_contract
from novel_factory.domain.canonical import fingerprint
from novel_factory.domain.scheduling import (
    SIMULATION_TEMPLATES,
    TERMINAL,
    after,
    apply_schedule_change,
    closure_at,
    coverage_blockers,
    deterministic_result,
    registry_fingerprint,
    state_from_revision,
    validate_registration,
)
from novel_factory.domain.temporal import point_key


def _control(kernel, r, principal, wr, capability="PROPOSE"):
    grant = kernel.authorize(r, principal, wr["work_id"], capability, wr["branch_id"])
    require("WORLD" in grant["planes"] or "SIMULATION" in grant["planes"],
            "VIEW_DENIED", "World scheduling requires an authorized world view")
    return grant


def _validate_evidence(kernel, r, principal, revision, references, *, at=None):
    for evidence in references:
        validate_contract("EvidenceRef", evidence)
        if evidence["kind"] == "EVENT":
            er = evidence["event_ref"]
            require(evidence["world_ref"]["work_id"] == revision["work_id"]
                    and revision["selection"].get(er["occurrence_id"]) == er["event_version_id"],
                    "INVALID_EVIDENCE", "Process evidence is outside the selected history")
            kernel.authorize(r, principal, revision["work_id"], "READ", evidence["world_ref"]["branch_id"])
            source = kernel.revision(r, evidence["world_ref"])
            require(source["selection"].get(er["occurrence_id"]) == er["event_version_id"],
                    "INVALID_EVIDENCE", "Evidence version is not selected")
            event = r.one("events", {"event_version_id": er["event_version_id"], "work_id": revision["work_id"]})
            occurrence = event["payload"]["occurrence_time"]
            if at is not None:
                require(occurrence["kind"] == "EXACT" and point_key(occurrence["at"]) <= point_key(at),
                        "INVALID_EVIDENCE", "Process evidence does not precede its use")
        else:
            artifact = kernel.artifact(r, evidence["artifact_ref"], revision["work_id"])
            allowed = {"RULESET"} if evidence["kind"] == "RULE" else {"EVIDENCE", "INITIAL_SETUP", "AGENT_OUTPUT", "TEXT"}
            require(artifact["kind"] in allowed,
                    "INVALID_EVIDENCE", "Process evidence has an incompatible kind")
            kernel.objects.verify(evidence["artifact_ref"]["sha256"])
            if "span" in evidence:
                try:
                    text = kernel.objects.read(evidence["artifact_ref"]["sha256"]).decode("utf-8")
                except UnicodeDecodeError:
                    require(False, "INVALID_EVIDENCE", "Evidence quotation is not text")
                span = evidence["span"]
                require(0 <= span["start"] < span["end"] <= len(text)
                        and sha256(text[span["start"]:span["end"]].encode()).hexdigest() == span["quote_sha256"],
                        "INVALID_EVIDENCE", "Evidence quotation does not match its immutable source")


def _expanded_scopes(snapshot, scope_ids, at):
    scopes = set(scope_ids)
    # Region-level declared influences also gate updates to occupants and
    # containers. No new direct template can silently bypass a regional gate.
    changed = True
    while changed:
        changed = False
        for relation in snapshot["relations"]:
            if relation["plane"] != "WORLD" or relation["predicate"] not in {"located_in", "contains"}:
                continue
            interval = relation["valid_time"]
            if point_key(interval["start"]) > point_key(at) or (
                interval["end"] is not None and point_key(at) >= point_key(interval["end"])
            ):
                continue
            subject, target = relation["subject_id"], relation["object_value"].get("entity_id")
            if relation["predicate"] == "contains":
                subject, target = target, subject
            if subject in scopes and target not in scopes:
                scopes.add(target)
                changed = True
    return scopes


def _validate_intents_current(kernel, r, interaction):
    rows = r.many("simulation_intents", {"interaction_id": interaction["interaction_id"]}, order=["actor_id"])
    slots = {slot["actor_id"]: slot["grant_id"] for slot in interaction["participant_slots"]}
    require({row["actor_id"] for row in rows} == set(slots),
            "INPUTS_PENDING", "Required actor inputs are incomplete; story time is unchanged")
    for row in rows:
        grant = r.one("grants", {"grant_id": row["grant_id"]}, lock="share")
        require(grant and grant["active"] and grant["actor_id"] == row["actor_id"]
                and slots[row["actor_id"]] == row["grant_id"]
                and grant["principal_id"] == row["principal_id"],
                "VIEW_DENIED", "A participant's authorization is no longer available")
        principal = Principal(row["grant_id"], row["principal_id"])
        current = r.one("tasks", {"task_id": row["task_id"]}, lock="update")
        if current and current["state"] == "ACCEPTED":
            # A closed, enrolled slot is immutable evidence only. Never make
            # completed tasks eligible for new proposals, calls or writes.
            from novel_factory.application.queries import WorldQueryService
            task, _, spec = WorldQueryService(kernel)._task(r, principal, row["task_id"], row["generation"])
            job = r.one("supervisor_jobs", {"task_id": row["task_id"]})
            context = r.one("contexts", {"context_bundle_id": task["context_bundle_id"], "task_id": row["task_id"],
                                        "generation": row["generation"], "grant_id": row["grant_id"]})
            require(job and job["config"].get("seal_interaction_id") == interaction["interaction_id"]
                    and context and task["context_bundle_id"] == row["payload"]["context_bundle_id"]
                    and context["read_cut"]["permission_epoch"] == grant["epoch"] == job["config"]["grant_epoch"]
                    and context["read_cut"]["knowledge_cut"] == job["config"]["knowledge_cut"]
                    and context["read_cut"]["knowledge_cut"]["plane"] == "BELIEF"
                    and context["read_cut"]["knowledge_cut"]["actor_id"] == row["actor_id"]
                    and context["payload"]["actor_id"] == row["actor_id"]
                    and context["dependency_fingerprint"] == row["payload"]["dependency_fingerprint"],
                    "VIEW_DENIED", "Closed actor slot is no longer authorized")
            world_ref = spec["world_ref"]
        else:
            task, context = kernel._task_binding(r, principal, row["task_id"], row["generation"],
                                                row["payload"]["context_bundle_id"])
            world_ref = task["world_ref"]
        require(world_ref == interaction["base_world_ref"]
                and context["payload"]["story_time"] == interaction["story_time"],
                "TASK_REBIND_REQUIRED", "Participant input no longer matches its sealed slot")
    return sorted(rows, key=lambda row: row["actor_id"])


def _canonical_bundle_hash(interaction, intents):
    return fingerprint({"interaction": interaction,
                        "intents": [{"actor_id": row["actor_id"], "artifact_ref": row["artifact_ref"],
                                     "payload_hash": row["payload_hash"]} for row in intents]})


def outcome_command(interaction_ref, interaction, outcome_ref, outcome):
    return {
        "interaction_id": interaction["interaction_id"], "at": deepcopy(interaction["story_time"]),
        "interaction_ref": deepcopy(interaction_ref), "outcome_ref": deepcopy(outcome_ref),
        "scope_ids": deepcopy(interaction["scope_ids"]), "process_ids": deepcopy(interaction["process_ids"]),
        "registry_fingerprint": interaction["registry_fingerprint"],
        "commands": deepcopy(outcome["commands"]), "process_outcomes": deepcopy(outcome["process_outcomes"]),
        "outgoing_processes": deepcopy(outcome["outgoing_processes"]),
    }


def _validate_registered_recipes(state, interaction, outcome, snapshot):
    from novel_factory.application.templates import build
    commands, updates = deterministic_result(state, interaction["process_ids"], snapshot, interaction["story_time"], build)
    expected = {entry["process_id"]: entry for entry in updates}
    actual = {entry["process_id"]: entry for entry in outcome["process_outcomes"]}
    require(all(actual.get(pid) == entry for pid, entry in expected.items()),
            "HARD_RULE_VIOLATION", "Selected outcome differs from the registered deterministic evaluation")
    from collections import Counter
    expected_commands = Counter(fingerprint(command) for command in commands)
    actual_commands = Counter(fingerprint(command) for command in outcome["commands"])
    require(all(actual_commands[key] >= count for key, count in expected_commands.items()),
            "HARD_RULE_VIOLATION", "Selected result omits a deterministic consequence")
    if len(expected) == len(interaction["process_ids"]):
        require(actual_commands == expected_commands, "HARD_RULE_VIOLATION", "Deterministic result contains unregistered effects")


def validate_schedule_command(kernel, r, principal, revision, template_id, args, footprint):
    """Kernel hook: call for proposals, certificate sampling, and final accept.

    Old P1 revisions without a registry take a no-extra-SQL fast path for their
    existing commands. New commands always validate trusted persisted inputs.
    """
    state = state_from_revision(revision)
    if template_id == "REPAIR_EVENT":
        if args.get("equivalence_proof_ref") is not None:
            from novel_factory.application.refinement import validate_refinement_command
            validate_refinement_command(kernel, r, principal, revision, args)
        else:
            require(not state["processes"], "SCHEDULE_REPAIR_REQUIRED",
                    "Historical edits with registered processes require a reviewed schedule rebuild")
        return
    if template_id == "REGISTER_PROCESS":
        artifact = kernel.artifact(r, args["record_ref"], revision["work_id"], kind="SIMULATION_PROCESS")
        require(artifact["payload"] == args["process"], "INVALID_EVIDENCE", "Process record differs from its immutable source")
        validate_registration(args["process"], revision["snapshot"], revision["frontiers"], state, args["at"])
        _validate_evidence(kernel, r, principal, revision, args["process"]["rule_evidence_refs"], at=args["at"])
        for channel in args["process"]["channels"]:
            _validate_evidence(kernel, r, principal, revision, channel["evidence_refs"], at=args["at"])
        return
    if not state["processes"] and template_id not in SIMULATION_TEMPLATES:
        return
    included = []
    if template_id == "JOINT_INTERACTION":
        interaction_artifact = kernel.artifact(r, args["interaction_ref"], revision["work_id"], kind="SIMULATION_INTERACTION")
        outcome_artifact = kernel.artifact(r, args["outcome_ref"], revision["work_id"], kind="SIMULATION_OUTCOME")
        interaction, outcome = interaction_artifact["payload"], outcome_artifact["payload"]
        require(interaction["base_world_ref"] == world_ref(revision)
                and outcome["base_world_ref"] == world_ref(revision),
                "HEAD_CONFLICT", "Joint arbitration requires its exact common starting world")
        require(args == outcome_command(args["interaction_ref"], interaction, args["outcome_ref"], outcome),
                "INVALID_EVIDENCE", "Joint command differs from the selected outcome")
        record = r.one("simulation_interactions", {"interaction_id": interaction["interaction_id"]})
        chosen = r.one("simulation_outcomes", {"interaction_id": interaction["interaction_id"]})
        require(record and chosen and record["artifact_ref"] == args["interaction_ref"]
                and chosen["artifact_ref"] == args["outcome_ref"],
                "INVALID_EVIDENCE", "Joint result has no trusted selection record")
        require(registry_fingerprint(state) == interaction["registry_fingerprint"],
                "DEPENDENCY_CONFLICT", "Registered influences changed after the group was closed")
        actual = closure_at(state, interaction["scope_ids"], args["at"])
        require(actual["process_ids"] == interaction["process_ids"]
                and actual["scope_ids"] == interaction["scope_ids"]
                and actual["actor_ids"] == sorted(slot["actor_id"] for slot in interaction["participant_slots"]),
                "UNPROVEN_FOOTPRINT", "Interaction omits a registered participant or influence")
        rows = _validate_intents_current(kernel, r, interaction)
        require(_canonical_bundle_hash(interaction, rows) == outcome["bundle_hash"],
                "INVALID_EVIDENCE", "Sealed input set differs from the selected decision")
        require(interaction["interaction_id"] not in state["closed_interactions"],
                "ALREADY_ACCEPTED", "Interaction is already closed")
        included = interaction["process_ids"]
        updates = args["process_outcomes"]
        require(len(updates) == len(included) and {u["process_id"] for u in updates} == set(included),
                "UNPROVEN_FOOTPRINT", "Every registered trigger needs one explicit outcome")
        for update in updates:
            if update["world_stage"] in TERMINAL:
                require(update["next_due"] is None, "INVALID_ARGUMENT", "A terminal process cannot retain a trigger")
            elif update["next_due"] is not None:
                require(point_key(update["next_due"]) > point_key(args["at"]),
                        "TIME_AMBIGUOUS", "A subsequent trigger needs a later causal point")
        _validate_registered_recipes(state, interaction, outcome, revision["snapshot"])
        _validate_evidence(kernel, r, principal, revision, outcome["rule_evidence_refs"], at=args["at"])
        temporary = deepcopy(state)
        for outgoing in args["outgoing_processes"]:
            artifact = kernel.artifact(r, outgoing["record_ref"], revision["work_id"], kind="SIMULATION_PROCESS")
            require(artifact["payload"] == outgoing["process"], "INVALID_EVIDENCE", "Outgoing process differs from its source")
            validate_registration(outgoing["process"], revision["snapshot"], revision["frontiers"], temporary, args["at"])
            _validate_evidence(kernel, r, principal, revision, outgoing["process"]["rule_evidence_refs"], at=args["at"])
            for channel in outgoing["process"]["channels"]:
                _validate_evidence(kernel, r, principal, revision, channel["evidence_refs"], at=args["at"])
            closed_scopes = set(footprint["process_scopes"])
            due = outgoing["process"]["due_at"]
            if closed_scopes.intersection(outgoing["process"]["scope_ids"]):
                require(due is not None and point_key(due) >= point_key(after(args["at"])),
                        "ADVANCE_BLOCKED", "Outgoing trigger requires an unresolved later phase")
            for channel in outgoing["process"]["channels"]:
                if channel["to_scope_id"] in closed_scopes:
                    bound = channel["earliest_possible_impact"]
                    require(bound is not None and point_key(bound) >= point_key(after(args["at"])),
                            "ADVANCE_BLOCKED", "Outgoing message cannot enter the just-closed phase")
            # Block duplicate registrations even within one joint outcome.
            temporary["processes"][outgoing["process"]["process_id"]] = {"reserved": True}
    targets = footprint.get("advance_targets")
    if targets is None:
        target = args.get("target") if template_id in {"ADVANCE_SCOPE", "SCHEDULE_ADVANCE"} else after(args["at"])
        targets = {scope: target for scope in footprint["process_scopes"]}
    for scope, target in targets.items():
        expanded = _expanded_scopes(revision["snapshot"], [scope], args.get("at", target))
        blockers = coverage_blockers(state, expanded, target, included_process_ids=included)
        require(not blockers, "ADVANCE_BLOCKED", "Registered unresolved influences prevent this coverage")


def changeset_extras(revision, template_id, args, event_candidates):
    """Kernel hook: materialize typed record operations and trigger evidence."""
    records, triggers = [], []
    processes = ([{"process": args["process"], "record_ref": args["record_ref"]}]
                 if template_id == "REGISTER_PROCESS" else args.get("outgoing_processes", []))
    for item in processes:
        records.append({"record_id": item["process"]["process_id"],
                        "record_version_id": item["record_ref"]["artifact_revision_id"],
                        "record_kind": "PROCESS", "operation": "SELECT", "prior_record_version_id": None,
                        "payload_ref": item["record_ref"]})
    if template_id == "JOINT_INTERACTION":
        state = state_from_revision(revision)
        event_refs = [{key: e[key] for key in ("occurrence_id", "event_version_id")} for e in event_candidates]
        for update in args["process_outcomes"]:
            entry = state["processes"][update["process_id"]]
            triggers.append({"trigger_id": entry["definition"]["trigger_id"],
                             "trigger_record_version_id": entry["trigger_version_id"], "outcome": update["outcome"],
                             "produced_event_refs": event_refs if update["outcome"] == "FIRED" else [],
                             "replacement_trigger_refs": [], "evidence_refs": entry["definition"]["rule_evidence_refs"]})
    return {"record_operations": records, "trigger_evaluations": triggers}


def attach_schedule_state(current, revision, proposal):
    """Kernel hook: call before inserting the immutable accepted revision.

    Manifest covers the schedule hash in addition to genesis/event/frontier
    content. A normal P1 command on a P2 world preserves its registry exactly.
    """
    previous = state_from_revision(current)
    if proposal["template_id"] == "REPAIR_EVENT" and proposal["template_args"].get("equivalence_proof_ref"):
        revision["provenance"]["equivalence_proof_ref"] = deepcopy(proposal["template_args"]["equivalence_proof_ref"])
        revision["provenance"]["refinement_source_event_ref"] = {
            "occurrence_id": proposal["template_args"]["occurrence_id"],
            "event_version_id": current["selection"][proposal["template_args"]["occurrence_id"]],
        }
    if not previous["processes"] and proposal["template_id"] not in SIMULATION_TEMPLATES:
        return
    events = [{key: e[key] for key in ("occurrence_id", "event_version_id")}
              for e in proposal["payload"]["event_candidates"]]
    state = apply_schedule_change(previous, proposal["template_id"], proposal["template_args"], events)
    revision["provenance"]["simulation_state"] = state
    revision["provenance"]["simulation_sha256"] = fingerprint(state)
    # Compose with the unchanged P1 manifest instead of changing historical
    # P1 hashing semantics. The covered original is retained for verification.
    revision["provenance"]["simulation_base_manifest"] = revision["manifest_hash"]
    revision["manifest_hash"] = fingerprint({"base_manifest": revision["manifest_hash"],
                                            "simulation_sha256": fingerprint(state)})


def verify_schedule_revision(revision):
    provenance = revision.get("provenance", {})
    if not any(key in provenance for key in ("simulation_state", "simulation_sha256", "simulation_base_manifest")):
        return
    state = state_from_revision(revision)
    require(revision["manifest_hash"] == fingerprint({
        "base_manifest": revision["provenance"].get("simulation_base_manifest"),
        "simulation_sha256": fingerprint(state)}), "PROJECTION_CORRUPT", "Schedule manifest integrity failed")


def inherit_schedule_state(source, target):
    """Fork hook: preserve precisely the registered records selected at source."""
    verify_schedule_revision(source)
    for key in ("simulation_state", "simulation_sha256", "simulation_base_manifest"):
        if key in source.get("provenance", {}):
            target["provenance"][key] = deepcopy(source["provenance"][key])


class WorldSchedulerService:
    def __init__(self, kernel):
        self.kernel = kernel

    def stage_process(self, principal, wr, process, idempotency_key):
        """Save a typed candidate; caller still must propose/prepare/accept it."""
        validate_contract("ProcessRegistration", process, "simulation")
        k = self.kernel
        command = {"world_ref": wr, "process": process}
        with k.db.uow(write=False) as r:
            _control(k, r, principal, wr)
            revision = k.revision(r, wr)
            validate_registration(process, revision["snapshot"], revision["frontiers"],
                                  state_from_revision(revision), process["start_at"])
        ref = k._blob(process)
        with k.objects.lock(), k.db.uow() as r:
            _control(k, r, principal, wr)
            r.one("branches", {"branch_id": wr["branch_id"]}, lock="update")
            old = k._prior(r, principal, wr["work_id"], "SIM_PROCESS", idempotency_key, command)
            if old:
                return old
            _validate_evidence(k, r, principal, revision, process["rule_evidence_refs"], at=process["start_at"])
            for channel in process["channels"]:
                _validate_evidence(k, r, principal, revision, channel["evidence_refs"], at=process["start_at"])
            k._register_artifact(r, ref, wr["work_id"], process, "SIMULATION_PROCESS", principal.principal_id)
            k._receipt(r, principal, wr, "SIM_PROCESS", idempotency_key, command, ref)
        return ref

    def propose_registration(self, principal, task_id, generation, process_ref, idempotency_key):
        k = self.kernel
        with k.db.uow(write=False) as r:
            task, _ = k._task_binding(r, principal, task_id, generation)
            _control(k, r, principal, task["world_ref"])
            process = k.artifact(r, process_ref, task["work_id"], kind="SIMULATION_PROCESS")["payload"]
        return k.propose(principal, task_id, generation, "REGISTER_PROCESS",
                         {"at": process["start_at"], "process": process, "record_ref": process_ref}, idempotency_key)

    def processes(self, principal, wr):
        k = self.kernel
        with k.db.uow(write=False) as r:
            _control(k, r, principal, wr, "READ")
            state = state_from_revision(k.revision(r, wr))
        records = []
        for pid, entry in sorted(state["processes"].items()):
            process = entry["definition"]
            due = entry["next_due"]
            record = {"process_id": pid, "world_ref": wr, "scope_key": "scope:" + process["scope_ids"][0],
                      "next_due": {"start": due, "end": after(due), "bounds": "[)"} if due else None,
                      "inbound_channels": sorted({channel["channel_id"] for other in state["processes"].values()
                          for channel in other["definition"]["channels"] if channel["to_scope_id"] in process["scope_ids"]}),
                      "outbound_bounds": [{"channel": channel["channel_id"],
                          "earliest_possible_impact": channel["earliest_possible_impact"], "evidence_refs": channel["evidence_refs"]}
                          for channel in process["channels"] if channel["earliest_possible_impact"] is not None],
                      "registry_version": registry_fingerprint(state),
                      "coverage": "COMPLETE" if entry["evaluation_status"] == "EVALUATED" else "PARTIAL",
                      "world_stage": entry["world_stage"], "evaluation_status": entry["evaluation_status"],
                      "evaluated_frontier": entry["evaluated_frontier"]}
            validate_contract("ProcessRecord", record)
            records.append(record)
        return records

    def prepare_advance(self, principal, request):
        validate_contract("AdvancePrepareRequest", request)
        k = self.kernel
        with k.db.uow(write=False) as r:
            task, _ = k._task_binding(r, principal, request["task_id"], request["generation"], request["context_bundle_id"])
            require(task["world_ref"] == request["world_ref"], "TASK_REBIND_REQUIRED", "Advance input differs from task")
            _control(k, r, principal, request["world_ref"])
        scope_ids = []
        for key in request["scope_keys"]:
            identifier = key.removeprefix("scope:")
            validate_contract("UUID", identifier)
            scope_ids.append(identifier)
        require(scope_ids and len(scope_ids) == len(set(scope_ids)), "INVALID_ARGUMENT", "Distinct scopes are required")
        return k.propose(principal, request["task_id"], request["generation"], "SCHEDULE_ADVANCE",
                         {"scope_ids": sorted(scope_ids), "target": request["target_frontier"]}, request["idempotency_key"])

    def prepare_interaction(self, principal, wr, at, scope_ids, participant_grants, idempotency_key):
        k = self.kernel
        validate_contract("WorldRef", wr)
        validate_contract("StoryPoint", at)
        command = {"world_ref": wr, "at": at, "scope_ids": sorted(scope_ids), "participant_grants": participant_grants}
        with k.db.uow(write=False) as r:
            _control(k, r, principal, wr)
            revision = k.revision(r, wr)
            state = state_from_revision(revision)
            closure = closure_at(state, scope_ids, at)
            require(set(participant_grants) == set(closure["actor_ids"]),
                    "UNPROVEN_FOOTPRINT", "All and only registered actor slots must be bound")
            for actor_id, gid in participant_grants.items():
                grant = r.one("grants", {"grant_id": gid})
                require(grant and grant["active"] and grant["actor_id"] == actor_id
                        and grant["role_id"] == "character_actor" and grant["work_id"] == wr["work_id"]
                        and (grant["branch_ids"] == ["*"] or wr["branch_id"] in grant["branch_ids"]),
                        "VIEW_DENIED", "Participant grant is not eligible for this slot")
        closure_hash = fingerprint({"at": at, "scope_ids": closure["scope_ids"], "process_ids": closure["process_ids"]})
        iid = str(uuid5(UUID(wr["world_revision_id"]), "interaction:" + closure_hash))
        payload = {"schema_version": "2.2", "interaction_id": iid, "base_world_ref": wr, "story_time": at,
                   "scope_ids": closure["scope_ids"], "process_ids": closure["process_ids"],
                   "participant_slots": [{"actor_id": aid, "grant_id": participant_grants[aid]} for aid in closure["actor_ids"]],
                   "registry_fingerprint": registry_fingerprint(state), "phase_policy": "SINGLE_STAGE",
                   "created_by": principal.principal_id}
        validate_contract("InteractionBundle", payload, "simulation")
        ref = k._blob(payload)
        with k.objects.lock(), k.db.uow() as r:
            _control(k, r, principal, wr)
            r.one("branches", {"branch_id": wr["branch_id"]}, lock="update")
            old = k._prior(r, principal, wr["work_id"], "SIM_INTERACTION", idempotency_key, command)
            if old:
                return old
            existing = r.one("simulation_interactions", {"interaction_id": iid})
            if existing:
                require(existing["payload"] == payload, "INTERACTION_ALREADY_CLOSED", "This common input already has a sealed group")
                ref = existing["artifact_ref"]
            else:
                k._register_artifact(r, ref, wr["work_id"], payload, "SIMULATION_INTERACTION", principal.principal_id)
                r.add("simulation_interactions", {"interaction_id": iid, "work_id": wr["work_id"], "branch_id": wr["branch_id"],
                    "base_revision_id": wr["world_revision_id"], "closure_hash": closure_hash, "artifact_ref": ref,
                    "payload": payload, "created_by": principal.principal_id})
            result = {"interaction_id": iid, "interaction_ref": ref, "base_world_ref": wr,
                      "required_actor_ids": closure["actor_ids"]}
            k._receipt(r, principal, wr, "SIM_INTERACTION", idempotency_key, command, result)
        return result

    def submit_action(self, principal, interaction_id, proposal):
        with self.kernel.objects.lock(), self.kernel.db.uow() as repo:
            return self.seal_in_transaction(repo, principal, interaction_id, proposal)

    def seal_in_transaction(self, r, principal, interaction_id, proposal):
        """Caller holds the shared object lock and short application transaction."""
        validate_contract("ActionProposal", proposal)
        k = self.kernel
        interaction = r.one("simulation_interactions", {"interaction_id": interaction_id})
        require(interaction, "VIEW_DENIED", "Interaction unavailable")
        wr = interaction["payload"]["base_world_ref"]
        grant = k.authorize(r, principal, wr["work_id"], "READ", wr["branch_id"])
        require(grant["role_id"] == "character_actor" and grant["actor_id"] == proposal["actor_id"],
                "VIEW_DENIED", "Actor identity is bound by the current grant")
        slots = {slot["actor_id"]: slot["grant_id"] for slot in interaction["payload"]["participant_slots"]}
        require(slots.get(grant["actor_id"]) == principal.grant_id, "VIEW_DENIED", "No authorized slot")
        # One interaction row is a short input-collection lock, not a world
        # resource lock and not held while any Agent is generating text.
        r.one("simulation_interactions", {"interaction_id": interaction_id}, lock="update")
        old = r.one("simulation_intents", {"interaction_id": interaction_id, "actor_id": grant["actor_id"]})
        if old:
            require(old["payload_hash"] == fingerprint(proposal), "IDEMPOTENCY_MISMATCH", "Slot is sealed to different input")
            return old["artifact_ref"]
        require(not r.one("simulation_outcomes", {"interaction_id": interaction_id}), "INTERACTION_ALREADY_CLOSED", "Input collection is closed")
        task, context = k._task_binding(r, principal, proposal["task_id"], proposal["generation"], proposal["context_bundle_id"])
        require(proposal["world_ref"] == task["world_ref"] == wr
                and context["payload"]["story_time"] == interaction["payload"]["story_time"]
                and proposal["story_interval"]["start"] == interaction["payload"]["story_time"]
                and point_key(proposal["story_interval"]["end"]) > point_key(proposal["story_interval"]["start"])
                and proposal["dependency_fingerprint"] == context["dependency_fingerprint"],
                "TASK_REBIND_REQUIRED", "Intent is not bound to the common story and knowledge input")
        permitted = {fingerprint(value) for value in context["payload"]["permitted_evidence_refs"]}
        require(all(fingerprint(value) in permitted for value in proposal["evidence_refs"]),
                "VIEW_DENIED", "Intent cites evidence outside its authorized context")
        ref = k._blob(proposal)
        k._register_artifact(r, ref, wr["work_id"], proposal, "SIMULATION_INTENT", principal.principal_id)
        r.add("simulation_intents", {"interaction_id": interaction_id, "actor_id": grant["actor_id"],
            "task_id": proposal["task_id"], "generation": proposal["generation"], "grant_id": principal.grant_id,
            "principal_id": principal.principal_id, "artifact_ref": ref, "payload": proposal, "payload_hash": fingerprint(proposal)})
        return ref

    def interaction_status(self, principal, interaction_id):
        """Actors see their own slot only; counts and missing actor IDs are private."""
        k = self.kernel
        with k.db.uow(write=False) as r:
            row = r.one("simulation_interactions", {"interaction_id": interaction_id})
            require(row, "VIEW_DENIED", "Interaction unavailable")
            wr, interaction = row["payload"]["base_world_ref"], row["payload"]
            grant = k.authorize(r, principal, wr["work_id"], "READ", wr["branch_id"])
            if grant["role_id"] == "character_actor":
                slots = {slot["actor_id"]: slot["grant_id"] for slot in interaction["participant_slots"]}
                require(slots.get(grant["actor_id"]) == principal.grant_id, "VIEW_DENIED", "Interaction unavailable")
                own = r.one("simulation_intents", {"interaction_id": interaction_id, "actor_id": grant["actor_id"]})
                return {"interaction_id": interaction_id, "own_input_stored": own is not None,
                        "status": "OWN_INPUT_STORED" if own else "PENDING_OWN_INPUT"}
            _control(k, r, principal, wr)
            intents = r.many("simulation_intents", {"interaction_id": interaction_id}, order=["actor_id"])
            missing = sorted({slot["actor_id"] for slot in interaction["participant_slots"]} - {x["actor_id"] for x in intents})
            chosen = r.one("simulation_outcomes", {"interaction_id": interaction_id})
            return {"interaction_id": interaction_id, "base_world_ref": wr,
                    "status": "INPUTS_PENDING" if missing else ("OUTCOME_SELECTED" if chosen else "READY_FOR_ADJUDICATION"),
                    "missing_actor_ids": missing, "intent_refs": [x["artifact_ref"] for x in intents],
                    "outcome_ref": chosen["artifact_ref"] if chosen else None}

    def choose_outcome(self, principal, interaction_id, *, commands, process_outcomes,
                       reasoning_summary, rule_evidence_refs, source_kind="RULE_DECISION",
                       source_ref=None, outgoing_processes=()):
        """An authorized explicit decision; no first-returned-worker winner rule."""
        k = self.kernel
        with k.db.uow(write=False) as r:
            record = r.one("simulation_interactions", {"interaction_id": interaction_id})
            require(record, "VIEW_DENIED", "Interaction unavailable")
            interaction = record["payload"]
            wr = interaction["base_world_ref"]
            _control(k, r, principal, wr, "APPROVE")
            intents = _validate_intents_current(k, r, interaction)
            if source_kind == "EXTERNAL_AGENT":
                require(source_ref is not None, "INVALID_EVIDENCE", "External result requires its stored output")
            if source_ref is not None:
                k.artifact(r, source_ref, wr["work_id"])
            revision = k.revision(r, wr)
            _validate_evidence(k, r, principal, revision, rule_evidence_refs, at=interaction["story_time"])
        # Canonical ordering stabilizes effects across model return permutations;
        # the explicit outcome already determines who wins under the world rule.
        normalized_commands = sorted(deepcopy(commands), key=fingerprint)
        outcome = {"schema_version": "2.2", "interaction_id": interaction_id, "base_world_ref": wr,
                   "story_time": interaction["story_time"], "bundle_hash": _canonical_bundle_hash(interaction, intents),
                   "action_proposal_refs": [row["artifact_ref"] for row in intents], "commands": normalized_commands,
                   "process_outcomes": sorted(deepcopy(process_outcomes), key=lambda x: x["process_id"]),
                   "outgoing_processes": list(deepcopy(outgoing_processes)), "reasoning_summary": reasoning_summary,
                   "source_kind": source_kind, "source_ref": source_ref, "rule_evidence_refs": rule_evidence_refs}
        validate_contract("SelectedOutcome", outcome, "simulation")
        _validate_registered_recipes(state_from_revision(revision), interaction, outcome, revision["snapshot"])
        # Check simultaneous resource conflicts before selecting an immutable
        # result. Failure candidates remain in their external call/output log.
        from novel_factory.application.templates import build
        provisional = outcome_command(record["artifact_ref"], interaction, record["artifact_ref"], outcome)
        build("JOINT_INTERACTION", provisional, revision["snapshot"], interaction_id)
        ref = k._blob(outcome)
        with k.objects.lock(), k.db.uow() as r:
            _control(k, r, principal, wr, "APPROVE")
            r.one("simulation_interactions", {"interaction_id": interaction_id}, lock="update")
            old = r.one("simulation_outcomes", {"interaction_id": interaction_id})
            if old:
                require(old["request_hash"] == fingerprint(outcome), "OUTCOME_ALREADY_SELECTED", "A selected result cannot be rerolled")
                return old["artifact_ref"]
            rows = _validate_intents_current(k, r, interaction)
            require(_canonical_bundle_hash(interaction, rows) == outcome["bundle_hash"], "DEPENDENCY_CONFLICT", "Input collection changed")
            k._register_artifact(r, ref, wr["work_id"], outcome, "SIMULATION_OUTCOME", principal.principal_id)
            r.add("simulation_outcomes", {"interaction_id": interaction_id, "artifact_ref": ref, "payload": outcome,
                                           "request_hash": fingerprint(outcome), "selected_by": principal.principal_id})
        return ref

    def evaluate_due(self, principal, wr, at, scope_ids, idempotency_key):
        """Prepare a fixed deterministic outcome; acceptance remains explicit."""
        k = self.kernel
        with k.db.uow(write=False) as r:
            _control(k, r, principal, wr, "APPROVE")
            revision = k.revision(r, wr)
            state = state_from_revision(revision)
            closure = closure_at(state, scope_ids, at)
            require(all(state["processes"][pid]["definition"]["evaluator_kind"] == "DETERMINISTIC"
                        for pid in closure["process_ids"]), "INPUTS_PENDING", "Coupled semantic processes need sealed actor/adjudicator input")
        interaction = self.prepare_interaction(principal, wr, at, scope_ids, {}, idempotency_key)
        from novel_factory.application.templates import build
        commands, updates = deterministic_result(state, closure["process_ids"], revision["snapshot"], at, build)
        evidence = []
        for pid in closure["process_ids"]:
            for item in state["processes"][pid]["definition"]["rule_evidence_refs"]:
                if item not in evidence:
                    evidence.append(item)
        outcome = self.choose_outcome(principal, interaction["interaction_id"], commands=commands,
            process_outcomes=updates, reasoning_summary="Evaluated registered deterministic recipes against the common world; false conditions apply zero effects.",
            rule_evidence_refs=evidence, source_kind="RULE_DECISION")
        return {**interaction, "outcome_ref": outcome}

    def propose_outcome(self, principal, task_id, generation, outcome_ref, idempotency_key):
        k = self.kernel
        with k.db.uow(write=False) as r:
            task, _ = k._task_binding(r, principal, task_id, generation)
            _control(k, r, principal, task["world_ref"])
            outcome = k.artifact(r, outcome_ref, task["work_id"], kind="SIMULATION_OUTCOME")["payload"]
            interaction = r.one("simulation_interactions", {"interaction_id": outcome["interaction_id"]})
            require(interaction, "INVALID_EVIDENCE", "Interaction record is absent")
            args = outcome_command(interaction["artifact_ref"], interaction["payload"], outcome_ref, outcome)
        return k.propose(principal, task_id, generation, "JOINT_INTERACTION", args, idempotency_key)


# A separate name exposes the responsibility without introducing another store.
AdjudicationService = WorldSchedulerService
