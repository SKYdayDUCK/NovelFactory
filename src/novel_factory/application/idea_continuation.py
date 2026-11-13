"""Obtain a bounded new volume from two native, budgeted tasks after a chapter prefix freezes."""

import json
from datetime import datetime

from novel_factory.application import rolling_production as rolling
from novel_factory.application.editorial_effects import scoped_accepted_effects
from novel_factory.application.idea_intake import (
    _audited_design_result,
    _point,
    _uid,
    create_planned_window,
)
from novel_factory.application.kernel import require
from novel_factory.application.planning import PlanningService
from novel_factory.application.production_pipeline import _current_owner
from novel_factory.application.runtime_tasks import RuntimeTaskService
from novel_factory.application.supervisor import RuntimeSupervisor
from novel_factory.application.volume_successor import _selected
from novel_factory.domain.errors import DomainError


def _pending(cursor):
    return (
        cursor
        and cursor["state"] == "FROZEN"
        and cursor["index"] + 1 == cursor["window_count"]
        and cursor["chapter_count"] < cursor.get("target_chapters", cursor["chapter_count"])
    )


def _enroll(kernel, repo, gateway, owner, wr, cursor, assignment, refs, *, reviewing):
    auth = cursor["allowance"]
    seconds = int(
        (datetime.fromisoformat(cursor["authorization"]["deadline"]) - repo.now()).total_seconds()
    )
    require(seconds > 0, "TASK_EXPIRED", "Planning allowance expired")
    kind = "fact" if reviewing else "window_plan"
    role = "reviewer" if reviewing else "volume_director"
    text = json.dumps(assignment, ensure_ascii=False)
    require(len(text) <= 32000, "CAPACITY_LIMIT", "Planning brief exceeds the role prompt boundary")
    if not reviewing:
        gateway.output_contracts[(role, kind)] = ("NextChapterPlan", "production_profile")
    budget = repo.one("budgets", {"budget_id": auth["budget_id"], "work_id": wr["work_id"]})
    prepared = RuntimeTaskService(kernel, gateway).prepare(
        owner,
        wr,
        role_id=role,
        task_kind=kind,
        skill_name="review-consistency" if reviewing else "plan-volume",
        assignment=text,
        adapter_id=auth["routes"]["review" if reviewing else "plan"],
        subject_ids=auth["subject_ids"],
        predicates=auth["predicates"],
        story_time=_point(cursor["frozen_chapters"]),
        queue_state="READY",
        deadline_seconds=seconds,
        lease_seconds=min(seconds, 600),
        input_artifacts=refs,
        max_amount=auth["reserve_amount"],
        token_budget=auth["token_reservation"],
        currency=budget["currency"],
    )
    RuntimeSupervisor(kernel, gateway).enroll(
        owner,
        prepared.task["task_id"],
        manifest_ref=prepared.manifest_ref,
        knowledge_cut={"plane": "WORLD"},
        context_tokens=20000,
        input_ref=prepared.agent_input["input_ref"],
        budget_id=auth["budget_id"],
        reserve_amount=auth["reserve_amount"],
        token_reservation=auth["token_reservation"],
    )
    return prepared.task["task_id"]


def _memory(kernel, repo, owner, cursor, checkpoint, work, wr):
    recent = []
    for chapter in checkpoint["chapters"][-4:]:
        final = chapter["scenes"][-1]
        accepted, _ = rolling._load_acceptance(repo, kernel, owner, final["acceptance_ref"], wr)
        require(
            accepted["text_ref"] == final["text_ref"],
            "NARRATIVE_SELECTION_CONFLICT",
            "Frozen text changed",
        )
        kernel.artifact(repo, final["text_ref"], work, kind="TEXT")
        text = kernel.objects.read(final["text_ref"]["sha256"]).decode("utf-8")
        scene = kernel.artifact(repo, final["scene_ref"], work, kind="SCENE_PLAN")["payload"]
        require(accepted["scene_ref"] == final["scene_ref"], "NARRATIVE_SELECTION_CONFLICT",
                "Checkpoint scene does not match the accepted text")
        entry = {
            "chapter": chapter["ordinal"], "excerpt": text[-800:], "text_hash": final["text_ref"]["sha256"]
        }
        events = scene["required_event_refs"]
        if events:
            projection = scoped_accepted_effects(
                kernel, repo, wr, _point(cursor["frozen_chapters"]), cursor["allowance"], events
            )
            entry["accepted_effects"] = projection["accepted_effects"]
        recent.append(entry)
    return recent


def _plan_turn(kernel, gateway, repo, owner, wr, cursor, key, source):
    work = wr["work_id"]
    checkpoint = rolling._payload(kernel, repo, wr, cursor["last_checkpoint"], "ROLLING_CHECKPOINT")
    require(
        checkpoint["run_id"] == cursor["run_id"]
        and checkpoint["world_ref"] == wr
        and checkpoint["index"] == cursor["index"],
        "STALE_PLAN",
        "Frozen prefix no longer matches current head",
    )
    previous_plan = _selected(repo, work, PlanningService.selection_key(checkpoint["volume_id"]))
    require(previous_plan, "STALE_PLAN", "Previous selected plan is unavailable")
    previous = kernel.artifact(repo, previous_plan, work, kind="PLAN_REVISION")["payload"]
    design = kernel.artifact(repo, source["design_ref"], work, kind="INITIAL_STORY_DESIGN")[
        "payload"
    ]
    max_goals = min(
        cursor["target_chapters"] - cursor["chapter_count"], cursor["profile"]["window_chapters"]
    )
    base = {"work_head": wr, "checkpoint": cursor["last_checkpoint"], "plan_ref": previous_plan}
    planned = kernel._prior(repo, owner, work, "P6_NEXT_PLANNER", key, {})
    if not planned:
        assignment = {
            "task": "根据已接受前文、现有规则及读者已知信息，自主规划下一卷。只提交未来目标，不把计划记作世界事实。",
            "inspiration": source["creative_input"],
            "rules": design["world_rules"][:6],
            "original_storyline": design["storyline"],
            "last_plan_goals": [n["goal"] for n in previous["nodes"][-4:]],
            "actual_text": _memory(kernel, repo, owner, cursor, checkpoint, work, wr),
            "frozen_chapters": cursor["chapter_count"],
            "target_chapters": cursor["target_chapters"],
            "maximum_new_goals": max_goals,
        }
        tid = _enroll(
            kernel,
            repo,
            gateway,
            owner,
            wr,
            cursor,
            assignment,
            [source["design_ref"], previous_plan, cursor["last_checkpoint"]],
            reviewing=False,
        )
        receipt = {**base, "task_id": tid, "status": "NEXT_PLAN_ENROLLED"}
        kernel._receipt(repo, owner, wr, "P6_NEXT_PLANNER", key, {}, receipt)
        return receipt
    require(all(planned[k] == v for k, v in base.items()), "STALE_PLAN", "Planner input changed")
    task = repo.one("tasks", {"task_id": planned["task_id"], "work_id": work}, lock="update")
    if task["state"] != "RESULT_STORED":
        return None
    evidence, plan = _audited_design_result(kernel, gateway, repo, task, wr)
    goals = plan["chapter_goals"]
    require(
        0 < len(goals) <= max_goals and len(set(goals)) == len(goals),
        "CAPACITY_LIMIT",
        "Next volume must fit the remaining authorized chapter extent",
    )
    reviewed = kernel._prior(repo, owner, work, "P6_NEXT_PLAN_REVIEW", key, {})
    if reviewed is None:
        review_brief = {
            "task": "独立检查下一卷目标是否延续已接受的前文，核查矛盾、遗漏或重复。登记须修改的问题。",
            "candidate": plan,
            "candidate_ref": evidence["output_ref"],
            "actual_text": _memory(kernel, repo, owner, cursor, checkpoint, work, wr),
        }
        tid = _enroll(
            kernel,
            repo,
            gateway,
            owner,
            wr,
            cursor,
            review_brief,
            [evidence["output_ref"], previous_plan, cursor["last_checkpoint"]],
            reviewing=True,
        )
        receipt = {
            **base,
            "task_id": tid,
            "candidate_ref": evidence["output_ref"],
            "status": "NEXT_PLAN_REVIEW_ENROLLED",
        }
        kernel._receipt(repo, owner, wr, "P6_NEXT_PLAN_REVIEW", key, {}, receipt)
        return receipt
    review = repo.one("tasks", {"task_id": reviewed["task_id"], "work_id": work}, lock="update")
    if review["state"] != "RESULT_STORED":
        return None
    require(
        task["principal_id"] != review["principal_id"]
        and evidence["output_ref"] == reviewed["candidate_ref"],
        "REVIEW_REQUIRED",
        "Review must address this planning revision from another principal",
    )
    proof, findings = _audited_design_result(kernel, gateway, repo, review, wr)
    require(not findings["issues"], "REVIEW_REQUIRED", "Next plan requires targeted revision")
    volume, refs = create_planned_window(
        kernel,
        owner,
        wr,
        _uid(work, key),
        source["subject_ids"][0],
        source["subject_ids"][1],
        evidence["output_ref"],
        plan["volume_brief"],
        goals,
        design["world_rules"],
        start_after=cursor["chapter_count"],
        upstream_refs=[cursor["last_checkpoint"], previous_plan, evidence["output_ref"], proof["output_ref"]],
    )
    cursor_ref = rolling.append_generated_volume(kernel, repo, owner, cursor, volume)
    for row in (task, review):
        kernel.tasks.accept_in_transaction(repo.conn, row["task_id"], row["generation"])
    receipt = {
        "status": "NEXT_PLAN_ACCEPTED",
        "cursor_ref": cursor_ref,
        "volume": volume["volume_id"],
        "added_chapters": len(goals),
        "source_checkpoint": base["checkpoint"],
        **refs,
        "plan_task": task["task_id"],
        "plan_attempt": evidence["attempt_id"],
        "plan_output": evidence["output_ref"],
        "review_task": review["task_id"],
        "review_attempt": proof["attempt_id"],
        "review_output": proof["output_ref"],
    }
    kernel._receipt(repo, owner, wr, "P6_NEXT_PLAN_ACCEPT", key, {}, receipt)
    return receipt


def advance_idea_planning(kernel, gateway, controller, work, branch):
    with kernel.db.uow(write=False) as repo:
        if not _pending(rolling._load(kernel, repo, work, branch)):
            return None
    with kernel.objects.lock(), kernel.db.uow() as repo:
        repo.one("works", {"work_id": work}, lock="update")
        kernel.authorize(repo, controller, work, "ADMIN", branch)
        cursor = rolling._load(kernel, repo, work, branch)
        if not _pending(cursor):
            return None
        owner = _current_owner(kernel, repo, cursor)
        wr = kernel.head(repo, work, branch)
        source = kernel._prior(repo, owner, work, "P6_IDEA_PROVENANCE", "initial", {})
        require(source, "VIEW_DENIED", "Generated planning requires a matching native intake")
        key = cursor["run_id"] + ":" + str(cursor["chapter_count"])
        if kernel._prior(repo, owner, work, "P6_NEXT_PLAN_BLOCKED", key, {}):
            return None
        bound = kernel.in_transaction(repo)
        try:
            with repo.conn.begin_nested():
                return _plan_turn(
                    bound, gateway.with_kernel(bound), repo, owner, wr, cursor, key, source
                )
        except DomainError as error:
            # A cost can reconcile on the same attempt. An unresolved call remains eligible
            # for a later recheck and never sends a replacement request.
            if error.code == "CALL_RECONCILIATION_REQUIRED":
                return None
            receipt = {
                "status": "NEXT_PLAN_BLOCKED",
                "reason": error.code,
                "source_checkpoint": cursor["last_checkpoint"],
            }
            kernel._receipt(repo, owner, wr, "P6_NEXT_PLAN_BLOCKED", key, {}, receipt)
            return receipt
