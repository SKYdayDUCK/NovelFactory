"""Atomically accept a verified child edition while preserving its frozen parent."""
from copy import deepcopy

from ..domain.canonical import fingerprint
from ..domain.review_gate import blocking_review_issues
from .agent_gateway import AgentGateway
from .candidate_delivery import CandidateDelivery
from .kernel import require
from .narrative import NarrativeService
from .planning import PlanningService
from .production_pipeline import _artifact, _current_owner
from .review_mcp import require_review_queries
from .volume_successor import _selected


def _reading(kernel, repo, wr, child, field, kind):
    task = repo.one("tasks", {"task_id": child[field]})
    writer = repo.one("tasks", {"task_id": child["writer_task_id"]})
    require(task and writer and task["state"] == "ACCEPTED"
            and (task["role_id"], task["task_kind"]) == ("reviewer", kind)
            and task["principal_id"] != writer["principal_id"] and task["grant_id"] != writer["grant_id"],
            "INDEPENDENT_REVIEW_REQUIRED", "Child edition requires independent accepted readers")
    evidence, candidate = CandidateDelivery(kernel, AgentGateway(kernel, {})).audited_candidate(
        repo, task, production_accounting=True)
    require(evidence["world_ref"] == wr and not blocking_review_issues(candidate["issues"]),
            "REVIEW_REQUIRED", "Child edition still has mandatory reading findings")
    require_review_queries(kernel, repo, task, evidence)
    frozen = kernel.artifact(repo, evidence["input_ref"], wr["work_id"], kind="AGENT_INPUT")["payload"]
    require(child["draft_ref"] in frozen["material_refs"],
            "INVALID_EVIDENCE", "Child reader did not read this exact candidate")


def guard_child(kernel, repo, principal, world_ref, draft_ref, edition_ref):
    """Narrow internal exception to the parent chapter guard, never to reconciliation."""
    from .frozen_editing import _state
    from .returned_edition import _guard
    from .rolling_production import _load

    wr = world_ref
    state = _state(kernel, repo, wr["work_id"], wr["branch_id"])
    cursor = _load(kernel, repo, wr["work_id"], wr["branch_id"])
    require(state and cursor and _current_owner(kernel, repo, cursor) == principal
            and state["status"] == "EDITION_REVISING" and state["edition"]["phase"] == "accept"
            and state.get("edition_ref") == edition_ref,
            "VIEW_DENIED", "Only the active edition's original controller may accept its children")
    manifest = kernel.artifact(repo, edition_ref, wr["work_id"], kind="P6_RETURNED_EDITION_CANDIDATE")["payload"]
    require(manifest["world_ref"] == wr == state["binding"]["world_ref"]
            and manifest["run"] == cursor["run_id"] == state["run"]
            and manifest["parent_session"] == state["session"]
            and manifest["parent_checkpoint"] == cursor["last_checkpoint"]
            and manifest["parent_binding_sha256"] == fingerprint(state["binding"])
            and manifest["controller"] == {"grant_id": principal.grant_id, "principal_id": principal.principal_id}
            and not _selected(repo, wr["work_id"], "RELEASE:" + state["volume"]),
            "STALE_GENERATION", "Child edition no longer matches its unpublished frozen parent")
    scenes = _guard(kernel, repo, state, cursor)
    children = manifest["candidates"]
    require([item["scene_id"] for item in children] == [scene["scene_id"] for scene in scenes]
            and any(item["changed"] for item in children)
            and any(item["draft_ref"] == draft_ref for item in children),
            "INVALID_EVIDENCE", "The accepted candidate is outside the complete changed edition")
    service = NarrativeService(kernel)
    for scene, child in zip(scenes, children, strict=True):
        draft = kernel.artifact(repo, child["draft_ref"], wr["work_id"], kind="NARRATIVE_DRAFT")["payload"]
        require(child["parent_ref"] == scene["draft_ref"] == draft["prior_draft_ref"]
                and draft["scene_ref"] == scene["scene_ref"] and draft["text_ref"] == child["text_ref"],
                "INVALID_EVIDENCE", "Child lineage changed after independent checking")
        _reading(kernel, repo, wr, child, "epistemic_task_id", "epistemic")
        _reading(kernel, repo, wr, child, "literary_task_id", "literary")
        facts = kernel.artifact(repo, child["facts_ref"], wr["work_id"], kind="ROLLING_FACT_REPORT")["payload"]
        require(facts["status"] == "CLEAR" and facts["reconciliation_ref"] == child["reconciliation_ref"]
                and facts["alignment_ref"] == child["alignment_ref"] and facts["knowledge_ref"] == child["knowledge_ref"],
                "NARRATIVE_ALIGNMENT_REQUIRED", "Child factual report changed")
        service.validate_for_acceptance(repo, principal, child["draft_ref"], child["reconciliation_ref"], wr)
    return True


def commit_edition(kernel, repo, owner, state, cursor):
    """Called under one work lock; scene acceptances and checkpoint advance commit together."""
    from .rolling_production import _save

    wr = state["binding"]["world_ref"]
    edition_ref = state["edition_ref"]
    manifest = kernel.artifact(repo, edition_ref, wr["work_id"], kind="P6_RETURNED_EDITION_CANDIDATE")["payload"]
    children = {item["scene_id"]: item for item in manifest["candidates"]}
    guard_child(kernel, repo, owner, wr, manifest["candidates"][0]["draft_ref"], edition_ref)
    parent = cursor["last_checkpoint"]
    checkpoints = []
    previous = parent
    while previous:
        payload = kernel.artifact(repo, previous, wr["work_id"], kind="ROLLING_CHECKPOINT")["payload"]
        if payload["volume_id"] != state["volume"]:
            break
        checkpoints.insert(0, payload)
        previous = payload["previous"]
    chapters = [deepcopy(chapter) for checkpoint in checkpoints for chapter in checkpoint["chapters"]]
    accepted = []
    for chapter in chapters:
        chapter["parent_reading_ref"] = chapter["reading_ref"]
        chapter["reading_ref"] = _artifact(kernel, repo, wr["work_id"], owner, "RETURNED_EDITION_CHAPTER_READING", {
            "world_ref": wr, "edition_ref": edition_ref, "chapter_id": chapter["chapter_id"],
            "draft_refs": [children[scene["scene_id"]]["draft_ref"] for scene in chapter["scenes"]],
            "scene_reading_refs": [children[scene["scene_id"]]["literary_ref"] for scene in chapter["scenes"]],
            "scope": "EXACT_CHILD_SCENES", "literary_quality": "PENDING_FRESH_VOLUME_EDIT"})
        for scene in chapter["scenes"]:
            child = children[scene["scene_id"]]
            acceptance = NarrativeService(kernel).accept_narrative(owner, child["draft_ref"], child["reconciliation_ref"], wr,
                "returned-child-accept:" + edition_ref["artifact_revision_id"] + ":" + scene["scene_id"],
                alignment_ref=child["alignment_ref"], edition_ref=edition_ref)
            accepted.append(acceptance)
            scene.update(draft_ref=child["draft_ref"], text_ref=child["text_ref"], acceptance_ref=acceptance,
                         knowledge_ref=child["knowledge_ref"])
    checkpoint = _artifact(kernel, repo, wr["work_id"], owner, "ROLLING_CHECKPOINT", {
        "world_ref": wr, "run_id": cursor["run_id"], "series_id": cursor["series_id"], "volume_id": state["volume"],
        "index": cursor["index"], "previous": previous, "chapters": chapters, "parent_checkpoint": parent,
        "edition_ref": edition_ref})
    for scene_id in children:
        PlanningService(kernel)._select(repo, wr, "P6_FROZEN_SCENE:" + scene_id, checkpoint)
    cursor.update(last_checkpoint=checkpoint)
    _save(kernel, repo, owner, cursor)
    receipt = {"parent_checkpoint": parent, "checkpoint_ref": checkpoint, "edition_ref": edition_ref,
               "acceptance_refs": accepted, "world_effects_applied": 0}
    reference = _artifact(kernel, repo, wr["work_id"], owner, "RETURNED_EDITION_ACCEPTANCE", receipt)
    state.update(status="EDITION_ACCEPTED", active=None, edition_acceptance_ref=reference)
    state.pop("reason", None)
    return reference
