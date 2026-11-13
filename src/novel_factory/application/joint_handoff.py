"""A preauthorized, single-process joint decision with one bounded location change or specifically authorized item gift."""

from datetime import timedelta
from uuid import UUID, uuid5

from novel_factory.application.authorization import Principal
from novel_factory.application.candidate_delivery import CandidateDelivery
from novel_factory.application.kernel import require
from novel_factory.application.queries import WorldQueryService
from novel_factory.application.simulation import WorldSchedulerService, _validate_intents_current
from novel_factory.contracts.registry import validate_contract
from novel_factory.domain.canonical import fingerprint
from novel_factory.domain.scheduling import state_from_revision


def _destinations(route):
    return [route["destination_id"], *route.get("alternative_destination_ids", [])]



def joint_manifest(route):
    subjects = [route["subject_id"], *_destinations(route), *route.get("observed_subject_ids", [])]
    predicates = ["located_in"]
    if gift := route.get("allowed_item_gift"):
        subjects.extend([gift["item_id"], gift["from_id"], *gift["recipient_ids"]])
        predicates.extend(["legal_owner", "possessed_by"])
    return {"subject_ids": list(dict.fromkeys(subjects)), "predicates": predicates}


def validate_item_gift(gift, actors):
    require(isinstance(gift, dict) and set(gift) == {"item_id", "from_id", "recipient_ids"},
            "INVALID_ARGUMENT", "Item grant requires an item, holder and explicit recipients")
    recipients = gift["recipient_ids"]
    require(isinstance(recipients, list) and 1 <= len(recipients) <= 128,
            "INVALID_ARGUMENT", "Item grant needs a bounded recipient list")
    for value in [gift["item_id"], gift["from_id"], *recipients]:
        validate_contract("UUID", value)
    require(len(set(recipients)) == len(recipients) and set(recipients) <= set(actors)
            and gift["from_id"] not in recipients and gift["item_id"] not in [gift["from_id"], *actors],
            "VIEW_DENIED", "Gift recipients must be distinct registered participants")


def _interaction(kernel, repo, route):
    require(
        isinstance(route, dict)
        and set(route) - {"alternative_destination_ids", "allowed_item_gift", "observed_subject_ids"}
        == {"interaction_id", "subject_id", "destination_id", "control_task_id"},
        "INVALID_ARGUMENT",
        "Joint authorization must specify one interaction, participant, destination and control task",
    )
    alternatives = route.get("alternative_destination_ids", [])
    require(
        isinstance(alternatives, list) and len(alternatives) <= 15,
        "INVALID_ARGUMENT",
        "Joint decision accepts at most 16 registered destinations",
    )
    destinations = _destinations(route)
    for value in [route["interaction_id"], route["subject_id"], route["control_task_id"], *destinations]:
        validate_contract("UUID", value)
    require(
        len(destinations) == len(set(destinations)) and route["subject_id"] not in destinations,
        "INVALID_ARGUMENT",
        "Locations must be distinct from each other and the participant",
    )
    row = repo.one("simulation_interactions", {"interaction_id": route["interaction_id"]})
    require(row, "VIEW_DENIED", "No authorized interaction")
    interaction = row["payload"]
    require(
        route["subject_id"] in {slot["actor_id"] for slot in interaction["participant_slots"]}
        and set(destinations) <= set(interaction["scope_ids"])
        and len(interaction["process_ids"]) == 1,
        "VIEW_DENIED",
        "Location is outside the registered participant and scope",
    )
    if "observed_subject_ids" in route:
        observed = route["observed_subject_ids"]
        require(isinstance(observed, list) and 0 < len(observed) <= 64,
                "INVALID_ARGUMENT", "Joint observation needs a bounded list")
        for subject in observed:
            validate_contract("UUID", subject)
        require(len(set(observed)) == len(observed) and set(observed) <= set(interaction["scope_ids"]),
                "VIEW_DENIED", "Observed subject falls outside the registered interaction")
    if "allowed_item_gift" in route:
        validate_item_gift(route["allowed_item_gift"], [slot["actor_id"] for slot in interaction["participant_slots"]])
    revision = kernel.revision(repo, interaction["base_world_ref"])
    process = state_from_revision(revision)["processes"][interaction["process_ids"][0]]["definition"]
    rules = repo.one("works", {"work_id": row["work_id"]})["rules_ref"]
    require(
        process["evaluator_kind"] == "SEMANTIC"
        and process["process_kind"] == "INTERACTION"
        and process["due_at"] == interaction["story_time"]
        and not process["channels"]
        and process["rule_evidence_refs"] == [{"kind": "RULE", "artifact_ref": rules}],
        "REVIEW_REQUIRED",
        "Automatic joint execution requires one due semantic interaction and its current ruleset",
    )
    require(
        kernel.head(repo, row["work_id"], row["branch_id"]) == interaction["base_world_ref"],
        "HEAD_CONFLICT",
        "Joint execution requires the exact starting world",
    )
    return row, rules


def create_control_task(
    kernel,
    controller,
    interaction_id,
    *,
    subject_id,
    destination_id,
    alternative_destination_ids=(),
    allowed_item_gift=None,
    observed_subject_ids=None,
    deadline_seconds=3600,
):
    """Reserve one deterministic Kernel task. No lease, model grant or provider call is created."""
    require(
        type(deadline_seconds) is int and 1 <= deadline_seconds <= 86400,
        "INVALID_ARGUMENT",
        "Joint control task needs a finite deadline",
    )
    validate_contract("UUID", interaction_id)
    task_id = str(uuid5(UUID(interaction_id), "joint-location-control"))
    route = dict(
        interaction_id=interaction_id,
        subject_id=subject_id,
        destination_id=destination_id,
        control_task_id=task_id,
    )
    require(
        isinstance(alternative_destination_ids, (tuple, list)), "INVALID_ARGUMENT", "Expected destination list"
    )
    if observed_subject_ids is not None:
        route["observed_subject_ids"] = observed_subject_ids
    if allowed_item_gift is not None:
        route["allowed_item_gift"] = allowed_item_gift
    if alternative_destination_ids:
        route["alternative_destination_ids"] = list(alternative_destination_ids)
    with kernel.objects.lock(), kernel.db.uow() as repo:
        row = repo.one("simulation_interactions", {"interaction_id": interaction_id})
        require(row, "VIEW_DENIED", "No authorized interaction")
        work = repo.one("works", {"work_id": row["work_id"]}, lock="update")
        grant = kernel.authorize(repo, controller, row["work_id"], "ADMIN", row["branch_id"])
        row, _ = _interaction(kernel, repo, route)
        identity = fingerprint({"kind": "joint_accept", "route": route, "deadline_seconds": deadline_seconds})
        prior = repo.one("task_inputs", {"task_id": task_id, "generation": 0})
        if prior:
            require(
                prior["spec"]["capability_grant_id"] == controller.grant_id
                and prior["spec"]["runtime_contract_version"] == identity,
                "IDEMPOTENCY_CONFLICT",
                "Control binding changed",
            )
            return {"task_id": task_id, "generation": 0}
        scoped = kernel.in_transaction(repo)
        manifest = scoped.put_artifact(
            controller,
            row["work_id"],
            joint_manifest(route),
            "INPUT_MANIFEST",
        )
        spec = {
            "schema_version": "2.0",
            "task_id": task_id,
            "generation": 0,
            "role_id": grant["role_id"],
            "task_kind": "joint_accept",
            "state": "READY",
            "world_ref": row["payload"]["base_world_ref"],
            "story_time": row["payload"]["story_time"],
            "context_bundle_id": None,
            "policy_version": work["policy_version"],
            "dependency_fingerprint": None,
            "runtime_contract_version": identity,
            "capability_grant_id": controller.grant_id,
            "input_artifacts": [manifest, row["artifact_ref"]],
            "budget": {"currency": "USD", "max_amount": "0", "token_budget": 20000},
            "wall_deadline": (repo.now() + timedelta(seconds=deadline_seconds)).isoformat(),
        }
        scoped.create_task(controller, spec, "joint-control:" + task_id)
        return {"task_id": task_id, "generation": 0}


def check_enrollment(kernel, repo, controller, task, spec, cut, agent_input, route, manifest_ref):
    require(
        (task["role_id"], task["task_kind"]) == ("adjudication_advisor", "joint_outcome")
        and cut == {"plane": "WORLD"}
        and not agent_input["material_refs"]
        and agent_input["output_contract"] == {"name": "AdjudicationProposal", "version": "2.0"},
        "VIEW_DENIED",
        "Joint decision requires a dedicated adjudication task",
    )
    runtime = kernel.artifact(repo, agent_input["runtime_ref"], task["work_id"], kind="AGENT_RUNTIME")
    require(runtime["payload"]["skill_name"] == "adjudicate-actions", "VIEW_DENIED", "Wrong adjudication skill")
    row, rules = _interaction(kernel, repo, route)
    interaction = row["payload"]
    expected = joint_manifest(route)
    require(
        interaction["base_world_ref"] == spec["world_ref"]
        and interaction["story_time"] == spec["story_time"]
        and row["artifact_ref"] in spec["input_artifacts"]
        and kernel.artifact(repo, manifest_ref, row["work_id"], kind="INPUT_MANIFEST")["payload"] == expected,
        "VIEW_DENIED",
        "Adjudicator needs the exact authorized interaction and locations",
    )
    control, _, control_spec = WorldQueryService(kernel)._task(
        repo, controller, route["control_task_id"], 0, "ACCEPT"
    )
    require(
        control["state"] == "READY"
        and control["task_kind"] == "joint_accept"
        and control_spec["world_ref"] == spec["world_ref"]
        and control_spec["story_time"] == spec["story_time"]
        and control_spec["policy_version"] == spec["policy_version"]
        and len(control_spec["input_artifacts"]) == 2
        and control_spec["input_artifacts"][1] == row["artifact_ref"]
        and kernel.artifact(repo, control_spec["input_artifacts"][0], row["work_id"], kind="INPUT_MANIFEST")[
            "payload"
        ]
        == expected,
        "VIEW_DENIED",
        "Joint acceptance task is outside this preauthorization",
    )
    return interaction, rules, control_spec


def _enrolled_cut(kernel, repo, owner, task, config):
    auth = config["joint_authorizer"]
    controller = Principal(auth["grant_id"], auth["principal_id"])
    grant = kernel.authorize(repo, controller, task["work_id"], "ADMIN", task["branch_id"])
    require(grant["epoch"] == auth["epoch"], "VIEW_DENIED", "Joint authorization was changed")
    _, actor_grant, spec = WorldQueryService(kernel)._task(repo, owner, task["task_id"], task["generation"])
    context = repo.one(
        "contexts",
        {
            "context_bundle_id": task["context_bundle_id"],
            "task_id": task["task_id"],
            "generation": task["generation"],
            "grant_id": owner.grant_id,
        },
    )
    require(
        context
        and actor_grant["epoch"] == config["grant_epoch"] == context["read_cut"]["permission_epoch"]
        and context["read_cut"]["knowledge_cut"] == config["knowledge_cut"]
        and config["policy_version"] == spec["policy_version"] == context["read_cut"]["policy_version"],
        "VIEW_DENIED",
        "Adjudicator grant or knowledge cut changed",
    )
    data = kernel.artifact(repo, config["dispatch"]["input_ref"], task["work_id"], kind="AGENT_INPUT")["payload"]
    route = config["joint"]
    interaction, rules, control = check_enrollment(
        kernel, repo, controller, task, spec, config["knowledge_cut"], data, route, config["manifest_ref"]
    )
    intents = _validate_intents_current(kernel, repo, interaction)
    wait = repo.one("workflow_waits", {"task_id": task["task_id"], "generation": task["generation"] - 1})
    edges = repo.many("workflow_edges", {"task_id": task["task_id"], "generation": task["generation"] - 1})
    require(
        wait
        and wait["mode"] == "AND"
        and wait["woken_at"] is not None
        and {r["predecessor_id"] for r in edges} == {r["task_id"] for r in intents},
        "INPUTS_PENDING",
        "Joint adjudicator needs an accepted barrier for these participants",
    )
    proposals = []
    for intent in intents:
        slot = repo.one("tasks", {"task_id": intent["task_id"], "generation": intent["generation"]})
        require(slot and slot["state"] == "ACCEPTED", "INPUTS_PENDING", "Participant has not finished")
        payload = kernel.artifact(repo, intent["artifact_ref"], task["work_id"], kind="SIMULATION_INTENT")[
            "payload"
        ]
        require(
            fingerprint(payload) == intent["payload_hash"] == intent["artifact_ref"]["sha256"],
            "INVALID_EVIDENCE",
            "Sealed participant content changed",
        )
        # Input content only: capability IDs and internal call/billing metadata stay server-side.
        proposals.append({"ref": intent["artifact_ref"], "proposal": payload})
    rule_content = kernel.artifact(repo, rules, task["work_id"], kind="RULESET")["payload"]
    require(fingerprint(rule_content) == rules["sha256"], "INVALID_EVIDENCE", "Ruleset hash changed")
    packet = {
        "interaction_id": interaction["interaction_id"],
        "action_proposals": proposals,
        "ruleset_ref": rules,
        "ruleset": rule_content,
        "story_time": interaction["story_time"],
        "allowed_location": {
            "subject_id": route["subject_id"],
            "destination_id": route["destination_id"],
            "alternative_destination_ids": route.get("alternative_destination_ids", []),
            "no_movement_allowed": True,
        },
    }
    if "allowed_item_gift" in route:
        packet["allowed_item_gift"] = route["allowed_item_gift"]
    return controller, spec, control, interaction, packet


def authorize_dispatch(kernel, repo, owner, task, config):
    return _enrolled_cut(kernel, repo, owner, task, config)[-1]


def prompt_material(kernel, principal, binding):
    task = binding["task"]
    if (task["role_id"], task["task_kind"]) != ("adjudication_advisor", "joint_outcome"):
        return None
    with kernel.db.uow(write=False) as repo:
        job = repo.one("supervisor_jobs", {"task_id": task["task_id"]})
        if job and "joint" in job["config"]:
            return authorize_dispatch(kernel, repo, principal, task, job["config"])
    return None


def _joint_commands(candidate, packet):
    effects = candidate["effects"]
    require(
        not candidate["unresolved_questions"] and len(effects) <= 1,
        "REVIEW_REQUIRED",
        "Automatic adjudication requires a resolved decision with at most one authorized command",
    )
    if not effects:
        return []
    effect = effects[0]
    if effect.get("predicate") == "legal_owner":
        gift = packet.get("allowed_item_gift")
        require(gift and effect["kind"] == "RELATION_SET" and effect["value_status"] == "VALUE"
                and effect["subject_id"] == gift["item_id"] and effect["object_id"] in gift["recipient_ids"],
                "VIEW_DENIED", "Candidate exceeds the registered item grant")
        return [{"template_id": "TRANSFER_ITEM", "args": {"item_id": gift["item_id"],
                 "from_id": gift["from_id"], "to_id": effect["object_id"], "transfer_kind": "GIFT",
                 "at": packet["story_time"]}}]
    scope = packet["allowed_location"]
    require(
        effect["kind"] == "RELATION_SET"
        and effect["predicate"] == "located_in"
        and effect["value_status"] == "VALUE"
        and effect["subject_id"] == scope["subject_id"]
        and effect["object_id"] in _destinations(scope),
        "VIEW_DENIED",
        "Candidate effect exceeds its location allowance",
    )
    return [
        {
            "template_id": "SET_RELATION",
            "args": {
                "subject_id": effect["subject_id"],
                "predicate": "located_in",
                "object_value": {"kind": "ENTITY", "entity_id": effect["object_id"]},
                "at": packet["story_time"],
            },
        }
    ]


class JointDelivery(CandidateDelivery):
    route = "joint"

    def _deliver(self, repo, task, key):
        kernel = self.kernel.in_transaction(repo)
        owner = Principal(task["grant_id"], task["principal_id"])
        cfg = repo.one("supervisor_jobs", {"task_id": task["task_id"]})["config"]
        controller, spec, control, interaction, packet = _enrolled_cut(kernel, repo, owner, task, cfg)
        evidence, candidate = self.audited_candidate(repo, task)
        refs = [p["ref"] for p in packet["action_proposals"]]
        require(
            candidate["interaction_id"] == packet["interaction_id"]
            and candidate["ruleset_ref"] == packet["ruleset_ref"]
            and candidate["input_world_ref"] == spec["world_ref"]
            and candidate["story_time"] == packet["story_time"]
            and candidate["dependency_fingerprint"] == task["dependency_fingerprint"]
            and candidate["action_proposal_refs"] == refs,
            "INVALID_EVIDENCE",
            "Adjudication does not bind its sealed cut",
        )
        commands = _joint_commands(candidate, packet)
        simulation = WorldSchedulerService(kernel)
        selected = simulation.choose_outcome(
            controller,
            packet["interaction_id"],
            commands=commands,
            process_outcomes=[
                {
                    "process_id": interaction["process_ids"][0],
                    "outcome": "FIRED",
                    "world_stage": "COMPLETED",
                    "next_due": None,
                }
            ],
            reasoning_summary=candidate["reasoning_summary"],
            rule_evidence_refs=[{"kind": "RULE", "artifact_ref": packet["ruleset_ref"]}],
            source_kind="EXTERNAL_AGENT",
            source_ref=evidence["output_ref"],
        )
        control_id = cfg["joint"]["control_task_id"]
        claim = kernel.claim_task(controller, "joint:" + control_id, task_id=control_id)
        require(
            claim and claim["generation"] == 1,
            "TASK_REBIND_REQUIRED",
            "Control claim lost its original generation",
        )
        gen = claim["generation"]
        wr = spec["world_ref"]
        compiled = WorldQueryService(kernel).context_compile(
            controller,
            {
                "schema_version": "2.1",
                "task_id": control_id,
                "generation": gen,
                "recipe_id": "ADJUDICATOR",
                "input_manifest_ref": control["input_artifacts"][0],
                "knowledge_cut": {"plane": "WORLD"},
                "read_contract": {
                    "revision": {"mode": "FIXED", "world_ref": wr},
                    "process_policy": "ALLOW_PARTIAL",
                    "index_policy": "AUTHORITATIVE",
                    "max_wait_ms": 0,
                },
                "subject_time": {"kind": "POINT", "at": packet["story_time"]},
                "token_budget": 20000,
                "idempotency_key": key + ":compile",
            },
        )
        require(compiled["status"] == "READY", "CONTEXT_REQUIRED", "Control task needs a fixed context")
        proposal = simulation.propose_outcome(controller, control_id, gen, selected, key + ":propose")
        require(proposal["approval_id"], "REVIEW_REQUIRED", "Joint command requires separate approval")
        common = {
            "schema_version": "2.1",
            "task_id": control_id,
            "generation": gen,
            "context_bundle_id": proposal["context_bundle_id"],
            "base_world_ref": wr,
            "concurrency_mode": "STRICT_HEAD",
            "proposal_ref": proposal["proposal_ref"],
            "approval_refs": [proposal["approval_id"]],
        }
        cert = kernel.prepare(
            controller,
            {**common, "dependency_handle": proposal["dependency_handle"], "idempotency_key": key + ":prepare"},
        )
        accepted = kernel.accept(
            controller,
            {
                **common,
                "acceptance_certificate_ref": cert,
                "dependency_fingerprint": proposal["dependency_fingerprint"],
                "idempotency_key": key + ":accept",
            },
        )
        kernel.tasks.accept_in_transaction(repo.conn, task["task_id"], task["generation"])
        return {
            "status": "JOINT_ACCEPTED",
            "control_task_id": control_id,
            "candidate_ref": evidence["output_ref"],
            "outcome_ref": selected,
            "world_ref": accepted["world_ref"],
            "accepted_event_refs": accepted["accepted_event_refs"],
        }
