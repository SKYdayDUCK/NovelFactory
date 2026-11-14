"""Bind an unsuccessful blind round to a real editor's bounded revision decision."""
from ..domain.canonical import canonical_bytes, fingerprint
from . import book_review, frozen_editing
from .kernel import require
from .production_pipeline import _artifact
from .rolling_production import _load
from .rolling_publication import final_review_request
from .volume_successor import _selected


def request_rework(kernel, owner, work, branch, round_ref):
    with kernel.objects.lock(), kernel.db.uow() as repo:
        repo.one("works", {"work_id": work}, lock="update")
        bound = kernel.in_transaction(repo)
        request = final_review_request(bound, owner, work, branch)
        cursor = _load(bound, repo, work, branch)
        review = book_review._load(bound, repo, work, branch)
        state = frozen_editing._state(bound, repo, work, branch)
        require(review and review["state"] == "ASSESSED" and review["submission_hash"] == fingerprint(request)
                and review["result"]["status"] == "P6_QUALITY_NOT_MET"
                and round_ref == _selected(repo, work, book_review._selection(branch))
                and cursor["state"] == "FROZEN" and not _selected(repo, work, "RELEASE:" + state["volume"]),
                "REVIEW_SELECTION_CONFLICT", "Book revision requires the exact unsuccessful unpublished edition")
        history = state.setdefault("book_rework_history", [])
        if any(row["round_ref"] == round_ref for row in history):
            return {"status": "BOOK_REWORK_PENDING", "state": state["status"]}
        if len(history) >= 2:
            return {"status": "PAUSED", "reason": "BOOK_REWORK_EXHAUSTED"}
        require(state["status"] == "EDITORIAL_APPROVED" and state["active"] is None,
                "TASK_NOT_ACCEPTABLE", "Finish the active edition before commissioning book revision")
        scores = [reader["score_ref"] for reader in review["readers"]]
        require(len(scores) == 2 and len({fingerprint(r) for r in scores}) == 2,
                "INDEPENDENT_REVIEW_REQUIRED", "Both independent assessments must be preserved")
        ref = _artifact(bound, repo, work, owner, "BOOK_REWORK_REQUEST", {
            "world_ref": request["world_ref"], "submission_sha256": fingerprint(request),
            "parent_session": state["session"], "round_ref": round_ref, "score_refs": scores,
            "parent_checkpoint": cursor["last_checkpoint"], "round": len(history) + 1})
        history.append({"round_ref": round_ref, "request_ref": ref})
        state["jobs"].append({"phase": "BOOK_REWORK", "scope": state["binding"]["ordered_draft_refs"],
                              "book_rework_ref": ref, "review_refs": [round_ref, *scores]})
        state.update(status="EDITING_ACTIVE", index=len(state["jobs"]) - 1)
        frozen_editing._save(bound, repo, owner, state)
        return {"status": "BOOK_REWORK_ENROLLED", "request_ref": ref}


def material(kernel, principal, task, ref, mark):
    with kernel.db.uow(write=False) as repo:
        state = frozen_editing._current_job(kernel, repo, task, mark)
        job = state["jobs"][state["index"]]
        require(job.get("phase") == "BOOK_REWORK" and ref in job["review_refs"]
                and ref in mark.get("review_refs", []), "VIEW_DENIED", "Unassigned book-revision evidence")
        request = kernel.artifact(repo, job["book_rework_ref"], task["work_id"], kind="BOOK_REWORK_REQUEST")["payload"]
        require(request["parent_session"] == state["session"] and request["world_ref"] == state["binding"]["world_ref"]
                and request["round_ref"] == _selected(repo, task["work_id"], book_review._selection(task["branch_id"])),
                "TASK_REBIND_REQUIRED", "Book-revision evidence is no longer current")
        artifact = kernel.artifact(repo, ref, task["work_id"])
        if artifact["kind"] == "BOOK_REVIEW_ROUND":
            value = {key: artifact["payload"][key] for key in
                     ("world_ref", "submission_hash", "text_refs", "result", "readers", "round")}
            value["readers"] = [{key: reader[key] for key in ("reader_id", "score_ref") if key in reader}
                                for reader in value["readers"]]
        else:
            require(artifact["kind"] == "BOOK_BLIND_SCORE" and ref in request["score_refs"], "VIEW_DENIED", "Unbound reader score")
            value = artifact["payload"]
        return canonical_bytes({"source_ref": ref, "evidence": value})
