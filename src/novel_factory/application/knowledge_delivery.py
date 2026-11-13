"""Controller-selected, immutable public methods for a P6 production task."""
from .kernel import require
from .knowledge_profiles import freeze_knowledge_profile
from .production_pipeline import _artifact
from .volume_successor import _selected
from novel_factory.domain.canonical import fingerprint


def freeze_for_task(kernel, repo, owner, world_ref, role_id, task_kind, reason=None):
    work, branch = world_ref["work_id"], world_ref["branch_id"]
    profile_ref = _selected(repo, work, "P6_PROFILE:" + branch)
    if not profile_ref:
        return None
    profile = kernel.artifact(repo, profile_ref, work, kind="PRODUCTION_PROFILE")["payload"]
    require(fingerprint(profile) == profile_ref["sha256"], "OBJECT_CORRUPT",
            "Production profile differs from its selection")
    if reason is None and task_kind == "targeted_revision":
        reason = "review_failure"
    notes = freeze_knowledge_profile(role_id, task_kind, profile["genre"], reread_reason=reason)
    if not notes["entries"]:
        return None
    return _artifact(kernel, repo, work, owner, "P6_KNOWLEDGE_PROFILE",
                     {"branch_id": branch, "profile_ref": profile_ref, "methods": notes})


def bound_methods(kernel, repo, spec, ref):
    scope = spec["world_ref"]
    payload = kernel.artifact(repo, ref, scope["work_id"], kind="P6_KNOWLEDGE_PROFILE")["payload"]
    require(fingerprint(payload) == ref["sha256"] and payload["branch_id"] == scope["branch_id"]
            and payload["methods"]["role_id"] == spec["role_id"]
            and payload["methods"]["task_kind"] == spec["task_kind"],
            "TASK_REBIND_REQUIRED", "Knowledge profile is outside this task binding")
    # Only the approved method text is useful in the prompt. References and
    # full selection metadata remain in the immutable task input for audit.
    return [{key: entry[key] for key in ("method", "limit", "original") if key in entry}
            for entry in payload["methods"]["entries"]]
