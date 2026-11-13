"""Freeze the S0 record and the actual model-facing bytes under one NF generation.

The immutable artifact table and the existing task row are the storage and CAS
boundary. No new task, world selection, or model call is created here.
"""
import json
from uuid import UUID, uuid5

from sqlalchemy import text

from novel_factory.application.kernel import require
from novel_factory.domain.canonical import canonical_bytes, fingerprint


def _revision(context_id):
    return str(uuid5(UUID(context_id), "creative-context-snapshot-v1:revision"))


def bind_material(item):
    """Extract fences only from authenticated service envelopes, never story text."""
    kind, ref = item["kind"], item["artifact_ref"]
    if kind in {"LITERARY_MEMORY", "CONTINUITY_SNAPSHOT"}:
        return [{"memory_ref" if kind == "LITERARY_MEMORY" else "continuity_ref": ref}]
    if kind == "STYLE_GUIDE":
        return [{"style_refs": [ref], "standalone_style": True}]
    if kind == "SCENE_PLAN":
        return [{"scene_ref": ref, "writing_card": True}]
    if kind in {"NARRATIVE_DRAFT", "TEXT"}:
        try:
            delivery = json.loads(item["content"])
        except ValueError:
            delivery = None
        require(isinstance(delivery, dict) and {"draft_ref", "text_ref", "span", "text"} <= delivery.keys(),
                "INVALID_MATERIAL", "Literary text requires an authorized draft delivery")
        require(ref == delivery["draft_ref" if kind == "NARRATIVE_DRAFT" else "text_ref"],
                "VIEW_DENIED", "Reading receipt does not match the assigned source")
        # A new proof-of-read gets a fresh UUID. It cannot change story material.
        delivery.pop("delivery_ref", None)
        item["content"] = canonical_bytes(delivery).decode()
        return [{"draft_ref": delivery["draft_ref"]}]
    return []


def _check_sources(kernel, repo, task, sources, input_ref):
    """Called while the NF work SHARE lock fences literary selection changes."""
    work = task["work_id"]
    if sources["handoff"]:
        from novel_factory.application.authorization import Principal
        from novel_factory.application.queries import WorldQueryService

        owner = Principal(task["grant_id"], task["principal_id"])
        task, _, spec = WorldQueryService(kernel)._task(repo, owner, task["task_id"], task["generation"])
        job = repo.one("supervisor_jobs", {"task_id": task["task_id"]})
        route = sources["handoff"]["route"]
        require(job is not None and fingerprint(job["config"].get(route)) == sources["handoff"]["sha256"],
                "TASK_REBIND_REQUIRED", "The enrolled creative handoff changed")
        if route == "joint":
            from .joint_handoff import authorize_dispatch
            authorize_dispatch(kernel, repo, owner, task, job["config"])
        else:
            from .editorial_handoff import check_authorizer, check_enrollment
            check_authorizer(kernel, repo, job["config"], task)
            ctx = repo.one("contexts", {"context_bundle_id": task["context_bundle_id"]})
            data = kernel.artifact(repo, input_ref, work, kind="AGENT_INPUT")["payload"]
            check_enrollment(kernel, repo, owner, task, spec, ctx["read_cut"]["knowledge_cut"], data,
                             job["config"][route])
    expected = sources["selections"]
    if expected:
        current = _selections(repo, work, [item["key"] for item in expected])
        require(current == expected, "TASK_REBIND_REQUIRED", "A selected literary source changed")
    if sources["continuity_refs"]:
        head = kernel.head(repo, work)
        current = kernel.revision(repo, head)
        for source in sources["continuity_refs"]:
            if source.get("source_selection_fixed"):
                original = kernel.revision(repo, source["world_ref"])["selection"]
                require(all(current["selection"].get(k) == v for k, v in original.items()),
                        "TASK_REBIND_REQUIRED", "A published prefix occurrence changed after simulation")
            require(repo.ancestor(source["world_ref"], head)
                    and source["interpretation_ref"] == current["interpretation_ref"]
                    and all(current["selection"].get(e["occurrence_id"]) == e["event_version_id"]
                            for e in source["events"]),
                    "TASK_REBIND_REQUIRED", "Continuity sources changed since compilation")
    drafts = sources["draft_ids"]
    if drafts:
        query = text("""
            SELECT 1 FROM nf_artifacts WHERE work_id = :work AND kind = 'NARRATIVE_DRAFT'
              AND payload #>> '{prior_draft_ref,artifact_revision_id}' = ANY(CAST(:drafts AS text[]))
              AND payload #>> '{input_world_ref,branch_id}' = :branch LIMIT 1
        """)
        require(repo.conn.scalar(query, {"work": work, "branch": task["branch_id"], "drafts": drafts}) is None,
                "TASK_REBIND_REQUIRED", "A source manuscript now has a newer draft")


def _selections(repo, work, keys):
    rows = {r["selection_kind"]: r for r in repo.many("selections", {"work_id": work, "selection_kind": keys})}
    return [{"key": key, "ref": rows[key]["artifact_ref"] if key in rows else None,
             "epoch": rows[key]["epoch"] if key in rows else None} for key in sorted(keys)]


def source_fences(kernel, repo, binding, material_sources, input_ref):
    """Resolve exact issued reading/writing scopes while holding the NF work lock."""
    from novel_factory.application.narrative import NarrativeService
    from novel_factory.application.authorization import Principal

    task, context = binding["task"], binding["context"]
    owner = Principal(context["grant_id"], context["principal_id"])
    branch, work = task["branch_id"], task["work_id"]
    service = NarrativeService(kernel)
    selection_keys, drafts, continuities = set(), set(), []
    pending = list(material_sources)
    for source in material_sources:
        if "memory_ref" in source:
            memory = kernel.artifact(repo, source["memory_ref"], work, kind="LITERARY_MEMORY")["payload"]
            require(memory["candidate"]["world_ref"]["branch_id"] == branch,
                    "VIEW_DENIED", "Literary memory belongs to another branch")
            pending.extend({"draft_ref": ref} for ref in memory["source_draft_refs"])
        if "continuity_ref" in source:
            record = kernel.artifact(repo, source["continuity_ref"], work, kind="CONTINUITY_SNAPSHOT")["payload"]
            wr = record["request"]["world_ref"]
            if wr != binding["spec"]["world_ref"]:
                from .continuity import ContinuityService

                ContinuityService(kernel).validate_task_world(repo, record, source["continuity_ref"], binding["spec"]["world_ref"])
                selection_keys.add("CONTINUITY:" + record["request"]["series_id"])
            active = set(record["active_thread_entries"].values())
            events = [item["event_ref"] for entry in record["request"]["entries"] if entry["entry_id"] in active
                      for item in entry["source_events"]]
            continuities.append({"world_ref": wr, "interpretation_ref": kernel.revision(repo, wr)["interpretation_ref"],
                                 "events": events,
                                 **({"source_selection_fixed": True} if wr != binding["spec"]["world_ref"] else {})})
        if source.get("standalone_style"):
            for ref in binding["spec"]["input_artifacts"]:
                row = kernel.artifact(repo, ref, work)
                if row["kind"] == "SCENE_PLAN" and source["style_refs"][0] in row["payload"]["scene_contract"]["style_refs"]:
                    pending.append({"scene_ref": ref, "writing_card": True})
    for source in pending:
        draft_ref = source.get("draft_ref")
        scene_ref = source.get("scene_ref")
        if draft_ref:
            _, _, _, draft = service._reader_binding(repo, owner, task["task_id"], task["generation"], draft_ref)
            require(draft["input_world_ref"]["branch_id"] == branch,
                    "VIEW_DENIED", "Draft was not produced on this branch")
            drafts.add(draft_ref["artifact_revision_id"])
            scene_ref = draft["scene_ref"]
            selection_keys.add(f"NARRATIVE_READ:{task['task_id']}:{task['generation']}")
        if scene_ref:
            scene = kernel.artifact(repo, scene_ref, work, kind="SCENE_PLAN")["payload"]["scene_contract"]
            require(scene["input_world_ref"]["branch_id"] == branch, "VIEW_DENIED", "Scene belongs to another branch")
            if source.get("writing_card"):
                service._writer_binding(repo, owner, task["task_id"], task["generation"], scene_ref)
                for style in scene["style_refs"]:
                    wr = kernel.artifact(repo, style, work, kind="STYLE_GUIDE")["payload"]["world_ref"]
                    require(wr["branch_id"] == branch, "VIEW_DENIED", "Scene style belongs to another branch")
            selection_keys.add("SCENE:" + scene["scene_id"])
            selection_keys.add("NARRATIVE_SCENE:" + scene["scene_id"])
        for style in source.get("style_refs", []):
            style_wr = kernel.artifact(repo, style, work, kind="STYLE_GUIDE")["payload"]["world_ref"]
            require(style_wr["branch_id"] == branch, "VIEW_DENIED", "Style belongs to another branch")
    job = repo.one("supervisor_jobs", {"task_id": task["task_id"]})
    handoff = None
    if job:
        for route in ("joint", "editorial"):
            if route in job["config"]:
                handoff = {"route": route, "sha256": fingerprint(job["config"][route])}
                break
    result = {"handoff": handoff, "selections": _selections(repo, work, selection_keys) if selection_keys else [], "draft_ids": sorted(drafts),
              "continuity_refs": continuities}
    _check_sources(kernel, repo, task, result, input_ref)
    return result


def load_snapshot(kernel, repo, task, input_ref, *, required=False):
    row = repo.one("artifacts", {"work_id": task["work_id"], "kind": "CREATIVE_CONTEXT_SNAPSHOT",
                                 "artifact_revision_id": _revision(task["context_bundle_id"])})
    if row is None and not required:
        return None
    require(row is not None, "TASK_REBIND_REQUIRED", "Frozen creative context is unavailable")
    snap = row["payload"]
    require(fingerprint(snap) == row["sha256"] and snap["input_ref"] == input_ref
            and snap["packet"]["scope"]["task_id"] == task["task_id"]
            and snap["packet"]["scope"]["generation"] == task["generation"]
            and snap["packet"]["context"]["context_bundle_id"] == task["context_bundle_id"],
            "TASK_REBIND_REQUIRED", "Frozen creative context does not match this task")
    _check_sources(kernel, repo, task, snap["source_versions"], input_ref)
    return {key: row[key] for key in ("artifact_id", "artifact_revision_id", "sha256")}, snap


def freeze_snapshot(kernel, repo, binding, input_ref, packet, user, material_sources, *, started):
    task = binding["task"]
    repo.one("tasks", {"task_id": task["task_id"]}, lock="update")
    versions = source_fences(kernel, repo, binding, material_sources, input_ref)
    payload = {"input_ref": input_ref, "packet": packet, "model_input": user, "source_versions": versions}
    for key in ("dynamic_assignment_ref", "adaptive_instruction_ref"):
        if key in binding:
            payload[key] = binding[key]
    prior = load_snapshot(kernel, repo, task, input_ref)
    if prior:
        require(prior[1] == payload, "TASK_REBIND_REQUIRED", "Concurrent context compilation differs")
        return prior
    ref = {"artifact_id": str(uuid5(UUID(task["context_bundle_id"]), "creative-context-snapshot-v1")),
           "artifact_revision_id": _revision(task["context_bundle_id"]),
           "sha256": kernel.objects.put(canonical_bytes(payload)).sha256}
    kernel._register_artifact(repo, ref, task["work_id"], payload, "CREATIVE_CONTEXT_SNAPSHOT",
                              task["principal_id"])
    from .harness_observations import creative_packet
    creative_packet(repo.conn, ref, packet, user, started)
    return ref, payload


def check_candidate_snapshot(kernel, repo, task, audit):
    ref = audit.get("creative_context_ref")
    if ref is not None:
        saved, snapshot = load_snapshot(kernel, repo, task, audit["input_ref"], required=True)
        require(ref == saved and all(snapshot.get(k) == audit.get(k) for k in ("dynamic_assignment_ref", "adaptive_instruction_ref")),
                "TASK_REBIND_REQUIRED", "Candidate used a different creative snapshot")
