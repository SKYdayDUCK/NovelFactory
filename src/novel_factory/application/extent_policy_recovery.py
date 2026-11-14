"""Adopt advisory length policy for an exact legacy length-only hold.

No manuscript, task result, call accounting, allowance or review conclusion is
changed. The existing draft returns to the ordinary independent review queue.
"""
import re

from ..contracts.registry import validate_contract
from ..domain.canonical import fingerprint
from .candidate_delivery import CandidateDelivery
from .kernel import require

COMMAND = "ADOPT_ADVISORY_EXTENT"


def recover_extent_policy(kernel, gateway, owner, world_ref, *, node_id, expected_board_sha256,
                          source_revision, idempotency_key, apply=False):
    from .narrative import NarrativeService
    from .planning import PlanningService
    from .production_extent import _count_han
    from .production_pipeline import _artifact, _current_owner, _key, _load, _save
    from .semantic_dependencies import check_task_dependencies
    from .volume_successor import _selected

    validate_contract("WorldRef", world_ref)
    PlanningService._key(idempotency_key)
    require(type(apply) is bool and isinstance(node_id, str) and node_id.startswith("draft:")
            and isinstance(expected_board_sha256, str) and re.fullmatch(r"[0-9a-f]{64}", expected_board_sha256)
            and isinstance(source_revision, str) and re.fullmatch(r"[0-9a-f]{40}", source_revision),
            "INVALID_ARGUMENT", "Advisory policy adoption requires fixed source identities")
    request = {"world_ref": world_ref, "node_id": node_id, "expected_board_sha256": expected_board_sha256,
               "source_revision": source_revision, "length_policy": "ADVISORY"}
    with kernel.objects.lock(), kernel.db.uow(write=apply) as repo:
        work, branch = world_ref["work_id"], world_ref["branch_id"]
        repo.one("works", {"work_id": work}, lock="update" if apply else "share")
        kernel.authorize(repo, owner, work, "ADMIN", branch)
        previous = kernel._prior(repo, owner, work, COMMAND, idempotency_key, request)
        if previous:
            return previous
        selected = _selected(repo, work, _key(branch))
        require(selected and selected["sha256"] == expected_board_sha256,
                "TASK_REBIND_REQUIRED", "The expected length hold changed")
        board = _load(kernel, repo, work, branch)
        require(board["world_ref"] == world_ref and _current_owner(kernel, repo, board) == owner,
                "VIEW_DENIED", "Only the current production owner can adopt the policy")
        writer = board["nodes"].get(node_id)
        require(writer and writer["stage"] == "draft" and writer["state"] == "STOPPED"
                and writer.get("reason") in {"REPAIR_BUDGET_EXHAUSTED", "STRATEGY_REASSESSMENT_REQUIRED",
                                            "SCENE_LENGTH_OUTSIDE_TARGET"}
                and writer.get("output_ref") and writer.get("extent_repairs") and writer.get("han_characters"),
                "TASK_NOT_ACCEPTABLE", "The current draft has no proven legacy length hold")
        require(not repo.one("selections", {"work_id": work, "selection_kind": "NARRATIVE_SCENE:" + node_id[6:]}),
                "NARRATIVE_SELECTION_CONFLICT", "An accepted scene cannot be reopened by a length policy")
        require(not any(node["state"] == "ENROLLED" for node in board["nodes"].values()),
                "TASK_NOT_ACCEPTABLE", "Drain this board's enrolled work before policy adoption")
        current = writer["output_ref"]
        draft = kernel.artifact(repo, current, work, kind="NARRATIVE_DRAFT")["payload"]
        task = repo.one("tasks", {"task_id": writer["task_id"]})
        require(task and task["state"] == "ACCEPTED" and task["generation"] == writer["generation"] == draft["generation"]
                and task["task_id"] == draft["task_id"] and task["role_id"] == "writer"
                and draft["scene_ref"] == writer["scene_ref"] and draft["handoff_refs"] == writer["handoff_refs"],
                "INVALID_EVIDENCE", "Length hold no longer refers to its completed exact writer")
        NarrativeService(kernel)._origin(repo, draft)
        check_task_dependencies(repo, task["task_id"], task["generation"])
        audit, candidate = CandidateDelivery(kernel, gateway).audited_candidate(repo, task, production_accounting=True)
        body = kernel.objects.read(draft["text_ref"]["sha256"]).decode("utf-8")
        actual = _count_han(body)
        require(draft.get("agent_output_ref") == audit["output_ref"] and candidate.get("text") == body
                and audit["world_ref"] == draft["input_world_ref"],
                "INVALID_EVIDENCE", "The length hold differs from the actual writer candidate")
        latest = writer["extent_repairs"][-1]
        ref = latest["reflection_ref"]
        reflection = kernel.artifact(repo, ref, work, kind="P6_REFLECTION_RECORD")["payload"]
        error = kernel.artifact(repo, reflection["error_ref"], work, kind="P6_ERROR_RECORD")["payload"]
        require(fingerprint(reflection) == ref["sha256"] and reflection["evidence_ref"] == current
                and fingerprint(error) == reflection["error_ref"]["sha256"]
                and error["trigger"] == "SCENE_HAN_EXTENT" and error["error_type"] == reflection["family"] == "extent"
                and all(error[key] == reflection[key] for key in ("task_id", "attempt_id", "evidence_ref"))
                and reflection["task_id"] == task["task_id"] and reflection["attempt_id"] == audit["attempt_id"]
                and latest["actual"] == actual
                and not writer["han_characters"]["min"] <= actual <= writer["han_characters"]["max"],
                "INVALID_EVIDENCE", "The selected stop has no exact historical length failure")
        # Pending external/world/semantic questions for this manuscript retain
        # their existing holds. This command never adopts a review conclusion.
        require(not any(node["stage"] == "internal_verification"
                        and node.get("internal_case", {}).get("source_draft_ref") == current
                        and node["state"] not in {"VERIFICATION_CONSUMED", "COVERAGE_TRANSITIONED"}
                        for node in board["nodes"].values()),
                "INTERNAL_REVIEW_UNRESOLVED", "The current draft still has an independent unresolved inquiry")
        result = {"status": "ADVISORY_EXTENT_PREVIEW", "draft_ref": current, "text_ref": draft["text_ref"],
                  "actual_han": actual, "historical_target": writer["han_characters"],
                  "provider_calls_sent": 0, "allowance_changed": False, "reviews_accepted": 0}
        if not apply:
            return result
        proof = {"record_version": 1, "request": request, "source_board_ref": selected,
                 "source_reason": writer["reason"], "source_reflection_ref": ref,
                 "draft_ref": current, "text_ref": draft["text_ref"], "task_id": task["task_id"],
                 "generation": task["generation"], "actual_han": actual, "policy": "ADVISORY",
                 "extent_history_sha256": fingerprint(writer["extent_repairs"]),
                 "revision_count": writer.get("revision_count", 0), "repair_allowance": writer.get("repair_allowance", {}),
                 "diagnoses_sha256": fingerprint(writer.get("reassessments", [])),
                 "provider_calls_sent": 0, "allowance_changed": False, "reviews_accepted": 0}
        proof_ref = _artifact(kernel, repo, work, owner, "ADVISORY_EXTENT_ADOPTION", proof)
        writer.update(state="DRAFT_STORED", extent_policy_ref=proof_ref,
                      extent_observation={"policy": "ADVISORY", "draft_ref": current,
                                          "actual_han": actual, "historical_target": writer["han_characters"]})
        writer.pop("reason", None)
        for node in board["nodes"].values():
            if (node["state"] == "STOPPED" and node.get("reason") == "PREDECESSOR_STOPPED"
                    and not node.get("task_id") and not node.get("output_ref") and node_id in node.get("parents", [])):
                node.update(state="PENDING")
                node.pop("reason", None)
        result.update(status="ADVISORY_EXTENT_ADOPTED", proof_ref=proof_ref, board_ref=_save(kernel, repo, owner, board))
        kernel._receipt(repo, owner, world_ref, COMMAND, idempotency_key, request, result)
        return result
