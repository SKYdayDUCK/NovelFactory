"""A durable cursor over small native production boards and immutable chapter receipts."""

from datetime import datetime, timedelta

from sqlalchemy import text

from ..contracts.registry import validate_contract
from ..domain.canonical import fingerprint
from ..domain.errors import DomainError
from . import production_pipeline as boards
from .kernel import require
from .planning import PlanningService
from .release import _load_acceptance
from .volume_successor import _selected


def partition_volumes(volumes, size):
    boards._bounded(size, 32, "Writing window must contain 1..32 chapters")
    require(isinstance(volumes, list) and volumes, "INVALID_ARGUMENT", "Missing planned volumes")
    volume_ids, chapter_ids, scenes = set(), set(), set()
    windows = []
    for volume in volumes:
        boards._fields(volume, "volume_id chapters")
        vid = validate_contract("UUID", volume["volume_id"])
        require(vid not in volume_ids, "INVALID_ARGUMENT", "Volume appears twice")
        volume_ids.add(vid)
        chapters = volume["chapters"]
        require(isinstance(chapters, list) and chapters, "INVALID_ARGUMENT", "Empty volume")
        for chapter in chapters:
            boards._fields(chapter, "chapter_id scenes")
            cid = validate_contract("UUID", chapter["chapter_id"])
            require(cid not in chapter_ids, "INVALID_ARGUMENT", "Chapter appears twice")
            chapter_ids.add(cid)
            for slot in boards._list(chapter["scenes"], 32, "Invalid chapter scenes", 1):
                boards._fields(slot, "scene_ref handoff_refs")
                ref = validate_contract("ArtifactRef", slot["scene_ref"])
                require(
                    ref["artifact_revision_id"] not in scenes,
                    "INVALID_ARGUMENT",
                    "Scene card reused",
                )
                scenes.add(ref["artifact_revision_id"])
                for handoff in boards._list(slot["handoff_refs"], 32, "Too many predecessors"):
                    validate_contract("ArtifactRef", handoff)
        # A native board holds at most 128 scene and chapter-review nodes.
        group, nodes = [], 0
        for chapter in chapters:
            weight = 3 * len(chapter["scenes"]) + 1
            if len(group) == size or nodes + weight > 128:
                windows.append({"volume_id": vid, "chapters": group})
                group, nodes = [], 0
            group.append(chapter)
            nodes += weight
        windows.append({"volume_id": vid, "chapters": group})
    return windows


def _cursor_key(branch):
    return "P6_ROLLING:" + branch


def _store(kernel, repo, principal, wr, kind, data):
    return boards._artifact(kernel, repo, wr["work_id"], principal, kind, data)


def _payload(kernel, repo, wr, ref, kind):
    return kernel.artifact(repo, ref, wr["work_id"], kind=kind)["payload"]


def _save(kernel, repo, principal, cursor):
    wr = cursor["world_ref"]
    ref = _store(kernel, repo, principal, wr, "ROLLING_CURSOR", cursor)
    PlanningService(kernel)._select(repo, wr, _cursor_key(wr["branch_id"]), ref)
    repo.outbox(
        "production.rolling.changed",
        "branch",
        wr["branch_id"],
        {"cursor_ref": ref, "state": cursor["state"]},
        "rolling:" + ref["artifact_revision_id"],
    )
    repo.conn.execute(text("SELECT pg_notify('nf_supervisor_wake', '')"))
    return ref


def _load(kernel, repo, work, branch):
    ref = _selected(repo, work, _cursor_key(branch))
    return (
        None if ref is None else kernel.artifact(repo, ref, work, kind="ROLLING_CURSOR")["payload"]
    )


def _window(kernel, repo, cursor):
    return _payload(
        kernel, repo, cursor["world_ref"], cursor["windows"][cursor["index"]], "ROLLING_WINDOW"
    )


def _scene_extents(length, chapters, first_chapter, total_chapters):
    """Split inclusive manuscript bounds in reading order, preserving the two totals exactly."""
    if not isinstance(length, dict) or length.get("unit") != "han_characters":
        return {}

    def share(total, index, count):
        quotient, remainder = divmod(total, count)
        return quotient + int(index < remainder)

    extents = {}
    for index, chapter in enumerate(chapters, start=first_chapter):
        scenes = chapter["scenes"]
        lower = share(length["min"], index, total_chapters)
        upper = share(length["max"], index, total_chapters)
        for ordinal, scene in enumerate(scenes):
            extents[scene["scene_ref"]["artifact_revision_id"]] = {
                "min": share(lower, ordinal, len(scenes)),
                "max": share(upper, ordinal, len(scenes)),
            }
    return extents


def rolling_status(kernel, owner, work, branch):
    with kernel.db.uow(write=False, repeatable_read=True) as repo:
        kernel.authorize(repo, owner, work, "ADMIN", branch)
        cursor = _load(kernel, repo, work, branch)
        require(cursor, "INVALID_ARGUMENT", "No rolling allowance")
        return {
            **{
                k: cursor[k]
                for k in (
                    "world_ref",
                    "series_id",
                    "state",
                    "index",
                    "window_count",
                    "target_chapters",
                    "chapter_count",
                    "final_volume_id",
                    "frozen_chapters",
                    "last_checkpoint",
                    "reason",
                    "profile",
                    "profile_ref",
                    "dag_ref",
                    "release_refs",
                    "run_id",
                )
            },
            "window_refs": cursor["windows"],
        }


def _open(kernel, repo, gateway, owner, cursor):
    window = _window(kernel, repo, cursor)
    request = dict(cursor["allowance"])
    wr = kernel.head(repo, cursor["world_ref"]["work_id"], cursor["world_ref"]["branch_id"])
    remaining = int(
        (datetime.fromisoformat(cursor["authorization"]["deadline"]) - repo.now()).total_seconds()
    )
    require(remaining > 0, "TASK_EXPIRED", "Rolling allowance expired")
    seen = set()
    first_ordinal = None
    ordering = []
    ends = []
    for chapter in window["chapters"]:
        for slot in chapter["scenes"]:
            card = _payload(kernel, repo, wr, slot["scene_ref"], "SCENE_PLAN")["scene_contract"]
            require(
                card["scene_id"] not in seen
                and not _selected(repo, wr["work_id"], "P6_FROZEN_SCENE:" + card["scene_id"]),
                "SERIAL_PREFIX_PROTECTED",
                "A registered scene was already frozen",
            )
            seen.add(card["scene_id"])
            ordering.append(card["reader_cut_ordinal"])
            ends.append(card["story_interval"]["end"])
    from ..domain.temporal import point_key

    end = max(ends, key=point_key)
    require(
        point_key(end) <= point_key(request["story_time"]),
        "VIEW_DENIED",
        "Writing window exceeds the authorized story horizon",
    )
    request["story_time"] = end
    if cursor["last_checkpoint"]:
        previous = _payload(kernel, repo, wr, cursor["last_checkpoint"], "ROLLING_CHECKPOINT")
        if previous["volume_id"] == window["volume_id"]:
            last_scene = previous["chapters"][-1]["scenes"][-1]
            first_ordinal = _payload(kernel, repo, wr, last_scene["scene_ref"], "SCENE_PLAN")[
                "scene_contract"
            ]["reader_cut_ordinal"]
    require(
        ordering == sorted(set(ordering))
        and (first_ordinal is None or first_ordinal < ordering[0]),
        "RELEASE_ORDER_MISMATCH",
        "Planned chapter order differs from scene reader order",
    )
    extent_ref = _selected(repo, wr["work_id"], "P6_EXTENT:" + wr["branch_id"])
    length = _payload(kernel, repo, wr, extent_ref, "MANUSCRIPT_EXTENT")["length"] if extent_ref else None
    result = boards._register(
        kernel,
        gateway,
        owner,
        world_ref=wr,
        series_id=cursor["series_id"],
        **window,
        future=[],
        **request,
        deadline_seconds=remaining,
        idempotency_key="rolling-open:" + cursor["run_id"] + ":" + str(cursor["index"]),
        rolling={"run_id": cursor["run_id"], "index": cursor["index"]},
        max_reworks=cursor.get("max_reworks", 0),
        quality=cursor["profile"].get("production_mode") == "quality",
        scene_extents=_scene_extents(length, window["chapters"], cursor["frozen_chapters"],
                                     cursor["chapter_count"]),
        continuity_ref=cursor["continuity_ref"]
        if cursor["profile"]["cross_volume_memory"]
        else None,
    )
    cursor.update(state="WRITING", reason=None)
    return result


def register_rolling(
    kernel,
    gateway,
    owner,
    *,
    profile,
    world_ref,
    series_id,
    volumes,
    story_time,
    subject_ids,
    predicates,
    routes,
    budget_id,
    reserve_amount,
    token_reservation,
    deadline_seconds,
    idempotency_key,
    target_chapters=None,
):
    from .production_profiles import bind_profile, resolve_profile

    validate_contract("WorldRef", world_ref)
    validate_contract("UUID", series_id)
    PlanningService._key(idempotency_key)
    boards._bounded(deadline_seconds, 86400, "A rolling allowance expires in at most 24 hours")
    require(
        isinstance(profile, dict)
        and {"preset", "genre"} <= profile.keys()
        and profile.keys() <= {"preset", "genre", "overrides", "complexity"},
        "INVALID_ARGUMENT",
        "Invalid production profile",
    )
    effective = resolve_profile(**profile)
    windows = partition_volumes(volumes, effective["window_chapters"])
    initial_chapters = sum(len(w["chapters"]) for w in windows)
    requested_extent = target_chapters
    # Bare receipt keys can be reused by distinct controllers or works.
    run_id = fingerprint([world_ref, owner.principal_id, idempotency_key])
    allowance = {
        "story_time": story_time,
        "subject_ids": subject_ids,
        "predicates": predicates,
        "routes": routes,
        "budget_id": budget_id,
        "reserve_amount": reserve_amount,
        "token_reservation": token_reservation,
        "limits": {
            "draft": effective["writing_concurrency"],
            "review": effective["review_concurrency"],
            "lookahead": effective["future_volume_lookahead"],
            "plan": 1,
            "review_backlog": effective["review_concurrency"],
            "window_goals": effective["window_chapters"],
        },
    }
    request = {
        "world_ref": world_ref,
        "series_id": series_id,
        "profile": profile,
        "target_chapters": requested_extent,
        "volumes": volumes,
        "allowance": allowance,
        "deadline_seconds": deadline_seconds,
    }
    work, branch = world_ref["work_id"], world_ref["branch_id"]
    with kernel.objects.lock(), kernel.db.uow() as repo:
        repo.one("works", {"work_id": work}, lock="update")
        kernel.authorize(repo, owner, work, "ADMIN", branch)
        prior = kernel._prior(repo, owner, work, "ROLLING_REGISTER", idempotency_key, request)
        if prior:
            return prior
        require(
            kernel.head(repo, work, branch) == world_ref,
            "STALE_PLAN",
            "Rolling input world changed",
        )
        provenance = kernel._prior(repo, owner, work, "P6_IDEA_PROVENANCE", "initial", {})
        extent = (provenance or {}).get("creative_input", {}).get("length", {})
        if isinstance(extent, dict) and extent.get("unit") == "chapters":
            from .production_extent import within_length
            target_chapters = extent["min"] if requested_extent is None else requested_extent
            require(
                within_length(extent, target_chapters),
                "INVALID_ARGUMENT",
                "Target exceeds the approved chapter range",
            )
        else:
            target_chapters = initial_chapters if requested_extent is None else requested_extent
            require(
                target_chapters == initial_chapters,
                "VIEW_DENIED",
                "Additional chapters require a chapter-scoped native intake",
            )
        require(
            type(target_chapters) is int and target_chapters >= initial_chapters,
            "INVALID_ARGUMENT",
            "Target is shorter than the registered plan",
        )
        refs = bind_profile(kernel, repo, owner, world_ref, effective)
        old = _load(kernel, repo, work, branch)
        require(
            old is None or old["state"] == "COMPLETED",
            "CAPACITY_LIMIT",
            "A rolling allowance is active",
        )
        grant = kernel.authorize(repo, owner, work, "ADMIN", branch)
        cursor = {
            "world_ref": world_ref,
            "series_id": series_id,
            "index": 0,
            "run_id": run_id,
            **refs,
            "continuity_ref": None,
            "profile": effective,
            "final_volume_id": volumes[-1]["volume_id"],
            "allowance": allowance,
            **({"max_reworks": effective["max_reworks"]} if (provenance or {}).get("model_routes") else {}),
            "state": "STARTING",
            "reason": None,
            "chapter_count": initial_chapters,
            "target_chapters": target_chapters,
            "frozen_chapters": 0,
            "window_count": len(windows),
            "last_checkpoint": None,
            "release_refs": [],
            "authorization": {
                "grant_id": owner.grant_id,
                "principal_id": owner.principal_id,
                "epoch": grant["epoch"],
                "policy_version": repo.one("works", {"work_id": work})["policy_version"],
                "deadline": (repo.now() + timedelta(seconds=deadline_seconds)).isoformat(),
            },
            "windows": [
                _store(kernel, repo, owner, world_ref, "ROLLING_WINDOW", w) for w in windows
            ],
        }
        _open(kernel.in_transaction(repo), repo, gateway, owner, cursor)
        result = {
            "status": "ROLLING_REGISTERED",
            "cursor_ref": _save(kernel, repo, owner, cursor),
            "run_id": run_id,
            "windows": len(windows),
            "chapters": cursor["chapter_count"],
        }
        kernel._receipt(
            repo, owner, world_ref, "ROLLING_REGISTER", idempotency_key, request, result
        )
        return result


def _freeze_window(kernel, repo, owner, cursor, board, window):
    wr = board["world_ref"]
    head = kernel.head(repo, wr["work_id"], wr["branch_id"])
    records = []
    for chapter in window["chapters"]:
        review = board["nodes"]["review:" + chapter["chapter_id"]]
        require(
            review["state"] == "ADVISORY_REVIEW", "APPROVAL_REQUIRED", "Chapter reading pending"
        )
        advice = _payload(kernel, repo, wr, review["output_ref"], "CHAPTER_READING_ADVICE")
        scenes = []
        for node_id in review["parents"]:
            node = board["nodes"][node_id]
            sid = node_id.split(":", 1)[1]
            selected = _selected(repo, wr["work_id"], "NARRATIVE_SCENE:" + sid)
            require(
                selected, "NARRATIVE_ALIGNMENT_REQUIRED", "Current narrative acceptance pending"
            )
            facts = board["nodes"]["facts:" + sid]
            require(
                facts["state"] == "FACTS_ACCEPTED",
                facts.get("reason", "NARRATIVE_ALIGNMENT_REQUIRED"),
                "The independent factual review has not passed",
            )
            report = _payload(kernel, repo, wr, facts["output_ref"], "ROLLING_FACT_REPORT")
            require(
                report["acceptance_ref"] == selected and report["status"] == "CLEAR",
                "NARRATIVE_ALIGNMENT_REQUIRED",
                "Factual report does not match the accepted draft",
            )
            if "knowledge_node" in facts:
                from .rolling_facts import check_knowledge

                knowledge_ref = check_knowledge(kernel, repo, board, facts, node["output_ref"])
                require(
                    report.get("knowledge_ref") == knowledge_ref,
                    "EPISTEMIC_REVIEW_REQUIRED",
                    "Factual acceptance must cite the reviewed text",
                )
            accepted, _ = _load_acceptance(repo, kernel, owner, selected, head)
            reviewed_draft_ref = node["output_ref"]
            reviewed_draft = _payload(kernel, repo, wr, reviewed_draft_ref, "NARRATIVE_DRAFT")
            require(
                reviewed_draft["task_id"] == node["task_id"]
                and reviewed_draft["generation"] == node["generation"]
                and accepted["draft_ref"] == reviewed_draft_ref
                and accepted["scene_ref"] == node["scene_ref"],
                "NARRATIVE_SELECTION_CONFLICT",
                "Accepted text differs from this reviewed board",
            )
            scenes.append(
                {
                    "scene_id": sid,
                    "scene_ref": node["scene_ref"],
                    "draft_ref": node["output_ref"],
                    "text_ref": accepted["text_ref"],
                    "acceptance_ref": selected,
                    **({"knowledge_ref": report["knowledge_ref"]} if "knowledge_ref" in report else {}),
                }
            )
        require(
            advice["draft_refs"] == [s["draft_ref"] for s in scenes],
            "APPROVAL_REQUIRED",
            "Chapter reader saw another draft",
        )
        records.append(
            {
                "chapter_id": chapter["chapter_id"],
                "ordinal": cursor["frozen_chapters"] + len(records) + 1,
                "reading_ref": review["output_ref"],
                "scenes": scenes,
            }
        )
    checkpoint = {
        "world_ref": head,
        "run_id": cursor["run_id"],
        "series_id": cursor["series_id"],
        "volume_id": window["volume_id"],
        "index": cursor["index"],
        "previous": cursor["last_checkpoint"],
        "chapters": records,
    }
    ref = _store(kernel, repo, owner, wr, "ROLLING_CHECKPOINT", checkpoint)
    for record in records:
        for s in record["scenes"]:
            key = "P6_FROZEN_SCENE:" + s["scene_id"]
            require(
                not _selected(repo, wr["work_id"], key),
                "NARRATIVE_SELECTION_CONFLICT",
                "Scene already frozen",
            )
            PlanningService(kernel)._select(repo, wr, key, ref)
    cursor.update(last_checkpoint=ref, frozen_chapters=cursor["frozen_chapters"] + len(records))


def protect_chapter(kernel, repo, wr, scene_id, draft_ref):
    ref = _selected(repo, wr["work_id"], "P6_FROZEN_SCENE:" + scene_id)
    if ref:
        checkpoint = _payload(kernel, repo, wr, ref, "ROLLING_CHECKPOINT")
        require(
            any(
                s["scene_id"] == scene_id and s["draft_ref"] == draft_ref
                for c in checkpoint["chapters"]
                for s in c["scenes"]
            ),
            "SERIAL_PREFIX_PROTECTED",
            "A completed rolling chapter has a frozen draft",
        )


def frozen_volume_scenes(kernel, repo, cursor, volume):
    """Ordered chapter/scene input to the currently frozen volume's publisher and blind readers."""
    wr = cursor["world_ref"]
    chunks, prev = [], cursor["last_checkpoint"]
    while prev:
        checkpoint = _payload(kernel, repo, wr, prev, "ROLLING_CHECKPOINT")
        if checkpoint["volume_id"] != volume:
            break
        chunks.append([s for c in checkpoint["chapters"] for s in c["scenes"]])
        prev = checkpoint["previous"]
    return [s for chunk in reversed(chunks) for s in chunk]


def _verify_volume(kernel, repo, cursor, volume, *, crossing):
    wr = cursor["world_ref"]
    work = wr["work_id"]
    ref = _selected(repo, work, "RELEASE:" + volume)
    require(ref, "RELEASE_INCOMPLETE", "Volume awaits its internal release")
    record = _payload(kernel, repo, wr, ref, "RELEASE_RECORD")
    manifest = _payload(kernel, repo, wr, record["manifest_ref"], "RELEASE_MANIFEST")
    require(
        manifest["world_ref"]["branch_id"] == wr["branch_id"],
        "BRANCH_CHANGED",
        "Release branch differs",
    )
    ordered = frozen_volume_scenes(kernel, repo, cursor, volume)
    require(
        record["ordered_draft_refs"] == [s["draft_ref"] for s in ordered]
        and record["ordered_scene_refs"] == [s["scene_ref"] for s in ordered]
        and [i["artifact_ref"] for i in manifest["ordered_artifacts"]]
        == [s["text_ref"] for s in ordered],
        "RELEASE_INCOMPLETE",
        "Release must cover the exact reviewed chapter prefix in order",
    )
    if crossing:
        published = _selected(repo, work, "CONTINUITY:" + cursor["series_id"])
        require(
            published,
            "CONTINUITY_REVALIDATION_REQUIRED",
            "Cross-volume continuity is not published",
        )
        snapshot = _payload(kernel, repo, wr, published, "CONTINUITY_SNAPSHOT")
        require(
            snapshot["request"]["world_ref"] == kernel.head(repo, work, wr["branch_id"])
            and [item["release_ref"] for item in snapshot["volumes"]]
            == [*cursor["release_refs"], ref],
            "CONTINUITY_REVALIDATION_REQUIRED",
            "Continuity does not match the frozen series",
        )
        from .rolling_publication import require_volume_memory

        require_volume_memory(cursor, snapshot["request"])
        cursor["continuity_ref"] = published
    cursor["release_refs"].append(ref)


def advance_rolling(kernel, gateway, controller, work, branch):
    with kernel.db.uow(write=False) as repo:
        if not _selected(repo, work, _cursor_key(branch)):
            return None
    with kernel.objects.lock(), kernel.db.uow() as repo:
        repo.one("works", {"work_id": work}, lock="update")
        kernel.authorize(repo, controller, work, "ADMIN", branch)
        cursor = _load(kernel, repo, work, branch)
        if cursor["state"] == "COMPLETED":
            return None
        previous = fingerprint(cursor)
        try:
            with repo.conn.begin_nested():
                owner = boards._current_owner(kernel, repo, cursor)
                window = _window(kernel, repo, cursor)
                if cursor["state"] in ("WRITING", "WAITING_ACCEPTANCE"):
                    board = boards._load(kernel, repo, work, branch)
                    require(
                        board
                        and board.get("rolling")
                        == {"run_id": cursor["run_id"], "index": cursor["index"]},
                        "STALE_PLAN",
                        "Rolling board changed",
                    )
                    _freeze_window(kernel, repo, owner, cursor, board, window)
                    cursor.update(state="FROZEN", reason=None)
                elif cursor["state"] == "OPEN_NEXT":
                    from .idea_volume_scene import advance_volume_scene

                    bound = kernel.in_transaction(repo)
                    handled, result = advance_volume_scene(bound, repo, gateway.with_kernel(bound), owner, cursor)
                    if handled:
                        return result
                    _open(bound, repo, gateway, owner, cursor)
                else:
                    final = cursor["index"] + 1 == cursor["window_count"]
                    require(
                        not final or cursor["chapter_count"] == cursor["target_chapters"],
                        "NEXT_PLAN_REQUIRED",
                        "An approved next-volume plan is required",
                    )
                    next_window = (
                        None
                        if final
                        else _payload(
                            kernel,
                            repo,
                            cursor["world_ref"],
                            cursor["windows"][cursor["index"] + 1],
                            "ROLLING_WINDOW",
                        )
                    )
                    crossing = not final and next_window["volume_id"] != window["volume_id"]
                    if crossing:
                        from .rolling_publication import advance_approved_publication

                        result = advance_approved_publication(
                            kernel.in_transaction(repo), repo, owner, cursor, window["volume_id"], gateway=gateway
                        )
                        if result:
                            return result
                    if final or crossing:
                        _verify_volume(kernel, repo, cursor, window["volume_id"], crossing=crossing)
                    if final:
                        cursor.update(state="COMPLETED", reason=None)
                    else:
                        cursor.update(index=cursor["index"] + 1, state="OPEN_NEXT", reason=None)
        except DomainError as exc:
            # No partial checkpoint survives a failed transaction. Retry only after evidence/authority changes.
            cursor = _load(kernel, repo, work, branch)
            if cursor["state"] == "WRITING":
                cursor["state"] = "WAITING_ACCEPTANCE"
            cursor["reason"] = exc.code
        if fingerprint(cursor) == previous:
            return None
        ref = _save(kernel, repo, controller, cursor)
        return {
            "status": "ROLLING_HANDOFF",
            "state": cursor["state"],
            "reason": cursor["reason"],
            "index": cursor["index"],
            "frozen_chapters": cursor["frozen_chapters"],
            "cursor_ref": ref,
        }


def retry_rolling(kernel, owner, *, world_ref, run_id, window_index, node_id, idempotency_key):
    """Retry precisely one failed node only when all of its attempts are proven unsent."""
    validate_contract("WorldRef", world_ref)
    PlanningService._key(idempotency_key)
    require(
        isinstance(node_id, str) and isinstance(run_id, str) and type(window_index) is int,
        "INVALID_ARGUMENT",
        "Invalid retry identity",
    )
    work, branch = world_ref["work_id"], world_ref["branch_id"]
    request = {
        "world_ref": world_ref,
        "run_id": run_id,
        "window_index": window_index,
        "node_id": node_id,
    }
    with kernel.objects.lock(), kernel.db.uow() as repo:
        repo.one("works", {"work_id": work}, lock="update")
        kernel.authorize(repo, owner, work, "ADMIN", branch)
        prior = kernel._prior(repo, owner, work, "ROLLING_RETRY", idempotency_key, request)
        if prior:
            return prior
        cursor = _load(kernel, repo, work, branch)
        require(
            cursor
            and cursor["run_id"] == run_id
            and cursor["index"] == window_index
            and cursor["state"] in {"WRITING", "WAITING_ACCEPTANCE"}
            and world_ref == kernel.head(repo, work, branch),
            "STALE_GENERATION",
            "Rolling window changed",
        )
        boards._current_owner(kernel, repo, cursor)
        board = boards._load(kernel, repo, work, branch)
        node = board["nodes"].get(node_id)
        require(
            node
            and node["state"] == "STOPPED"
            and board.get("rolling") == {"run_id": run_id, "index": window_index},
            "TASK_NOT_ACCEPTABLE",
            "Only a stopped current node can retry",
        )
        history = node.get("history", [])
        require(
            (cursor["profile"].get("production_mode") == "quality" and
             node.get("reason") not in {event.get("reason") for event in history})
            or len(history) < cursor["profile"]["max_reworks"],
            "CAPACITY_LIMIT",
            "Node retry allowance exhausted",
        )
        if "task_id" in node:
            sent = repo.conn.scalar(
                text("""
                SELECT EXISTS(SELECT FROM nf_call_attempts c WHERE c.task_id=:task AND
                  (c.state IN ('PREPARED','SENT','UNKNOWN') OR NOT EXISTS
                   (SELECT FROM nf_runtime_unsent u WHERE u.attempt_id=c.attempt_id)))
            """),
                {"task": node["task_id"]},
            )
            require(
                not sent,
                "CALL_RECONCILIATION_REQUIRED",
                "A previous attempt may have reached the provider",
            )
            old_task = repo.one("tasks", {"task_id": node["task_id"]}, lock="update")
            require(
                old_task["state"] in {"CANCELLED", "FAILED", "STALE", "PAUSED"},
                "TASK_NOT_ACCEPTABLE",
                "The earlier generation is still live",
            )
        history.append(
            {
                key: node[key]
                for key in ("reason", "task_id", "generation", "enrolled_at")
                if key in node
            }
        )
        for key in ("reason", "task_id", "generation", "enrolled_at"):
            node.pop(key, None)
        node.update(state="PENDING", history=history)
        for other in board["nodes"].values():
            if (
                other["state"] == "STOPPED"
                and other.get("reason") == "PREDECESSOR_STOPPED"
                and (node_id in other.get("parents", []) or node_id == other.get("knowledge_node"))
            ):
                other.update(state="PENDING")
                del other["reason"]
        result = {
            "status": "QUEUED",
            "node_id": node_id,
            "board_ref": boards._save(kernel, repo, owner, board),
        }
        kernel._receipt(repo, owner, world_ref, "ROLLING_RETRY", idempotency_key, request, result)
        repo.conn.execute(text("SELECT pg_notify('nf_supervisor_wake', '')"))
        return result


def append_generated_volume(kernel, repo, owner, cursor, volume):
    """Called in the reviewed native planner's transaction after it adopts a new volume plan.

    The registered scope and chapter budget remain immutable. The cursor stays at
    its frozen window until the previous volume's release and continuity actually exist.
    """
    wr = cursor["world_ref"]
    work, branch = wr["work_id"], wr["branch_id"]
    repo.one("works", {"work_id": work}, lock="update")
    kernel.authorize(repo, owner, work, "ADMIN", branch)
    latest = _load(kernel, repo, work, branch)
    require(
        latest == cursor
        and cursor["state"] == "FROZEN"
        and cursor["index"] + 1 == cursor["window_count"]
        and cursor["frozen_chapters"] == cursor["chapter_count"],
        "STALE_PLAN",
        "The proposed continuation no longer matches the frozen series",
    )
    boards._current_owner(kernel, repo, cursor)
    windows = partition_volumes([volume], cursor["profile"]["window_chapters"])
    count = len(volume["chapters"])
    require(
        cursor["chapter_count"] + count <= cursor["target_chapters"],
        "CAPACITY_LIMIT",
        "Continuation exceeds the authorized book extent",
    )
    established = [_payload(kernel, repo, wr, ref, "ROLLING_WINDOW") for ref in cursor["windows"]]
    existing_chapters = {c["chapter_id"] for w in established for c in w["chapters"]}
    existing_scenes = {
        s["scene_ref"]["artifact_revision_id"]
        for w in established
        for c in w["chapters"]
        for s in c["scenes"]
    }
    require(
        all(w["volume_id"] != volume["volume_id"] for w in established)
        and all(
            c["chapter_id"] not in existing_chapters
            and all(
                s["scene_ref"]["artifact_revision_id"] not in existing_scenes for s in c["scenes"]
            )
            for c in volume["chapters"]
        ),
        "INVALID_ARGUMENT",
        "Continuation repeats the planned prefix",
    )
    head = kernel.head(repo, work, branch)
    ref = _selected(repo, work, PlanningService.selection_key(volume["volume_id"]))
    require(ref, "STALE_PLAN", "Continuation requires a selected native plan")
    plan = _payload(kernel, repo, head, ref, "PLAN_REVISION")
    require(plan["input_world_ref"] == head, "STALE_PLAN", "Continuation plan was superseded")
    for window in windows:
        boards._nodes(
            kernel,
            repo,
            head,
            window["volume_id"],
            plan,
            window["chapters"],
            [],
            0,
            cursor["allowance"]["subject_ids"],
            cursor["allowance"]["predicates"],
            rolling=True,
        )
    cursor["windows"].extend(_store(kernel, repo, owner, wr, "ROLLING_WINDOW", w) for w in windows)
    cursor.update(
        window_count=len(cursor["windows"]),
        chapter_count=cursor["chapter_count"] + count,
        final_volume_id=volume["volume_id"],
        reason=None,
    )
    return _save(kernel, repo, owner, cursor)


def replace_planned_window(
    kernel,
    owner,
    *,
    world_ref,
    run_id,
    window_index,
    expected_window_ref,
    chapters,
    idempotency_key,
):
    """Apply a newly adopted scene plan only to the indicated unopened chapter range."""
    validate_contract("WorldRef", world_ref)
    validate_contract("ArtifactRef", expected_window_ref)
    require(
        type(window_index) is int and window_index >= 0, "INVALID_ARGUMENT", "Invalid window index"
    )
    PlanningService._key(idempotency_key)
    work, branch = world_ref["work_id"], world_ref["branch_id"]
    request = {
        "world_ref": world_ref,
        "run_id": run_id,
        "window_index": window_index,
        "expected_window_ref": expected_window_ref,
        "chapters": chapters,
    }
    with kernel.objects.lock(), kernel.db.uow() as repo:
        repo.one("works", {"work_id": work}, lock="update")
        kernel.authorize(repo, owner, work, "ADMIN", branch)
        prior = kernel._prior(repo, owner, work, "ROLLING_REPLAN", idempotency_key, request)
        if prior:
            return prior
        cursor = _load(kernel, repo, work, branch)
        require(
            cursor
            and cursor["run_id"] == run_id
            and cursor["profile"]["local_replanning"]
            and cursor["state"] != "COMPLETED",
            "VIEW_DENIED",
            "Local replanning is unavailable",
        )
        boards._current_owner(kernel, repo, cursor)
        require(
            kernel.head(repo, work, branch) == world_ref
            and (
                window_index > cursor["index"]
                or (window_index == cursor["index"] and cursor["state"] == "OPEN_NEXT")
            )
            and window_index < cursor["window_count"]
            and cursor["windows"][window_index] == expected_window_ref,
            "STALE_PLAN",
            "Only an unchanged unopened window can be replaced",
        )
        old = _payload(kernel, repo, world_ref, expected_window_ref, "ROLLING_WINDOW")
        updated = {"volume_id": old["volume_id"], "chapters": chapters}
        windows = partition_volumes([updated], cursor["profile"]["window_chapters"])
        require(
            len(windows) == 1
            and [c["chapter_id"] for c in chapters] == [c["chapter_id"] for c in old["chapters"]],
            "STALE_PLAN",
            "Replacement must preserve this chapter range and order",
        )
        chosen_plan = _selected(repo, work, PlanningService.selection_key(old["volume_id"]))
        plan = _payload(kernel, repo, world_ref, chosen_plan, "PLAN_REVISION")
        require(
            plan["input_world_ref"] == world_ref,
            "STALE_PLAN",
            "Select a plan for the current world",
        )
        boards._nodes(
            kernel,
            repo,
            world_ref,
            old["volume_id"],
            plan,
            chapters,
            [],
            0,
            cursor["allowance"]["subject_ids"],
            cursor["allowance"]["predicates"],
            rolling=True,
        )
        foreign = set()
        for ref in cursor["windows"]:
            if ref != expected_window_ref:
                window = _payload(kernel, repo, world_ref, ref, "ROLLING_WINDOW")
                foreign.update(
                    slot["scene_ref"]["artifact_revision_id"]
                    for c in window["chapters"]
                    for slot in c["scenes"]
                )
        require(
            all(
                slot["scene_ref"]["artifact_revision_id"] not in foreign
                for c in chapters
                for slot in c["scenes"]
            ),
            "INVALID_ARGUMENT",
            "A replacement scene already belongs to another window",
        )
        new_ref = _store(kernel, repo, owner, world_ref, "ROLLING_WINDOW", updated)
        cursor["windows"][window_index] = new_ref
        result = {
            "status": "REPLANNED",
            "prior_window_ref": expected_window_ref,
            "window_ref": new_ref,
            "cursor_ref": _save(kernel, repo, owner, cursor),
        }
        kernel._receipt(repo, owner, world_ref, "ROLLING_REPLAN", idempotency_key, request, result)
        return result
