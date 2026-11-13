"""Consume signed native approvals when a rolling run reaches an intermediate volume."""

from .continuity import ContinuityService
from .kernel import require
from .planning import PlanningService
from .release import ReleaseService
from .volume_successor import _selected
from sqlalchemy import text
from ..domain.canonical import fingerprint


def _editorial_key(world_ref, plan, acceptances):
    return "P6_SERIAL_EDITORIAL:" + fingerprint([world_ref, plan, acceptances])


def _continuity_key(world_ref, series, prior, releases):
    return "P6_SERIAL_CONTINUITY:" + fingerprint([world_ref, series, prior, releases])


def _enrolled(repo, wr):
    return _selected(repo, wr["work_id"], "P6_ROLLING:" + wr["branch_id"])


def record_editorial(kernel, repo, binding, approval_ref):
    wr = binding["world_ref"]
    if _enrolled(repo, wr):
        key = _editorial_key(wr, binding["plan_ref"], binding["ordered_acceptance_refs"])
        PlanningService(kernel)._select(repo, wr, key, approval_ref)
        repo.conn.execute(text("SELECT pg_notify('nf_supervisor_wake', '')"))


def record_continuity(kernel, repo, candidate, review_ref):
    request = candidate["request"]
    wr = request["world_ref"]
    if request["operation"] == "APPEND" and _enrolled(repo, wr):
        key = _continuity_key(
            wr, request["series_id"], request["expected_prior_ref"], request["ordered_release_refs"]
        )
        PlanningService(kernel)._select(repo, wr, key, review_ref)
        repo.conn.execute(text("SELECT pg_notify('nf_supervisor_wake', '')"))


def _approved_request(kernel, repo, cursor, wr, volume):
    from .rolling_production import frozen_volume_scenes

    work = wr["work_id"]
    ordered = frozen_volume_scenes(kernel, repo, cursor, volume)
    accepts = [s["acceptance_ref"] for s in ordered]
    plan_ref = _selected(repo, work, PlanningService.selection_key(volume))
    approval_ref = _selected(repo, work, _editorial_key(wr, plan_ref, accepts))
    if not approval_ref:
        return None

    approval = kernel.artifact(repo, approval_ref, work, kind="EDITORIAL_APPROVAL")["payload"]
    binding = approval["binding"]
    batches = []
    for scene in ordered:
        plan = kernel.artifact(repo, scene["scene_ref"], work, kind="SCENE_PLAN")["payload"]
        if plan["batch_ref"] not in batches:
            batches.append(plan["batch_ref"])
    return {
        "schema_version": "2.2",
        "world_ref": wr,
        "volume_id": volume,
        "plan_ref": plan_ref,
        "batch_refs": batches,
        "ordered_acceptance_refs": accepts,
        "editorial_approval_ref": approval_ref,
        **{
            key: binding[key]
            for key in (
                "narrative_revision_id",
                "arrangement_version",
                "disclosure_revision_id",
                "style_refs",
                "literary_refs",
            )
        },
        "prior_release_ref": None,
        "revision_reason": None,
    }


def _freeze(kernel, repo, owner, cursor, wr, volume):
    request = _approved_request(kernel, repo, cursor, wr, volume)
    if request is None:
        return None
    result = ReleaseService(kernel).freeze(
        owner, request, "p6-serial-freeze:" + fingerprint(request)
    )
    return {
        "status": "ROLLING_VOLUME_RELEASED",
        "volume_id": volume,
        "release_ref": result["release_ref"],
    }


def _publish(kernel, repo, owner, cursor, wr, volume, release_ref):
    work, series = wr["work_id"], cursor["series_id"]
    selection = _selected(repo, work, "CONTINUITY:" + series)
    if selection != cursor["continuity_ref"]:
        # An already published edition is checked by the rolling boundary itself.
        return None
    releases = [*cursor["release_refs"], release_ref]
    review_ref = _selected(repo, work, _continuity_key(wr, series, selection, releases))
    if not review_ref:
        return None
    review = kernel.artifact(repo, review_ref, work, kind="CONTINUITY_REVIEW")["payload"]
    require(
        review["policy_version"] == repo.one("works", {"work_id": work})["policy_version"],
        "APPROVAL_REVOKED",
        "Continuity review policy changed",
    )
    if review["request"]["decision"] != "APPROVED":
        return None
    candidate_ref = review["request"]["candidate_ref"]
    candidate = kernel.artifact(repo, candidate_ref, work, kind="CONTINUITY_CANDIDATE")["payload"][
        "request"
    ]
    require(
        candidate["world_ref"] == wr
        and candidate["series_id"] == series
        and candidate["expected_prior_ref"] == selection
        and candidate["operation"] == "APPEND"
        and candidate["ordered_release_refs"] == releases,
        "CONTINUITY_REVALIDATION_REQUIRED",
        "Independent continuity approval addresses another edition",
    )
    require_volume_memory(cursor, candidate)
    publish = {
        "schema_version": "2.3",
        "candidate_ref": candidate_ref,
        "review_ref": review_ref,
        "idempotency_key": "p6-serial-publish:" + review_ref["artifact_revision_id"],
    }
    result = ContinuityService(kernel).publish(owner, work, publish)
    return {
        "status": "ROLLING_CONTINUITY_PUBLISHED",
        "volume_id": volume,
        "snapshot_ref": result["snapshot_ref"],
    }


def require_volume_memory(cursor, request):
    if cursor["profile"]["cross_volume_memory"]:
        ordinal = len(request["ordered_release_refs"])
        require(
            any(entry["volume_ordinal"] == ordinal for entry in request["entries"]),
            "CONTINUITY_REVALIDATION_REQUIRED",
            "The profile requires source-backed memory for the volume being closed",
        )


def advance_approved_publication(kernel, repo, owner, cursor, volume, *, gateway=None):
    wr = kernel.head(repo, cursor["world_ref"]["work_id"], cursor["world_ref"]["branch_id"])
    release_ref = _selected(repo, wr["work_id"], "RELEASE:" + volume)
    if release_ref is None:
        return _freeze(kernel, repo, owner, cursor, wr, volume)
    if cursor["profile"].get("production_mode") == "quality" and gateway:
        series = cursor["series_id"]
        releases = [*cursor["release_refs"], release_ref]
        review_key = _continuity_key(wr, series, cursor["continuity_ref"], releases)
        if (_selected(repo, wr["work_id"], "CONTINUITY:" + series) == cursor["continuity_ref"]
                and not _selected(repo, wr["work_id"], review_key)):
            from .serial_continuity_review import advance_serial_reading
            return advance_serial_reading(kernel, repo, gateway, owner, wr, cursor, volume, release_ref)
    return _publish(kernel, repo, owner, cursor, wr, volume, release_ref)

def final_review_request(kernel, owner, work, branch):
    """Read the initial final edition's editorial authorization and ordered accepted scenes."""
    from .rolling_production import _load, _window

    with kernel.db.uow(write=False, repeatable_read=True) as repo:
        kernel.authorize(repo, owner, work, "ADMIN", branch)
        cursor = _load(kernel, repo, work, branch)
        require(
            cursor
            and cursor["state"] in {"FROZEN", "COMPLETED"}
            and cursor["index"] + 1 == cursor["window_count"]
            and cursor["frozen_chapters"] == cursor["chapter_count"] == cursor["target_chapters"],
            "RELEASE_INCOMPLETE",
            "The full authorized set of chapters must be frozen",
        )
        volume = _window(kernel, repo, cursor)["volume_id"]
        require(volume == cursor["final_volume_id"], "RELEASE_INCOMPLETE", "Wrong final volume")
        wr = kernel.head(repo, work, branch)
        request = _approved_request(kernel, repo, cursor, wr, volume)
        require(request is not None, "RELEASE_INCOMPLETE", "Final editorial approval is missing")
        return request
