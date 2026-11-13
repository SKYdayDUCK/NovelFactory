"""Simulate a newly planned volume after the exact preceding edition and continuity are published."""

from datetime import datetime

from . import rolling_production as rolling
from .editorial_effects import scoped_accepted_effects
from .idea_intake import _point, _uid, create_planned_window
from .idea_opening import _delivery, enroll_volume_scene
from .kernel import require
from .planning import PlanningService
from .production_pipeline import _artifact
from .volume_successor import _selected


def _bound_prefix(kernel, repo, cursor, plan, wr):
    work, branch = wr["work_id"], wr["branch_id"]
    snapshot_ref = cursor["continuity_ref"]
    require(
        cursor["release_refs"]
        and snapshot_ref
        and snapshot_ref == _selected(repo, work, "CONTINUITY:" + cursor["series_id"]),
        "CONTINUITY_REVALIDATION_REQUIRED",
        "Upcoming simulation needs the published preceding edition",
    )
    snapshot = kernel.artifact(repo, snapshot_ref, work, kind="CONTINUITY_SNAPSHOT")["payload"]
    checkpoint = kernel.artifact(repo, plan["checkpoint"], work, kind="ROLLING_CHECKPOINT")[
        "payload"
    ]
    require(
        checkpoint["run_id"] == cursor["run_id"] == plan["run_id"]
        and cursor["last_checkpoint"] == plan["checkpoint"]
        and checkpoint["world_ref"]["branch_id"] == branch
        and snapshot["request"]["world_ref"] == checkpoint["world_ref"]
        and snapshot["request"]["ordered_release_refs"] == cursor["release_refs"]
        and snapshot["volumes"][-1]["volume_id"] == checkpoint["volume_id"]
        and repo.ancestor(checkpoint["world_ref"], wr),
        "STALE_PLAN",
        "Released prefix or continuity changed",
    )
    return checkpoint


def advance_volume_scene(kernel, repo, gateway, owner, cursor):
    """Return (handled, progress). A running interaction preempts the writer even between receipts."""
    window = rolling._window(kernel, repo, cursor)
    volume = window["volume_id"]
    work, branch = cursor["world_ref"]["work_id"], cursor["world_ref"]["branch_id"]
    planned = _selected(repo, work, "P6_VOLUME_SIM_PLAN:" + volume)
    if not planned:
        return False, None
    plan = kernel.artifact(repo, planned, work, kind="P6_VOLUME_SIM_PLAN")["payload"]
    wr = kernel.head(repo, work, branch)
    checkpoint = _bound_prefix(kernel, repo, cursor, plan, wr)
    source = kernel._prior(repo, owner, work, "P6_IDEA_PROVENANCE", "initial", {})
    require(
        source and source["design_ref"] == plan["source"],
        "INVALID_EVIDENCE",
        "Source design changed",
    )
    candidate = kernel.artifact(repo, plan["plan_output"], work, kind="AGENT_CANDIDATE")["payload"]
    design = kernel.artifact(repo, plan["source"], work, kind="INITIAL_STORY_DESIGN")["payload"]
    key = "P6_VOLUME_SCENE:" + volume
    running = _selected(repo, work, key)
    if not running:
        require(
            checkpoint["world_ref"] == wr
            and plan["plan_ref"] == _selected(repo, work, PlanningService.selection_key(volume)),
            "STALE_PLAN",
            "Simulation must start on the published cut and reviewed plan",
        )
        seconds = int(
            (
                datetime.fromisoformat(cursor["authorization"]["deadline"]) - repo.now()
            ).total_seconds()
        )
        require(seconds > 0, "TASK_EXPIRED", "Simulation allowance expired")
        destinations = {
            "INITIAL_PLACE": _uid(work, "place"),
            "OTHER_PLACE": _uid(work, "other-place"),
            "NO_MOVEMENT": None,
        }
        scene_wr, sim, events, receipts = enroll_volume_scene(
            kernel,
            repo,
            gateway,
            owner,
            source,
            {**cursor["allowance"], "deadline_seconds": seconds},
            wr,
            volume,
            len(candidate["chapter_goals"]),
            scene_key=volume,
            start_tick=plan["start_tick"],
            situation=candidate["scene_situation"],
            environment_destination=destinations[candidate["environment_destination"]],
        )
        value = {
            "plan": planned,
            "continuity": cursor["continuity_ref"],
            "release": cursor["release_refs"][-1],
            "starting_world": wr,
            "scene_world": scene_wr,
            "simulation": sim,
            "environment_events": events,
            "process_receipts": receipts,
        }
        record = _artifact(kernel, repo, work, owner, "P6_VOLUME_SCENE", value)
        PlanningService(kernel)._select(repo, wr, key, record)
        return True, {"status": "VOLUME_SIMULATION_ENROLLED", "scene_ref": record, **sim}
    record = kernel.artifact(repo, running, work, kind="P6_VOLUME_SCENE")["payload"]
    require(
        record["plan"] == planned
        and record["continuity"] == cursor["continuity_ref"]
        and record["release"] == cursor["release_refs"][-1],
        "STALE_PLAN",
        "Simulation inputs changed",
    )
    sim = record["simulation"]
    joint = _delivery(repo, "joint", sim["adjudicator_task_id"])
    if not joint or joint.get("status") != "JOINT_ACCEPTED":
        return True, None
    director = sim["director_task_id"]
    while director:
        delivery = _delivery(repo, "editorial", director)
        if not delivery or delivery.get("status") != "EDITORIAL_DELIVERED":
            return True, None
        director = delivery.get("successor_task_id")
        if not director:
            require(
                delivery.get("decision") in {"APPROVED", "NO_MAJOR_CHANGE"},
                "REVIEW_REQUIRED",
                "Volume plan returned",
            )
    require(
        joint["world_ref"] == wr,
        "HEAD_CONFLICT",
        "Volume scene was overtaken by another world change",
    )
    selected = _selected(repo, work, PlanningService.selection_key(volume))
    adopted = kernel.artifact(repo, selected, work, kind="PLAN_REVISION")["payload"]
    events = [*record["environment_events"], *joint["accepted_event_refs"]]
    at = _point(plan["start_tick"], 5)
    effects = scoped_accepted_effects(kernel, repo, wr, at, cursor["allowance"], events)
    names = kernel.revision(repo, wr)["snapshot"]["entities"]
    facts = []
    for event in effects["accepted_effects"]:
        for effect in event["effects"]:
            dest = effect.get("object_id") or effect.get("object_value", {}).get("entity_id")
            if (
                dest
                and effect.get("predicate") == "located_in"
                and effect.get("operation", "OPEN") != "CLOSE"
            ):
                facts.append(names[effect["subject_id"]]["name"] + "抵达" + names[dest]["name"])
    explanation = (
        "本卷演绎的起始条件："
        + candidate["scene_situation"]
        + "。已裁决的位置变化："
        + ("；".join(dict.fromkeys(facts)) or "没有")
        + "。"
    )
    writing, refs = create_planned_window(
        kernel,
        owner,
        wr,
        _uid(volume, "writing"),
        _uid(work, "protagonist"),
        _uid(work, "place"),
        plan["plan_output"],
        candidate["volume_brief"],
        [n["goal"] for n in adopted["nodes"]],
        design["world_rules"],
        start_after=plan["start_tick"],
        opening_events=events,
        opening_explanation=explanation,
        upstream_refs=[
            planned,
            running,
            selected,
            joint["outcome_ref"],
            cursor["continuity_ref"],
            record["release"],
        ],
    )
    old = []
    for i in range(cursor["index"], len(cursor["windows"])):
        slot = rolling._payload(kernel, repo, wr, cursor["windows"][i], "ROLLING_WINDOW")
        if slot["volume_id"] != volume:
            break
        old.append(i)
    replacements = rolling.partition_volumes([writing], cursor["profile"]["window_chapters"])
    require(
        old and old[0] == cursor["index"] and len(old) == len(replacements),
        "STALE_PLAN",
        "Simulation may replace only this unopened volume's slots",
    )
    for i, replacement in zip(old, replacements, strict=True):
        cursor["windows"][i] = rolling._store(
            kernel, repo, owner, wr, "ROLLING_WINDOW", replacement
        )
    if cursor["final_volume_id"] == volume:
        cursor["final_volume_id"] = writing["volume_id"]
    cursor["reason"] = None
    cursor_ref = rolling._save(kernel, repo, owner, cursor)
    result = {
        "status": "VOLUME_SIMULATION_ACCEPTED",
        "writing_volume": writing["volume_id"],
        "scene_ref": running,
        "joint_outcome_ref": joint["outcome_ref"],
        "world_ref": wr,
        "accepted_event_refs": events,
        "cursor_ref": cursor_ref,
        **refs,
    }
    handoff = _artifact(kernel, repo, work, owner, "P6_VOLUME_SCENE_HANDOFF", result)
    PlanningService(kernel)._select(repo, wr, "P6_VOLUME_SCENE_HANDOFF:" + volume, handoff)
    return True, {**result, "handoff_ref": handoff}
