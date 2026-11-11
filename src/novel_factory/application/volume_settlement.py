"""Volume-end receipts and review candidates. No publication or world acceptance authority."""

from uuid import UUID, uuid5

from novel_factory.application.authorization import Principal
from novel_factory.application.continuity import ContinuityService
from novel_factory.application.kernel import require
from novel_factory.application.planning import PlanningService
from novel_factory.domain.errors import DomainError
from novel_factory.domain.scheduling import after
from novel_factory.domain.temporal import point_key
from novel_factory.orchestration.workflow import review_acceptance


def _ref(repo, work, name):
    row = repo.one("selections", {"work_id": work, "selection_kind": name})
    return row and row["artifact_ref"]


def _pin(kernel, repo, owner, work, name, kind, payload):
    ref = kernel._blob(payload)
    kernel._register_artifact(repo, ref, work, payload, kind, owner.principal_id)
    repo.add(
        "selections", {"work_id": work, "selection_kind": name, "artifact_ref": ref, "epoch": 1}
    )
    return ref


def record_scene(kernel, repo, owner, allowance, task, spec, outcome):
    work = task["work_id"]
    director_id = allowance["review_task_id"]
    director = repo.one(
        "tasks", {"task_id": director_id, "work_id": work, "branch_id": task["branch_id"]}
    )
    director_spec = repo.one(
        "task_inputs", {"task_id": director_id, "generation": director["generation"]}
    )["spec"]
    receipt = review_acceptance(repo.conn, director, director_spec)
    require(
        director["state"] == "ACCEPTED"
        and receipt["accepted_event_refs"]
        and receipt["world_ref"] == spec["world_ref"],
        "INVALID_EVIDENCE",
        "Volume settlement requires the exact accepted scene that woke its director",
    )
    prior_ref = _ref(repo, work, "SETTLEMENT_INPUT:" + director_id)
    scenes = (
        kernel.artifact(repo, prior_ref, work, kind="SETTLEMENT_SCENES")["payload"]["scenes"]
        if prior_ref
        else []
    )
    require(
        len(scenes) <= allowance["scene_limit"]
        and (
            not scenes
            or repo.ancestor(scenes[-1]["receipt"]["world_ref"], receipt["world_ref"])
            and scenes[-1]["receipt"]["world_ref"] != receipt["world_ref"]
        ),
        "INVALID_EVIDENCE",
        "Settlement history exceeds the allowance or no longer follows accepted history",
    )
    starts, ends = [], []
    for ref in receipt["accepted_event_refs"]:
        event = repo.one("events", {"work_id": work, **ref})["payload"]["occurrence_time"]
        start, end = event.get("at", event.get("earliest")), event.get("at", event.get("latest"))
        require(
            start is not None and end is not None,
            "TIME_AMBIGUOUS",
            "A settled event needs a resolved story interval",
        )
        starts.append(start)
        ends.append(end)
    interval = {
        "start": min(starts, key=point_key),
        "end": after(max(ends, key=point_key)),
        "bounds": "[)",
    }
    return [
        *scenes,
        {
            "review_task_id": director_id,
            "editorial_task_id": task["task_id"],
            "editorial_decision": outcome["decision"],
            "review_ref": outcome.get("review_ref"),
            "receipt": receipt,
            "story_interval": interval,
        },
    ]


def carry(kernel, repo, owner, task, child_director, scenes):
    _pin(
        kernel,
        repo,
        owner,
        task["work_id"],
        "SETTLEMENT_INPUT:" + child_director,
        "SETTLEMENT_SCENES",
        {"scenes": scenes},
    )


def finish(kernel, repo, owner, task, spec, allowance, scenes):
    wr = spec["world_ref"]
    work, branch, volume = wr["work_id"], wr["branch_id"], allowance["volume_id"]
    key = f"VOLUME_SETTLEMENT:{branch}:{volume}"
    require(
        not _ref(repo, work, key),
        "SETTLEMENT_ALREADY_RECORDED",
        "This volume already has a terminal candidate",
    )
    grant = kernel.authorize(repo, owner, work, "ADMIN", branch)
    selected = _ref(repo, work, PlanningService.selection_key(volume))
    plan = kernel.artifact(repo, selected, work, kind="PLAN_REVISION")["payload"]
    series = allowance["volume_end"]["series_id"]
    body = {
        "series_id": series,
        "volume_id": volume,
        "world_ref": wr,
        "scenes": scenes,
        "reference_timeline_ref": _ref(repo, work, f"REFERENCE_TIMELINE:{branch}:{volume}"),
        "plan_ref": selected,
        "published_prefix_node_ids": plan["published_prefix_node_ids"],
        "prior_continuity_ref": _ref(repo, work, "CONTINUITY:" + series),
        "protected_volume_prefix_ref": _ref(repo, work, "SERIAL_PREFIX:" + volume),
        "release_at_settlement": _ref(repo, work, "RELEASE:" + volume),
        "authorizer": {
            "principal_id": owner.principal_id,
            "grant_id": owner.grant_id,
            "epoch": grant["epoch"],
            "policy_version": spec["policy_version"],
        },
        "authority": "VOLUME_SETTLEMENT_CANDIDATE",
        "world_effects_applied": 0,
    }
    ref = _pin(kernel, repo, owner, work, key, "VOLUME_SETTLEMENT_CANDIDATE", body)
    result = {
        "status": "SETTLEMENT_RECORDED",
        "settlement_ref": ref,
        "accepted_scenes": len(scenes),
    }
    repo.outbox(
        "volume.settlement.recorded",
        "volume",
        volume,
        result,
        "volume-settle:" + ref["artifact_revision_id"],
    )
    if body["release_at_settlement"]:
        prepare_after_release(kernel, repo, wr, volume, body["release_at_settlement"])
    return result


def prepare_after_release(kernel, repo, wr, volume, release_ref):
    """Called by native freeze or the final scene while holding the work and branch locks."""
    work, branch = wr["work_id"], wr["branch_id"]
    settlement = _ref(repo, work, f"VOLUME_SETTLEMENT:{branch}:{volume}")
    if not settlement:
        return
    key = (
        "settlement-continuity:"
        + settlement["artifact_revision_id"]
        + ":"
        + release_ref["artifact_revision_id"]
    )
    prior = repo.one("outbox", {"dedupe_key": key})
    if prior:
        return
    body = kernel.artifact(repo, settlement, work, kind="VOLUME_SETTLEMENT_CANDIDATE")["payload"]
    try:
        with repo.conn.begin_nested():
            owner = Principal(body["authorizer"]["grant_id"], body["authorizer"]["principal_id"])
            grant = kernel.authorize(repo, owner, work, "ADMIN", branch)
            require(
                grant["epoch"] == body["authorizer"]["epoch"]
                and repo.one("works", {"work_id": work})["policy_version"]
                == body["authorizer"]["policy_version"],
                "VIEW_DENIED",
                "Settlement authority changed",
            )
            release = kernel.artifact(repo, release_ref, work, kind="RELEASE_RECORD")["payload"]
            manifest = kernel.artifact(
                repo, release["manifest_ref"], work, kind="RELEASE_MANIFEST"
            )["payload"]
            require(
                release["volume_id"] == volume
                and _ref(repo, work, "RELEASE:" + volume) == release_ref
                and repo.ancestor(body["world_ref"], manifest["world_ref"]),
                "RELEASE_SELECTION_CONFLICT",
                "Release does not cover the closing scene's accepted world",
            )
            release_cut = kernel.revision(repo, manifest["world_ref"])
            prior_ref = body["prior_continuity_ref"]
            prior = (
                kernel.artifact(repo, prior_ref, work, kind="CONTINUITY_SNAPSHOT")["payload"]
                if prior_ref
                else None
            )
            previous = prior["request"] if prior else {"ordered_release_refs": [], "entries": []}
            require(
                not prior or all(v["volume_id"] != volume for v in prior["volumes"]),
                "CONTINUITY_REVISION_REQUIRED",
                "An already published volume requires a separate edition review",
            )
            releases = [*previous["ordered_release_refs"], release_ref]
            entries = list(previous["entries"])
            for scene in body["scenes"]:
                receipt = scene["receipt"]
                sources = [
                    {"world_ref": receipt["world_ref"], "event_ref": ref}
                    for ref in receipt["accepted_event_refs"]
                ]
                require(
                    all(
                        release_cut["selection"].get(e["event_ref"]["occurrence_id"])
                        == e["event_ref"]["event_version_id"]
                        for e in sources
                    ),
                    "CONTINUITY_REVALIDATION_REQUIRED",
                    "Release no longer selects all settled events",
                )
                entry_id = str(
                    uuid5(UUID(settlement["artifact_revision_id"]), scene["review_task_id"])
                )
                entries.append(
                    {
                        "entry_id": entry_id,
                        "thread_id": entry_id,
                        "volume_ordinal": len(releases),
                        "kind": "WORLD_INHERITANCE",
                        "plane": "WORLD_EVIDENCE",
                        "status": "RESOLVED",
                        "entity_ids": [],
                        "story_interval": scene["story_interval"],
                        "interpretation": "本卷场景的联合裁决已正式接受，具体结果以所引事件为据。",
                        "epistemic_status": "EVENT_REFERENCE",
                        "source_events": sources,
                        "source_texts": [],
                        "supersedes_entry_ids": [],
                        "planned_payoff_at": None,
                    }
                )
            request = {
                "schema_version": "2.3",
                "series_id": body["series_id"],
                "operation": "APPEND",
                "world_ref": kernel.head(repo, work, branch),
                "expected_prior_ref": prior_ref,
                "ordered_release_refs": releases,
                "entries": entries,
                "revision_reason": None,
                "idempotency_key": key,
            }
            candidate = ContinuityService(kernel.in_transaction(repo)).prepare(owner, request)
            result = {"status": "AWAITING_INDEPENDENT_REVIEW", "candidate_ref": candidate}
    except DomainError as error:
        result = {"status": "SETTLEMENT_REVIEW_DEFERRED", "reason": error.code}
    repo.outbox(
        "volume.settlement.continuity",
        "volume",
        volume,
        {**result, "settlement_ref": settlement, "release_ref": release_ref},
        key,
    )
