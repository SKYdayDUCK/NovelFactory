"""Register a bounded scene once, without exposing planning data to its participants."""

from novel_factory.application.joint_handoff import create_control_task, joint_manifest, validate_item_gift
from novel_factory.application.kernel import after, require
from novel_factory.application.planning import PlanningService
from novel_factory.application.rolling_plans import RollingPlanning, _anchor_name
from novel_factory.application.runtime_tasks import RuntimeTaskService
from novel_factory.application.simulation import WorldSchedulerService
from novel_factory.application.supervisor import RuntimeSupervisor
from novel_factory.contracts.registry import validate_contract
from novel_factory.domain.canonical import fingerprint
from novel_factory.infrastructure.database import tables
from novel_factory.infrastructure.repository import KernelRepository


def _participants(entries):
    require(
        isinstance(entries, list) and 1 <= len(entries) <= 128,
        "INVALID_ARGUMENT",
        "Supply 1..128 registered participants",
    )
    slots = {}
    for entry in entries:
        require(
            isinstance(entry, dict)
            and "actor_id" in entry
            and set(entry) <= {"actor_id", "after_actors", "after_tasks"},
            "INVALID_ARGUMENT",
            "Invalid scene participant",
        )
        actor = validate_contract("UUID", entry["actor_id"])
        require(actor not in slots, "INVALID_ARGUMENT", "Duplicate actor")
        waits = []
        for name in ("after_actors", "after_tasks"):
            identifiers = entry.get(name, [])
            require(
                isinstance(identifiers, list), "INVALID_ARGUMENT", "Expected a prerequisite list"
            )
            waits.append([validate_contract("UUID", value) for value in identifiers])
        slots[actor] = waits
    require(
        all(set(peers) <= slots.keys() for peers, _ in slots.values()),
        "INVALID_ARGUMENT",
        "Prerequisite actor is outside this interaction",
    )
    return slots


def register_scene(
    kernel,
    gateway,
    controller,
    *,
    world_ref,
    story_time,
    scope_ids,
    volume_id,
    scene_limit,
    participants,
    subject_id,
    destination_id,
    budget_id,
    adapter_id,
    reserve_amount,
    token_reservation,
    idempotency_key,
    deadline_seconds=3600,
    alternative_destination_ids=None,
    following_scenes=None,
    allowed_item_gift=None,
):
    """Preauthorize one registered interaction, one location allowance, and its editorial response.

    All references, grants, expenses and waits are local configuration. No model is called
    during setup. The current plan and accepted world must already exist.
    """
    PlanningService._key(idempotency_key)
    validate_contract("WorldRef", world_ref)
    validate_contract("StoryPoint", story_time)
    for value in (volume_id, subject_id, destination_id, budget_id):
        validate_contract("UUID", value)
    require(
        isinstance(scope_ids, list) and scope_ids, "INVALID_ARGUMENT", "Scene scopes are required"
    )
    for value in scope_ids:
        validate_contract("UUID", value)
    require(
        type(scene_limit) is int and 1 <= scene_limit <= 32,
        "INVALID_ARGUMENT",
        "Scene limit must be 1..32",
    )
    require(
        type(deadline_seconds) is int and 1 <= deadline_seconds <= 86400,
        "INVALID_ARGUMENT",
        "Invalid scene deadline",
    )
    alternatives = [] if alternative_destination_ids is None else alternative_destination_ids
    require(
        isinstance(alternatives, list),
        "INVALID_ARGUMENT",
        "Expected a list of alternative locations",
    )
    for place in alternatives:
        validate_contract("UUID", place)
    location_ids = [destination_id, *alternatives]
    require(
        len(set(location_ids)) == len(location_ids) and subject_id not in location_ids,
        "INVALID_ARGUMENT",
        "Participant and authorized locations must be distinct",
    )
    location_options = {"alternative_destination_ids": alternatives} if alternatives else {}
    slots = _participants(participants)
    if allowed_item_gift is not None:
        validate_item_gift(allowed_item_gift, slots)
        location_options["allowed_item_gift"] = allowed_item_gift
    request = dict(
        world_ref=world_ref,
        story_time=story_time,
        scope_ids=scope_ids,
        volume_id=volume_id,
        scene_limit=scene_limit,
        participants=participants,
        subject_id=subject_id,
        destination_id=destination_id,
        adapter_id=adapter_id,
        budget_id=budget_id,
        reserve_amount=reserve_amount,
        token_reservation=token_reservation,
        deadline_seconds=deadline_seconds,
        **location_options,
    )
    if following_scenes is not None:
        from novel_factory.application.scene_continuation import validate_slots
        validate_slots(following_scenes, scene_limit)
        request["following_scenes"] = following_scenes
    wid, bid = world_ref["work_id"], world_ref["branch_id"]
    with kernel.objects.lock(), kernel.db.uow() as repo:
        repo.one("works", {"work_id": wid}, lock="update")
        kernel.authorize(repo, controller, wid, "ADMIN", bid)
        previous = kernel._prior(
            repo, controller, wid, "REGISTER_SCENE_RUNTIME", idempotency_key, request
        )
        if previous:
            return previous
        require(
            kernel.head(repo, wid, bid) == world_ref,
            "HEAD_CONFLICT",
            "Scene requires the accepted HEAD",
        )
        bound = kernel.in_transaction(repo)
        local_gateway = gateway.with_kernel(bound)
        runtime = RuntimeTaskService(bound, local_gateway)
        supervisor = RuntimeSupervisor(bound, local_gateway)
        receipt_key = fingerprint(
            {"scene": idempotency_key, "principal": controller.principal_id, "work": wid}
        )
        plan = repo.one(
            "selections",
            {"work_id": wid, "selection_kind": PlanningService.selection_key(volume_id)},
        )
        manifest = repo.one(
            "selections", {"work_id": wid, "selection_kind": "PLAN_MANIFEST:" + volume_id}
        )
        require(
            plan and manifest,
            "PLAN_SELECTION_CONFLICT",
            "Select the volume plan before registering a scene",
        )
        anchor = repo.one(
            "selections", {"work_id": wid, "selection_kind": _anchor_name(world_ref, volume_id)}
        )
        if anchor:
            anchor_ref = anchor["artifact_ref"]
            frozen = bound.artifact(repo, anchor_ref, wid, kind="REFERENCE_TIMELINE")["payload"]
            require(
                frozen["scene_limit"] == scene_limit,
                "REFERENCE_FROZEN",
                "The volume already has a different limit",
            )
        else:
            anchor_ref = RollingPlanning(bound).enable(
                controller, world_ref, volume_id, scenes=scene_limit, idempotency_key=receipt_key
            )

        def prepare(
            role,
            mode,
            assignment,
            cut,
            subjects,
            *,
            state,
            predicates=None,
            at=story_time,
            refs=(),
            actor=None,
            skill=None,
        ):
            return runtime.prepare(
                controller,
                world_ref,
                role_id=role,
                task_kind=mode,
                skill_name=skill,
                assignment=assignment,
                knowledge_cut=cut,
                actor_id=actor,
                subject_ids=subjects,
                predicates=predicates or ["located_in"],
                input_artifacts=refs,
                story_time=at,
                queue_state=state,
                adapter_id=adapter_id,
                max_amount=reserve_amount,
                token_budget=token_reservation,
                lease_seconds=min(600, deadline_seconds),
                deadline_seconds=deadline_seconds,
            )

        def enroll(job, cut, **route):
            supervisor.enroll(
                controller,
                job.task["task_id"],
                input_ref=job.agent_input["input_ref"],
                manifest_ref=job.manifest_ref,
                knowledge_cut=cut,
                context_tokens=20000,
                budget_id=budget_id,
                reserve_amount=reserve_amount,
                token_reservation=token_reservation,
                **route,
            )

        def wait(job, dependencies):
            supervisor.workflow.wait(
                job.task["task_id"],
                0,
                dependencies,
                work_id=wid,
                branch_id=bid,
                mode="AND",
                authorize_in_tx=lambda conn: bound.authorize(
                    KernelRepository(conn, tables), controller, wid, "ADMIN", bid
                ),
            )

        jobs = {}
        for actor, (peers, other) in slots.items():
            cut = {"plane": "BELIEF", "actor_id": actor, "known_at": story_time}
            job = prepare(
                "character_actor",
                "character_action",
                "依据自己在此刻所知及自身动机，独立作出行动。",
                cut,
                [actor],
                actor=actor,
                state="BLOCKED" if peers or other else "READY",
            )
            jobs[actor] = (job, cut)
        group = WorldSchedulerService(bound).prepare_interaction(
            controller,
            world_ref,
            story_time,
            scope_ids,
            {actor: job.principal.grant_id for actor, (job, _) in jobs.items()},
            receipt_key,
        )
        interaction_id = group["interaction_id"]
        actor_tasks = {actor: job.task["task_id"] for actor, (job, _) in jobs.items()}
        for actor, (job, cut) in jobs.items():
            enroll(job, cut, seal_interaction_id=interaction_id)
            peers, other = slots[actor]
            if peers or other:
                wait(job, [actor_tasks[peer] for peer in peers] + other)
        control = create_control_task(
            bound,
            controller,
            interaction_id,
            subject_id=subject_id,
            destination_id=destination_id,
            deadline_seconds=deadline_seconds,
            **location_options,
        )
        visible = joint_manifest({"subject_id": subject_id, "destination_id": destination_id, **location_options})
        targets = visible["subject_ids"]
        joint = prepare(
            "adjudication_advisor",
            "joint_outcome",
            "根据规则和已齐的密封意图，提出一次联合裁决。",
            {"plane": "WORLD"},
            targets,
            predicates=visible["predicates"],
            refs=[group["interaction_ref"]],
            skill="adjudicate-actions",
            state="BLOCKED",
        )
        enroll(
            joint,
            {"plane": "WORLD"},
            joint={
                "interaction_id": interaction_id,
                "subject_id": subject_id,
                "destination_id": destination_id,
                "control_task_id": control["task_id"],
                **location_options,
            },
        )
        wait(joint, list(actor_tasks.values()))
        plan_cut = {"plane": "PLAN", "plan_manifest_ref": manifest["artifact_ref"]}
        director = prepare(
            "volume_director",
            "volume_review",
            "复核已接受的后果，有重大偏离时请求有限重规划。",
            plan_cut,
            targets,
            predicates=visible["predicates"],
            refs=[plan_cut["plan_manifest_ref"], plan["artifact_ref"], anchor_ref],
            at=after(story_time),
            skill="volume-director-review",
            state="BLOCKED",
        )
        enroll(
            director,
            plan_cut,
            editorial={
                "operation": "request_change",
                "volume_id": volume_id,
                "prior_plan_ref": plan["artifact_ref"],
                "reference_timeline_ref": anchor_ref,
            },
            continue_editorial=True,
        )
        wait(director, [control["task_id"]])
        if following_scenes is not None:
            from novel_factory.application.scene_continuation import authorize_slots
            authorize_slots(bound, repo, controller, director.task["task_id"], request, plan["artifact_ref"])
        result = {
            "interaction_id": interaction_id,
            "actor_tasks": actor_tasks,
            "control_task_id": control["task_id"],
            "adjudicator_task_id": joint.task["task_id"],
            "director_task_id": director.task["task_id"],
            "reference_timeline_ref": anchor_ref,
        }
        bound._receipt(
            repo, controller, world_ref, "REGISTER_SCENE_RUNTIME", idempotency_key, request, result
        )
        return result
