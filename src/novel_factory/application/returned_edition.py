"""Create and independently read a child draft set for a returned, unpublished volume."""

import json
from datetime import datetime

from .authorization import Principal
from .idea_intake import _audited_design_result
from .kernel import require
from .narrative import NarrativeService
from .planning import PlanningService
from .production_pipeline import _artifact, _current_owner
from .release import ReleaseService
from .runtime_tasks import RuntimeTaskService
from .volume_successor import _selected
from ..domain.canonical import fingerprint


def begin(kernel, repo, owner, state, cursor, task, proof, output):
    from .quality_memory import plan_repair, reflect_repair

    wr = state["binding"]["world_ref"]
    findings = {"phase": state["jobs"][state["index"] - 1].get("phase"),
                "reason": output.get("reason", output.get("summary", "")),
                "deferred_questions": output.get("deferred_questions", [])}
    plan = reflect_repair(kernel, repo, owner, wr, task["task_id"], proof["attempt_id"], proof["output_ref"],
                          plan_repair("editorial", findings, []), "FROZEN_EDITOR_RETURNED")
    state.update(status="EDITION_REVISING", edition={"reflection_ref": plan["reflection_ref"], "error_ref": plan["error_ref"],
        "parent_checkpoint": cursor["last_checkpoint"], "parent_session": state["session"],
        "editor_task_id": task["task_id"], "editor_attempt_id": proof["attempt_id"],
        "editor_output_ref": proof["output_ref"], "scene_index": 0, "phase": "write",
        "candidates": [], "readings": []})


def _guard(kernel, repo, state, cursor):
    from .rolling_production import frozen_volume_scenes

    wr = state["binding"]["world_ref"]
    edit = state["edition"]
    require(cursor["run_id"] == state["run"] and cursor["state"] == "FROZEN"
            and cursor["last_checkpoint"] == edit["parent_checkpoint"] and state["session"] == edit["parent_session"]
            and kernel.head(repo, wr["work_id"], wr["branch_id"]) == wr
            and not _selected(repo, wr["work_id"], "RELEASE:" + state["volume"]),
            "TASK_REBIND_REQUIRED", "Returned edition is no longer the current unpublished frozen parent")
    scenes = frozen_volume_scenes(kernel, repo, cursor, state["volume"])
    require([s["acceptance_ref"] for s in scenes] == state["binding"]["ordered_acceptance_refs"],
            "NARRATIVE_SELECTION_CONFLICT", "Original frozen acceptances changed")
    return scenes


def _assign(kernel, repo, gateway, owner, state, cursor, scene):
    from .supervisor import RuntimeSupervisor

    ed = state["edition"]
    wr = state["binding"]["world_ref"]
    work = wr["work_id"]
    reading = ed["phase"] == "read"
    role, kind = ("reviewer", "literary") if reading else ("writer", "targeted_revision")
    deadline = int((datetime.fromisoformat(cursor["authorization"]["deadline"]) - repo.now()).total_seconds())
    require(deadline > 0, "TASK_EXPIRED", "Returned edition allowance expired")
    auth = cursor["allowance"]
    prior = kernel.artifact(repo, scene["draft_ref"], work, kind="NARRATIVE_DRAFT")["payload"]
    returned = kernel.artifact(repo, ed["editor_output_ref"], work, kind="AGENT_CANDIDATE")["payload"]
    reason = returned.get("reason", returned.get("summary", ""))
    findings = []
    for ref in state["reports"]:
        report = kernel.artifact(repo, ref, work, kind="REVIEW_REPORT")["payload"]["submission"]
        findings.extend(i for i in report["issues"] if i["subject_ref"] in (scene["text_ref"], scene["draft_ref"]))
    assignment = {"operation": "read_returned_edition" if reading else "revise_returned_edition",
                  "editor_reason": reason, "deferred_questions": returned.get("deferred_questions", []),
                  "scene_findings": findings, "volume_scene_ordinal": ed["scene_index"] + 1,
                  "instructions": ("先读原稿，再读本次候选；核对是否回应退回意见、保留视角与既有事件。报告仍有的问题。"
                                   if reading else "按已授权的本场原文与退回意见修订。返回完整正文。只有涉及本场的问题才改动；保留事件、认知边界和未受影响文字。")}
    original = scene["draft_ref"]
    mate = ed["candidates"][ed["scene_index"]]["draft_ref"] if reading else None
    # If the writer preserved a passage, its child draft has a new identity yet equal bytes.
    materials = [original, mate] if reading else [scene["scene_ref"], original]
    card = kernel.artifact(repo, scene["scene_ref"], work, kind="SCENE_PLAN")["payload"]
    refs = [card["plan_ref"], card["batch_ref"], scene["scene_ref"], original, ed["editor_output_ref"],
            *prior["handoff_refs"], *([mate] if reading else [])]
    prepared = RuntimeTaskService(kernel, gateway).prepare(
        owner, wr, role_id=role, task_kind=kind, assignment=json.dumps(assignment, ensure_ascii=False),
        adapter_id=auth["routes"]["review" if reading else "draft"], subject_ids=auth["subject_ids"],
        predicates=auth["predicates"], story_time=auth["story_time"], material_refs=materials, input_artifacts=refs,
        queue_state="READY", deadline_seconds=deadline, lease_seconds=min(600, deadline),
        max_amount=auth["reserve_amount"], token_budget=auth["token_reservation"],
        currency=repo.one("budgets", {"work_id": work, "budget_id": auth["budget_id"]})["currency"])
    sup = RuntimeSupervisor(kernel, gateway)
    config, _ = sup.dispatch_config(
        repo, prepared.spec, repo.one("grants", {"grant_id": prepared.principal.grant_id}),
        manifest_ref=prepared.manifest_ref, knowledge_cut={"plane": "WORLD"}, context_tokens=20000,
        input_ref=prepared.agent_input["input_ref"], budget_id=auth["budget_id"],
        reserve_amount=auth["reserve_amount"], token_reservation=auth["token_reservation"])
    config["returned_edition"] = {"parent_session": state["session"], "phase": ed["phase"], "scene": ed["scene_index"]}
    sup.install_job(repo, prepared.task, config)
    state["active"] = prepared.task["task_id"]


def prepare_materials(kernel, task, mark):
    from .frozen_editing import _state
    from .rolling_production import _load

    work, branch = task["work_id"], task["branch_id"]
    with kernel.objects.lock(), kernel.db.uow() as repo:
        repo.one("works", {"work_id": work}, lock="update")
        bound = kernel.in_transaction(repo)
        cursor = _load(bound, repo, work, branch)
        owner = _current_owner(bound, repo, cursor)
        state = _state(bound, repo, work, branch)
        require(state and state["status"] == "EDITION_REVISING" and state["active"] == task["task_id"]
                and mark == {"parent_session": state["session"], "phase": state["edition"]["phase"],
                             "scene": state["edition"]["scene_index"]},
                "TASK_REBIND_REQUIRED", "Child-edition task lost its frozen assignment")
        scene = _guard(bound, repo, state, cursor)[mark["scene"]]
        key = "returned-edition:" + task["task_id"] + ":" + str(task["generation"])
        orig = scene["draft_ref"]
        refs = [orig]
        if mark["phase"] == "write":
            prior = bound.artifact(repo, orig, work, kind="NARRATIVE_DRAFT")["payload"]
            card = bound.artifact(repo, scene["scene_ref"], work, kind="SCENE_PLAN")["payload"]
            PlanningService(bound).bind_task_for_controller(owner, task["task_id"], task["generation"],
                                                          card["plan_ref"], card["plan_node_ids"], key)
            ReleaseService(bound).bind_writer_task(Principal(task["grant_id"], task["principal_id"]),
                task["task_id"], task["generation"], scene["scene_ref"], prior["handoff_refs"], key)
        else:
            child = state["edition"]["candidates"][mark["scene"]]
            source = repo.one("tasks", {"work_id": work, "task_id": child["writer_task_id"]})
            require(source and source["principal_id"] != task["principal_id"], "INDEPENDENT_REVIEW_REQUIRED",
                    "The child author cannot independently read its own revision")
            refs.append(child["draft_ref"])
        NarrativeService(bound).bind_reader_task(owner, task["task_id"], task["generation"], refs, key)


def advance(kernel, repo, gateway, owner, state, cursor):
    ed = state["edition"]
    scenes = _guard(kernel, repo, state, cursor)
    scene = scenes[ed["scene_index"]]
    if not state["active"]:
        _assign(kernel, repo, gateway, owner, state, cursor, scene)
        return True
    task = repo.one("tasks", {"task_id": state["active"]})
    if task["state"] != "RESULT_STORED":
        if task["state"] in {"FAILED", "PAUSED", "STALE", "WAITING_REVIEW", "CANCELLED"}:
            state.update(status="EDITION_REVISION_BLOCKED", reason=task["state"])
            return True
        return False
    proof, output = _audited_design_result(kernel, gateway, repo, task, state["binding"]["world_ref"])
    work = task["work_id"]
    if ed["phase"] == "write":
        prior = kernel.artifact(repo, scene["draft_ref"], work, kind="NARRATIVE_DRAFT")["payload"]
        draft = NarrativeService(kernel).store_draft(
            Principal(task["grant_id"], task["principal_id"]), task["task_id"], task["generation"],
            scene["scene_ref"], output["text"], "returned-child:" + task["task_id"],
            prior_draft_ref=scene["draft_ref"], handoff_refs=prior["handoff_refs"], title=prior["title"],
            declared_important_deltas=output["important_deltas"], agent_output_ref=proof["output_ref"])
        payload = kernel.artifact(repo, draft, work, kind="NARRATIVE_DRAFT")["payload"]
        ed["candidates"].append({"scene_id": scene["scene_id"], "parent_ref": scene["draft_ref"], "draft_ref": draft,
                                "text_ref": payload["text_ref"], "changed": scene["text_ref"]["sha256"] != payload["text_ref"]["sha256"],
                                "writer_task_id": task["task_id"], "attempt_id": proof["attempt_id"]})
        ed["phase"] = "read"
    else:
        ed["readings"].append({"scene_id": scene["scene_id"], "review_ref": proof["output_ref"],
                              "review_task_id": task["task_id"], "attempt_id": proof["attempt_id"],
                              "issues": len(output["issues"])})
        ed["scene_index"] += 1
        ed["phase"] = "write"
        if ed["scene_index"] == len(scenes):
            changed = any(c["changed"] for c in ed["candidates"])
            state.update(status="EDITION_HANDOFF_REQUIRED",
                         reason="CANDIDATE_ACCEPTANCE_REQUIRED" if changed else "EDITORIAL_NO_PROGRESS")
            payload = {"world_ref": state["binding"]["world_ref"], "run": state["run"], "volume": state["volume"],
                       "parent_binding_sha256": fingerprint(state["binding"]), **ed}
            state["edition_ref"] = _artifact(kernel, repo, work, owner, "P6_RETURNED_EDITION_CANDIDATE", payload)
    kernel.tasks.accept_in_transaction(repo.conn, task["task_id"], task["generation"])
    state["active"] = None
    return True
