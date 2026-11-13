"""Connect a reviewed opening design to sealed actors, native world effects and writing."""

from datetime import datetime, timedelta

from ..domain.canonical import fingerprint
from ..domain.propositions import encode_proposition
from ..domain.scheduling import state_from_revision
from ..domain.temporal import point_key
from .authorization import Principal
from .idea_intake import _point, _uid, create_planned_window
from .kernel import require
from .planning import PlanningService
from .production_pipeline import _artifact
from .queries import WorldQueryService
from .rolling_production import register_rolling
from .scene_runtime import register_scene
from .simulation import WorldSchedulerService
from .volume_successor import _selected


def opening_entities(work, design):
    opening = design["opening"]
    require(
        opening["counterpart"] != design["protagonist"]
        and opening["other_place"] != design["place"],
        "INVALID_MODEL_OUTPUT",
        "Opening needs two distinct people and places",
    )
    extra = [
        (_uid(work, "counterpart"), "CHARACTER", opening["counterpart"]),
        (_uid(work, "other-place"), "REGION", opening["other_place"]),
    ]
    if opening.get("environment"):
        extra.append((_uid(work, "moving-entity"), "ITEM", opening["environment"]["moving_entity"]))
    return extra


def add_opening_facts(genesis, work, design, at):
    opening = design["opening"]
    place = _uid(work, "place")
    genesis["cognitions"] = []
    for label, knowledge in (
        ("protagonist", "protagonist_knowledge"),
        ("counterpart", "counterpart_knowledge"),
    ):
        actor = _uid(work, label)
        for kind, statement in (
            ("PRIVATE_DECISION", opening[knowledge]),
            (
                "BELIEF",
                encode_proposition(actor, "located_in", {"kind": "ENTITY", "entity_id": place}),
            ),
        ):
            genesis["cognitions"].append(
                {
                    "record_id": _uid(actor, "opening:" + kind),
                    "actor_id": actor,
                    "record_kind": kind,
                    "acquired_at": at,
                    "statement": statement,
                    "evidence_refs": [],
                }
            )
    for entity in [
        _uid(work, "counterpart"),
        *([_uid(work, "moving-entity")] if opening.get("environment") else []),
    ]:
        genesis["relations"].append(
            {
                "subject_id": entity,
                "predicate": "located_in",
                "object_value": {"kind": "ENTITY", "entity_id": place},
                "valid_time": {"start": at, "end": None, "bounds": "[)"},
            }
        )


def actor_brief(kernel, repo, wr, at, actor):
    """The assignment can include only this character's own frozen opening memory."""
    snapshot = kernel.revision(repo, wr)["snapshot"]
    records = [
        r["statement"]
        for r in snapshot["cognitions"]
        if r["actor_id"] == actor
        and r["record_id"] == _uid(actor, "opening:PRIVATE_DECISION")
        and r["record_kind"] == "PRIVATE_DECISION"
        and point_key(r["acquired_at"]) <= point_key(at)
    ]
    require(len(records) == 1, "INVALID_EVIDENCE", "Actor's opening memory is unavailable")
    return (
        "\n你的身份：" + snapshot["entities"][actor]["name"] + "。你自己的开场记忆：" + records[0]
    )


def adjudication_brief(kernel, repo, wr, interaction_id, targets):
    interaction = repo.one("simulation_interactions", {"interaction_id": interaction_id})["payload"]
    revision = kernel.revision(repo, wr)
    process = state_from_revision(revision)["processes"][interaction["process_ids"][0]][
        "definition"
    ]
    names = {eid: revision["snapshot"]["entities"][eid]["name"] for eid in targets}
    from json import dumps

    return "\n" + dumps(
        {"starting_situation": process["goal"], "authorized_entities": names}, ensure_ascii=False
    )


def _accept(kernel, repo, owner, wr, at, subjects, label, record_ref, propose):
    """One local control task, on one fixed head, with the normal approval and acceptance certificate."""
    work = wr["work_id"]
    task_id = _uid(work, "opening-kernel:" + label)
    manifest = kernel.put_artifact(
        owner, work, {"subject_ids": subjects, "predicates": ["located_in"]}, "INPUT_MANIFEST"
    )
    grant = kernel.authorize(repo, owner, work, "ADMIN", wr["branch_id"])
    spec = {
        "schema_version": "2.0",
        "task_id": task_id,
        "generation": 0,
        "role_id": grant["role_id"],
        "task_kind": "opening_process",
        "state": "READY",
        "world_ref": wr,
        "story_time": at,
        "context_bundle_id": None,
        "policy_version": repo.one("works", {"work_id": work})["policy_version"],
        "dependency_fingerprint": None,
        "runtime_contract_version": fingerprint([wr, label, record_ref]),
        "capability_grant_id": owner.grant_id,
        "input_artifacts": [manifest, record_ref],
        "budget": {"currency": "USD", "max_amount": "0", "token_budget": 20000},
        "wall_deadline": (repo.now() + timedelta(hours=1)).isoformat(),
    }
    kernel.create_task(owner, spec, task_id)
    claim = kernel.claim_task(owner, "opening:" + task_id, task_id=task_id)
    require(claim, "TASK_NOT_ACCEPTABLE", "Opening control task is unavailable")
    gen = claim["generation"]
    compiled = WorldQueryService(kernel).context_compile(
        owner,
        {
            "schema_version": "2.1",
            "task_id": task_id,
            "generation": gen,
            "recipe_id": "ADJUDICATOR",
            "input_manifest_ref": manifest,
            "knowledge_cut": {"plane": "WORLD"},
            "read_contract": {
                "revision": {"mode": "FIXED", "world_ref": wr},
                "process_policy": "ALLOW_PARTIAL",
                "index_policy": "AUTHORITATIVE",
                "max_wait_ms": 0,
            },
            "subject_time": {"kind": "POINT", "at": at},
            "token_budget": 20000,
            "idempotency_key": task_id,
        },
    )
    require(
        compiled["status"] == "READY", "CONTEXT_REQUIRED", "Opening decision needs a fixed context"
    )
    proposal = propose(owner, task_id, gen, record_ref, task_id)
    require(proposal["approval_id"], "REVIEW_REQUIRED", "Opening decision needs separate approval")
    common = {
        "schema_version": "2.1",
        "task_id": task_id,
        "generation": gen,
        "context_bundle_id": proposal["context_bundle_id"],
        "concurrency_mode": "STRICT_HEAD",
        "base_world_ref": wr,
        "proposal_ref": proposal["proposal_ref"],
        "approval_refs": [proposal["approval_id"]],
        "idempotency_key": task_id,
    }
    cert = kernel.prepare(owner, {**common, "dependency_handle": proposal["dependency_handle"]})
    return kernel.accept(
        owner,
        {
            **common,
            "acceptance_certificate_ref": cert,
            "dependency_fingerprint": proposal["dependency_fingerprint"],
        },
    )


def enroll_opening(kernel, repo, gateway, owner, source, registration):
    """Register and accept processes in the same transaction as the opening enrollment."""
    wr = source["world_ref"]
    work, branch = wr["work_id"], wr["branch_id"]
    require(kernel.head(repo, work, branch) == wr, "HEAD_CONFLICT", "Opening genesis changed")
    design = kernel.artifact(repo, source["design_ref"], work, kind="INITIAL_STORY_DESIGN")[
        "payload"
    ]
    opening = design["opening"]
    scheduler = WorldSchedulerService(kernel)
    rules = repo.one("works", {"work_id": work})["rules_ref"]
    first, other = _uid(work, "place"), _uid(work, "other-place")
    actors = [_uid(work, role) for role in ("protagonist", "counterpart")]
    receipts = []

    def register(label, kind, scopes, participants, start, due, goal, **evaluation):
        pid = _uid(work, "opening-process:" + label)
        proc = {
            "schema_version": "2.2",
            "process_id": pid,
            "process_kind": kind,
            "scope_ids": scopes,
            "participant_ids": participants,
            "start_at": start,
            "due_at": due,
            "world_stage": "ACTIVE",
            "high_impact": True,
            "channels": [],
            "goal": goal,
            "rule_evidence_refs": [{"kind": "RULE", "artifact_ref": rules}],
            "trigger_id": _uid(pid, "trigger"),
            "trigger_version_id": _uid(pid, "trigger:1"),
            **evaluation,
        }
        ref = scheduler.stage_process(owner, wr, proc, pid)
        accepted = _accept(
            kernel,
            repo,
            owner,
            wr,
            start,
            list(dict.fromkeys(scopes + participants)),
            label,
            ref,
            scheduler.propose_registration,
        )
        receipts.append(accepted)
        return accepted["world_ref"]

    events = []
    if source["profile"]["environment_processes"]:
        moving = _uid(work, "moving-entity")
        require(
            opening.get("environment"), "INVALID_MODEL_OUTPUT", "No approved environmental process"
        )
        due = _point(0, 2)
        wr = register(
            "environment",
            "TRAVEL",
            [first, other, moving],
            [moving],
            _point(0, 1),
            due,
            opening["environment"]["moving_entity"] + "抵达" + opening["other_place"],
            required_actor_ids=[],
            evaluator_kind="DETERMINISTIC",
            deterministic_commands=[
                {
                    "template_id": "SET_RELATION",
                    "args": {
                        "subject_id": moving,
                        "predicate": "located_in",
                        "object_value": {"kind": "ENTITY", "entity_id": other},
                        "at": due,
                    },
                }
            ],
        )
        outcome = scheduler.evaluate_due(owner, wr, due, [first, other], "opening-environment")
        accepted = _accept(
            kernel,
            repo,
            owner,
            wr,
            due,
            [first, other, moving],
            "evaluate-environment",
            outcome["outcome_ref"],
            scheduler.propose_outcome,
        )
        receipts.append(accepted)
        wr = accepted["world_ref"]
        events = accepted["accepted_event_refs"]
    wr = register(
        "interaction",
        "INTERACTION",
        [first, other],
        actors,
        _point(0, 4),
        _point(0, 5),
        opening["situation"],
        required_actor_ids=actors,
        evaluator_kind="SEMANTIC",
    )
    simulation = register_scene(
        kernel,
        gateway,
        owner,
        world_ref=wr,
        story_time=_point(0, 5),
        scope_ids=[first, other],
        volume_id=source["volumes"][0]["volume_id"],
        scene_limit=len(source["volumes"][0]["chapters"]),
        participants=[{"actor_id": actor} for actor in actors],
        subject_id=actors[0],
        destination_id=other,
        alternative_destination_ids=[first],
        budget_id=registration["budget_id"],
        adapter_id=registration["routes"]["draft"],
        reserve_amount=registration["reserve_amount"],
        token_reservation=registration["token_reservation"],
        deadline_seconds=registration["deadline_seconds"],
        idempotency_key="idea-opening",
        opening_scene=True,
        local_replanning=source["profile"]["local_replanning"],
    )
    grant = kernel.authorize(repo, owner, work, "ADMIN", branch)
    payload = {
        "world_ref": wr,
        "source": source["design_ref"],
        "initial_plan_ref": source["plan_ref"],
        "registration": registration,
        "simulation": simulation,
        "environment_events": events,
        "receipts": receipts,
        "grant_id": owner.grant_id,
        "principal_id": owner.principal_id,
        "epoch": grant["epoch"],
        "policy_version": repo.one("works", {"work_id": work})["policy_version"],
        "deadline": (repo.now() + timedelta(seconds=registration["deadline_seconds"])).isoformat(),
    }
    ref = _artifact(kernel, repo, work, owner, "P6_OPENING", payload)
    PlanningService(kernel)._select(repo, wr, "P6_OPENING:" + branch, ref)
    return {"status": "OPENING_ENROLLED", "opening_ref": ref, **simulation}


def _delivery(repo, route, tid):
    task = repo.one("tasks", {"task_id": tid})
    event = repo.one("outbox", {"dedupe_key": f"{route}:{tid}:{task['generation']}"})
    return event and event["payload"]


def advance_opening(kernel, gateway, controller, work, branch):

    with kernel.db.uow(write=False) as repo:
        if not _selected(repo, work, "P6_OPENING:" + branch) or _selected(
            repo, work, "P6_OPENING_DONE:" + branch
        ):
            return None
    with kernel.objects.lock(), kernel.db.uow() as repo:
        repo.one("works", {"work_id": work}, lock="update")
        kernel.authorize(repo, controller, work, "ADMIN", branch)
        if _selected(repo, work, "P6_OPENING_DONE:" + branch):
            return None
        ref = _selected(repo, work, "P6_OPENING:" + branch)
        opening = kernel.artifact(repo, ref, work, kind="P6_OPENING")["payload"]
        owner = Principal(opening["grant_id"], opening["principal_id"])
        grant = kernel.authorize(repo, owner, work, "ADMIN", branch)
        require(
            grant["epoch"] == opening["epoch"]
            and opening["policy_version"] == repo.one("works", {"work_id": work})["policy_version"],
            "VIEW_DENIED",
            "Opening authority changed",
        )
        sim = opening["simulation"]
        joint = _delivery(repo, "joint", sim["adjudicator_task_id"])
        if not joint or joint.get("status") != "JOINT_ACCEPTED":
            return None
        tid = sim["director_task_id"]
        verdict = None
        while tid:
            verdict = _delivery(repo, "editorial", tid)
            if not verdict or verdict.get("status") != "EDITORIAL_DELIVERED":
                return None
            tid = verdict.get("successor_task_id")
        if verdict and verdict.get("decision") not in {"NO_MAJOR_CHANGE", "APPROVED"}:
            key = "p6-opening-decision:" + verdict["task_id"] + ":" + str(verdict["generation"])
            if repo.one("outbox", {"dedupe_key": key}):
                return None
            failure = {
                "status": "OPENING_REVIEW_REQUIRED",
                "decision": verdict.get("decision"),
                "review_task_id": verdict["task_id"],
                "opening_ref": ref,
            }
            repo.outbox("production.opening.blocked", "branch", branch, failure, key)
            return failure
        wr = joint["world_ref"]
        require(
            kernel.head(repo, work, branch) == wr,
            "HEAD_CONFLICT",
            "Opening decisions need a fresh writing cut",
        )
        old = opening["registration"]
        plan_ref = _selected(
            repo, work, PlanningService.selection_key(old["volumes"][0]["volume_id"])
        )
        plan = kernel.artifact(repo, plan_ref, work, kind="PLAN_REVISION")["payload"]
        design = kernel.artifact(repo, opening["source"], work, kind="INITIAL_STORY_DESIGN")[
            "payload"
        ]
        effect = kernel.artifact(repo, joint["outcome_ref"], work, kind="SIMULATION_OUTCOME")[
            "payload"
        ]
        snapshot = kernel.revision(repo, wr)["snapshot"]
        facts = "开场行动已裁决。"
        for command in effect["commands"]:
            args = command["args"]
            require(
                command["template_id"] == "SET_RELATION" and args["predicate"] == "located_in",
                "INVALID_EVIDENCE",
                "Unexpected effect outside the opening allowance",
            )
            character = snapshot["entities"][args["subject_id"]]["name"]
            location = snapshot["entities"][args["object_value"]["entity_id"]]["name"]
            facts += character + "移动到" + location + "。"
        if not effect["commands"]:
            facts += "本次行动没有改变人物所在地点。"

        if opening["environment_events"]:
            facts += (
                "。已发生的环境变化："
                + design["opening"]["environment"]["moving_entity"]
                + "抵达"
                + design["opening"]["other_place"]
            )
        # The simulation plan is a retained editorial source. The first writing volume receives its
        # own frozen plan, explicitly bound to the accepted world and the reviewed planning lineage.
        bound = kernel.in_transaction(repo)
        events = [*opening["environment_events"], *joint["accepted_event_refs"]]
        volume, refs = create_planned_window(
            bound,
            owner,
            wr,
            _uid(work, "opening-writing-volume"),
            _uid(work, "protagonist"),
            _uid(work, "place"),
            opening["source"],
            design["storyline"],
            [node["goal"] for node in plan["nodes"]],
            design["world_rules"],
            opening_events=events,
            opening_explanation=facts,
            upstream_refs=[plan_ref, ref, joint["outcome_ref"]],
        )
        remaining = int((datetime.fromisoformat(opening["deadline"]) - repo.now()).total_seconds())
        require(remaining > 0, "TASK_EXPIRED", "Opening authorization expired")
        written = register_rolling(
            bound,
            gateway.with_kernel(bound),
            owner,
            **{**old, "world_ref": wr, "volumes": [volume], "deadline_seconds": remaining},
        )
        result = {
            "status": "OPENING_ACCEPTED",
            "world_ref": wr,
            "accepted_event_refs": events,
            "opening_ref": ref,
            "joint_outcome_ref": joint["outcome_ref"],
            "reviewed_plan_ref": plan_ref,
            "writing_registration": written,
            **refs,
        }
        done = _artifact(bound, repo, work, owner, "P6_OPENING_HANDOFF", result)
        PlanningService(bound)._select(repo, wr, "P6_OPENING_DONE:" + branch, done)
        return result
