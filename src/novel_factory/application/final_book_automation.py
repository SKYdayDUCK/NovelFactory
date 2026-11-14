"""Enroll independent native blind readers and freeze only their accepted final edition."""
from datetime import datetime

from ..domain.canonical import fingerprint
from . import book_review
from .frozen_editing import _state as editing_state
from .kernel import require
from .model_routing import select
from .release import ReleaseService
from .rolling_production import _load
from .rolling_publication import final_review_request
from .volume_successor import _selected


def advance(kernel, gateway, owner, work, branch):
    request = final_review_request(kernel, owner, work, branch)
    with kernel.db.uow(write=False) as repo:
        kernel.authorize(repo, owner, work, "ADMIN", branch)
        cursor = _load(kernel, repo, work, branch)
        review = book_review._load(kernel, repo, work, branch)
        round_ref = _selected(repo, work, book_review._selection(branch))
        edit = editing_state(kernel, repo, work, branch)
        seconds = int((datetime.fromisoformat(cursor["authorization"]["deadline"]) - repo.now()).total_seconds())
        texts = [kernel.artifact(repo, ref, work, kind="NARRATIVE_DRAFT")["payload"]["text_ref"]
                 for ref in edit["binding"]["ordered_draft_refs"]]
    revision = {}
    if review:
        require(review["world_ref"] == request["world_ref"] and review["final_volume_id"] == request["volume_id"],
                "REVIEW_SELECTION_CONFLICT", "The selected blind review belongs to a different final edition")
        same = review["submission_hash"] == fingerprint(request)
        if same and review["state"] == "ASSESSED":
            if review["result"]["status"] not in {"QUALITY_TARGET_MET", "QUALITY_PASS_TARGET_NOT_MET"}:
                from .book_rework import request_rework
                return request_rework(kernel, owner, work, branch, round_ref)
            released = ReleaseService(kernel).freeze(owner, request, "native-final-freeze:" + fingerprint(request))
            return {"status": "BOOK_RELEASE_FROZEN", "release_ref": released["release_ref"]}
        if same:
            if review["state"] == "BLOCKED":
                return {"status": "PAUSED", "reason": review.get("reason", "BOOK_REVIEW_BLOCKED")}
            return {"status": "FINAL_BOOK_REVIEW_PENDING", "state": review["state"]}
        changed = next((ref for ref in texts if ref["sha256"] not in {r["sha256"] for r in review["text_refs"]}), None)
        require(review["state"] == "ASSESSED" and review["result"]["status"] == "P6_QUALITY_NOT_MET" and changed,
                "REVIEW_SELECTION_CONFLICT", "A new reading requires an actually revised, independently approved edition")
        revision = {"revision_note": "Bound internal revision following the preceding independent blind assessment.",
                    "revision_evidence": {"prior_score_ref": review["readers"][0]["score_ref"], "changed_ref": changed,
                        "expected_effect": "Address the preceding edition's verified review findings.",
                        "verification": "Recheck the full revised text independently on both blind tracks."}}
    require(0 < seconds <= 86400, "GRANT_EXPIRED", "The final review requires a current production allowance")
    auth = cursor["allowance"]
    route = auth["routes"]["review"]
    if route == "@policy":
        with kernel.db.uow(write=False) as repo:
            route = select(kernel, repo, request["world_ref"],
                {"role_id": "reviewer", "task_kind": "literary", "task_id": request["volume_id"], "instance_id": None},
                gateway.adapters)["profile_id"]
    adapter = gateway.adapters.get(route)
    require(adapter and adapter.configuration.get("adapter_kind") == "codex-cli"
            and adapter.configuration.get("auth_mode") == "host-login", "MODEL_UNAVAILABLE",
            "Automatic final blind reading requires the authorized native Codex route")
    enrolled = book_review.start_review(kernel, gateway, owner, release_request=request,
        routes=[route, route], budget_id=auth["budget_id"], reserve_amount=auth["reserve_amount"],
        token_reservation=auth["token_reservation"], deadline_seconds=seconds,
        idempotency_key="native-final-read:" + fingerprint(request), **revision)
    return {"status": "FINAL_BOOK_REVIEW_ENROLLED", **enrolled}
