"""Finite projection of the accepted joint effects bound to one volume review."""
from novel_factory.application.kernel import require
from novel_factory.application.planning import PlanningService
from novel_factory.contracts.registry import validate_contract
from novel_factory.domain.temporal import point_key
from novel_factory.domain.world import ALIASES, PREDICATES
from novel_factory.orchestration.workflow import review_acceptance


def _visible_effect(effect, subjects, predicates, at):
    """Project fields from public world-effect variants. Unknown variants stay private."""
    kind = effect["kind"]
    if kind == "RESOURCE_DELTA":
        account, resource, unit = effect["account_id"], effect["resource_id"], effect["unit"]
        if {account, resource} <= subjects and f"resource:{resource}:{unit}" in predicates:
            return {key: effect[key] for key in ("kind", "account_id", "resource_id", "unit", "quantity")}
    elif kind == "LIFECYCLE_SET":
        if effect["entity_id"] in subjects and "lifecycle" in predicates:
            return {key: effect[key] for key in ("kind", "entity_id", "lifecycle")}
    elif kind in {"RELATION_SET", "RELATION_TRANSITION"}:
        subject, predicate = effect["subject_id"], effect["predicate"]
        if subject not in subjects or predicate not in PREDICATES or predicate not in predicates:
            return None
        if kind == "RELATION_SET":
            if effect["value_status"] == "VALUE" and effect["object_id"] not in subjects:
                return None
            value = {key: effect[key] for key in ("kind", "subject_id", "predicate", "value_status")}
            if effect["value_status"] == "VALUE":
                value["object_id"] = effect["object_id"]
            return value
        target = effect["object_value"]
        if point_key(effect["at"]) <= point_key(at) and (target["kind"] != "ENTITY" or target["entity_id"] in subjects):
            return {key: effect[key] for key in ("kind", "operation", "at", "subject_id", "predicate", "object_value")}
    return None


def review_effects(kernel, repo, task, spec, manifest_ref, read_cut):
    receipt = review_acceptance(repo.conn, task, spec)
    wr = spec["world_ref"]
    require(receipt["world_ref"] == read_cut["world_ref"] == wr, "TASK_REBIND_REQUIRED", "Wake receipt changed")
    require(manifest_ref in spec["input_artifacts"] and manifest_ref in read_cut["source_manifest_refs"],
            "VIEW_DENIED", "The review requires its enrolled and compiled input manifest")
    manifest = validate_contract("InputManifest", kernel.artifact(repo, manifest_ref, wr["work_id"],
                                 kind="INPUT_MANIFEST")["payload"], "kernel")
    return scoped_accepted_effects(kernel, repo, wr, spec["story_time"], manifest, receipt["accepted_event_refs"])


def scoped_accepted_effects(kernel, repo, wr, at, manifest, refs):
    """Project only selected events, through this story time, within the caller's task material allowance."""
    subjects = set(manifest["subject_ids"])
    predicates = {ALIASES.get(name, name) for name in manifest["predicates"]}
    PlanningService(kernel)._events(kernel.revision(repo, wr), refs)
    entries = []
    for ref in refs:
        row = repo.one("events", {"work_id": wr["work_id"], **ref})
        require(row, "INVALID_EVIDENCE", "Selected event is unavailable")
        event = row["payload"]
        occurrence = event["occurrence_time"]
        end = occurrence.get("at", occurrence.get("latest"))
        require(end is not None and point_key(end) <= point_key(at),
                "VIEW_DENIED", "The accepted event exceeds the review's story horizon")
        effects = []
        for effect in event["effects"]:
            item = _visible_effect(effect, subjects, predicates, at)
            if item is not None:
                effects.append(item)
        entries.append({"event_ref": ref, "occurrence_time": occurrence, "effects": effects})
    return {"accepted_event_refs": refs, "accepted_effects": entries,
            "effect_scope": "Only authorized effects are shown. Omitted material is unknown."}
