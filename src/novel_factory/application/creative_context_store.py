"""Freeze the S0 record and the actual model-facing bytes under one NF generation.

The immutable artifact table and the existing task row are the storage and CAS
boundary. No new task, world selection, or model call is created here.
"""
import json
from hashlib import sha256
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


def _own_completed_draft(kernel, repo, task, audit, row):
    """The completed writer may prove its own product, never requalify a send."""
    if not audit or task["state"] != "ACCEPTED" or task["role_id"] != "writer":
        return False
    draft = row["payload"]
    ref = {key: str(row[key]) for key in ("artifact_id", "artifact_revision_id", "sha256")}
    if not (fingerprint(draft) == ref["sha256"] and draft["task_id"] == task["task_id"]
            and draft["generation"] == task["generation"] and draft["context_bundle_id"] == task["context_bundle_id"]
            and draft.get("agent_output_ref") == audit.get("output_ref") and draft["input_world_ref"] == audit["world_ref"]):
        return False
    result = repo.one("task_results", {"result_id": task["current_result_id"], "task_id": task["task_id"],
                                       "generation": task["generation"], "disposition": "CURRENT"})
    if not (result and result["payload_hash"] == fingerprint(result["payload"])
            and result["attempt_id"] == task["attempt_id"] and result["payload"]["status"] == "SUCCEEDED"
            and result["payload"]["context_bundle_id"] == task["context_bundle_id"]
            and result["payload"]["dependency_fingerprint"] == task["dependency_fingerprint"]
            and audit.get("output_ref") in result["payload"]["artifacts"]):
        return False
    candidate = kernel.artifact(repo, audit["output_ref"], task["work_id"], kind="AGENT_CANDIDATE")["payload"]
    if not (fingerprint(candidate) == audit["output_ref"]["sha256"] and isinstance(candidate.get("text"), str)
            and sha256(candidate["text"].encode()).hexdigest() == draft["text_ref"]["sha256"]
            and candidate["important_deltas"] == draft["declared_important_deltas"]):
        return False
    kernel.objects.verify(draft["text_ref"]["sha256"])
    request = {"task_id": task["task_id"], "generation": task["generation"], "scene_ref": draft["scene_ref"],
               "text_sha256": draft["text_ref"]["sha256"], "title": draft["title"],
               "declared_important_deltas": draft["declared_important_deltas"], "prior_draft_ref": draft["prior_draft_ref"],
               "handoff_refs": draft["handoff_refs"], "agent_output_ref": draft["agent_output_ref"]}
    if not any(receipt["payload"] == ref and receipt["request_hash"] == fingerprint(request)
               for receipt in repo.many("receipts", {"work_id": task["work_id"], "principal_id": task["principal_id"],
                                                     "command_type": "NARRATIVE_DRAFT"})):
        return False
    newer = repo.conn.scalar(text("""SELECT 1 FROM nf_artifacts WHERE work_id=:work AND kind='NARRATIVE_DRAFT'
        AND payload #>> '{prior_draft_ref,artifact_revision_id}'=:prior LIMIT 1"""),
        {"work": task["work_id"], "prior": ref["artifact_revision_id"]})
    return newer is None


def _assigned_draft(kernel, repo, task, input_ref, ref):
    """Full identity must be frozen in task, model input and controller read scope."""
    record = repo.one("task_inputs", {"task_id": task["task_id"], "generation": task["generation"]})
    scoped = repo.one("selections", {"work_id": task["work_id"],
        "selection_kind": "NARRATIVE_READ:" + task["task_id"] + ":" + str(task["generation"])})
    if not record or not scoped or fingerprint(record["spec"]) != record["input_hash"]:
        return False
    frozen = kernel.artifact(repo, input_ref, task["work_id"], kind="AGENT_INPUT")["payload"]
    scope = kernel.artifact(repo, scoped["artifact_ref"], task["work_id"], kind="NARRATIVE_READ_SCOPE")["payload"]
    return (ref in record["spec"]["input_artifacts"] and ref in frozen["material_refs"] and ref in scope["draft_refs"]
            and scope["task_id"] == task["task_id"] and scope["generation"] == task["generation"]
            and scope["grant_id"] == task["grant_id"] and scope["world_ref"] == record["spec"]["world_ref"]
            and fingerprint(frozen) == input_ref["sha256"] and fingerprint(scope) == scoped["artifact_ref"]["sha256"])


def _known_assigned_successor(kernel, repo, task, sources, input_ref, row):
    ref = {key: str(row[key]) for key in ("artifact_id", "artifact_revision_id", "sha256")}
    prior = row["payload"]["prior_draft_ref"]
    return (ref["artifact_revision_id"] in sources["draft_ids"] and prior
            and prior["artifact_revision_id"] in sources["draft_ids"]
            and fingerprint(row["payload"]) == ref["sha256"]
            and _assigned_draft(kernel, repo, task, input_ref, ref)
            and _assigned_draft(kernel, repo, task, input_ref, prior))


def _completed_reader_draft(kernel, repo, task, audit, row):
    if (not audit or task["state"] != "ACCEPTED" or task["role_id"] not in {
            "reviewer", "evidence_maintainer", "literary_editor", "chief_editor"}
            or audit["model_configuration"].get("adapter_kind") != "codex-cli"):
        return False
    ref = {key: str(row[key]) for key in ("artifact_id", "artifact_revision_id", "sha256")}
    draft = row["payload"]
    if fingerprint(draft) != ref["sha256"] or not _assigned_draft(kernel, repo, task, audit["input_ref"], ref):
        return False
    from .review_mcp import require_review_queries
    receipts = require_review_queries(kernel, repo, task, audit)
    for receipt in receipts:
        material = receipt["result"].get("structuredContent", {})
        if (receipt["tool"] != "nf_review_material" or receipt["outcome"] != "OK"
                or receipt["arguments"].get("artifact_ref") != ref or material.get("draft_ref") != ref
                or material.get("text_ref") != draft["text_ref"] or not material.get("delivery_ref")):
            continue
        delivery_ref = material["delivery_ref"]
        delivery = kernel.artifact(repo, delivery_ref, task["work_id"], kind="TEXT_DELIVERY")["payload"]
        if (fingerprint(delivery) == delivery_ref["sha256"] and delivery["task_id"] == task["task_id"]
                and delivery["generation"] == task["generation"] and delivery["context_bundle_id"] == task["context_bundle_id"]
                and delivery["draft_ref"] == ref and delivery["text_ref"] == draft["text_ref"]
                and delivery["world_ref"] == audit["world_ref"] and delivery["read_mode"] == "FULL_TEXT"):
            return True
    return False


def _edition_acceptance(kernel, repo, task, acceptance_ref, acceptance):
    """Recognize only the actual atomic edition receipt family for this child."""
    from .volume_successor import _selected
    work = task["work_id"]
    edition_ref = _selected(repo, work, "RETURNED_EDITION_FOR_DRAFT:" + acceptance["draft_ref"]["artifact_revision_id"])
    if not edition_ref:
        return False
    manifest = kernel.artifact(repo, edition_ref, work, kind="P6_RETURNED_EDITION_CANDIDATE")["payload"]
    accepted_ref = _selected(repo, work, "RETURNED_EDITION_ACCEPTANCE:" + edition_ref["artifact_revision_id"])
    if not accepted_ref:
        return False
    atomic = kernel.artifact(repo, accepted_ref, work, kind="RETURNED_EDITION_ACCEPTANCE")["payload"]
    checkpoint = kernel.artifact(repo, atomic["checkpoint_ref"], work, kind="ROLLING_CHECKPOINT")["payload"]
    intent = {"world_ref": manifest["world_ref"], "edition_ref": edition_ref, "parent_checkpoint": manifest["parent_checkpoint"]}
    receipt = repo.one("receipts", {"work_id": work, "branch_id": task["branch_id"], "command_type": "RETURNED_EDITION_ACCEPT",
                                    "idempotency_key": edition_ref["artifact_revision_id"], "payload": accepted_ref})
    scenes = [scene for chapter in checkpoint["chapters"] for scene in chapter["scenes"]]
    children = manifest["candidates"]
    if not (receipt and receipt["principal_id"] == manifest["controller"]["principal_id"]
            and receipt["request_hash"] == fingerprint(intent)
            and fingerprint(manifest) == edition_ref["sha256"] and fingerprint(atomic) == accepted_ref["sha256"]
            and fingerprint(checkpoint) == atomic["checkpoint_ref"]["sha256"]
            and atomic["edition_ref"] == checkpoint["edition_ref"] == edition_ref
            and atomic["parent_checkpoint"] == checkpoint["parent_checkpoint"] == manifest["parent_checkpoint"]
            and checkpoint["world_ref"] == manifest["world_ref"] and checkpoint["run_id"] == manifest["run"]
            and checkpoint["volume_id"] == manifest["volume"]
            and [scene["scene_id"] for scene in scenes] == [child["scene_id"] for child in children]
            and atomic["acceptance_refs"] == [scene["acceptance_ref"] for scene in scenes]
            and acceptance_ref in atomic["acceptance_refs"]):
        return False
    for child, scene in zip(children, scenes, strict=True):
        adopted = kernel.artifact(repo, scene["acceptance_ref"], work, kind="NARRATIVE_ACCEPTANCE")["payload"]
        request = {"draft_ref": child["draft_ref"], "reconciliation_ref": child["reconciliation_ref"],
                   "world_ref": manifest["world_ref"], "alignment_ref": child["alignment_ref"], "edition_ref": edition_ref}
        if not (adopted["draft_ref"] == scene["draft_ref"] == child["draft_ref"]
                and adopted["text_ref"] == scene["text_ref"] == child["text_ref"]
                and adopted["reconciliation_ref"] == child["reconciliation_ref"]
                and adopted["alignment_ref"] == child["alignment_ref"]
                and fingerprint(adopted) == scene["acceptance_ref"]["sha256"]
                and repo.one("receipts", {"work_id": work, "branch_id": task["branch_id"], "command_type": "NARRATIVE_ACCEPT",
                    "principal_id": manifest["controller"]["principal_id"], "payload": scene["acceptance_ref"], "request_hash": fingerprint(request)})):
            return False
    return any(child["draft_ref"] == acceptance["draft_ref"] and child["text_ref"] == acceptance["text_ref"] for child in children)


def _own_acceptance(kernel, repo, task, audit, previous, current):
    if not current["ref"] or not current["key"].startswith("NARRATIVE_SCENE:"):
        return False
    acceptance = kernel.artifact(repo, current["ref"], task["work_id"], kind="NARRATIVE_ACCEPTANCE")["payload"]
    row = kernel.artifact(repo, acceptance["draft_ref"], task["work_id"], kind="NARRATIVE_DRAFT")
    if not (_own_completed_draft(kernel, repo, task, audit, row)
            or _completed_reader_draft(kernel, repo, task, audit, row)):
        return False
    draft = row["payload"]
    scene = kernel.artifact(repo, draft["scene_ref"], task["work_id"], kind="SCENE_PLAN")["payload"]
    if not (current["key"] == "NARRATIVE_SCENE:" + scene["scene_contract"]["scene_id"]
            and acceptance["task_id"] == draft["task_id"] and acceptance["generation"] == draft["generation"]
            and acceptance["text_ref"] == draft["text_ref"] and acceptance["scene_ref"] == draft["scene_ref"]
            and fingerprint(acceptance) == current["ref"]["sha256"]):
        return False
    if previous["ref"]:
        parent = kernel.artifact(repo, previous["ref"], task["work_id"], kind="NARRATIVE_ACCEPTANCE")["payload"]
        if parent["draft_ref"] != draft["prior_draft_ref"] and parent["draft_ref"] != acceptance["draft_ref"]:
            return False
    hashes = {fingerprint({"draft_ref": acceptance["draft_ref"], "reconciliation_ref": acceptance["reconciliation_ref"],
                           "world_ref": acceptance["world_ref"], "alignment_ref": alignment})
              for alignment in (None, acceptance["alignment_ref"])}
    return any(receipt["payload"] == current["ref"] and receipt["request_hash"] in hashes
               for receipt in repo.many("receipts", {"work_id": task["work_id"], "branch_id": task["branch_id"],
                                                     "command_type": "NARRATIVE_ACCEPT"})) or _edition_acceptance(kernel, repo, task, current["ref"], acceptance)


def _check_sources(kernel, repo, task, sources, input_ref, *, completed_audit=None):
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
        require(all(actual == old or _own_acceptance(kernel, repo, task, completed_audit, old, actual)
                    for old, actual in zip(expected, current, strict=True)),
                "TASK_REBIND_REQUIRED", "A selected literary source changed")
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
            SELECT * FROM nf_artifacts WHERE work_id = :work AND kind = 'NARRATIVE_DRAFT'
              AND payload #>> '{prior_draft_ref,artifact_revision_id}' = ANY(CAST(:drafts AS text[]))
              AND payload #>> '{input_world_ref,branch_id}' = :branch
        """)
        changed = repo.conn.execute(query, {"work": work, "branch": task["branch_id"], "drafts": drafts}).mappings().all()
        require(all(_known_assigned_successor(kernel, repo, task, sources, input_ref, row)
                    or _own_completed_draft(kernel, repo, task, completed_audit, row) for row in changed),
                "TASK_REBIND_REQUIRED", "A source manuscript now has a newer draft")


def _selections(repo, work, keys):
    rows = {r["selection_kind"]: r for r in repo.many("selections", {"work_id": work, "selection_kind": keys})}
    return [{"key": key, "ref": rows[key]["artifact_ref"] if key in rows else None,
             "epoch": rows[key]["epoch"] if key in rows else None} for key in sorted(keys)]


def source_fences(kernel, repo, binding, material_sources, input_ref):
    """Resolve exact issued reading/writing scopes while holding the NF work lock."""
    from novel_factory.application.authorization import Principal
    from novel_factory.application.narrative import NarrativeService

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


def load_snapshot(kernel, repo, task, input_ref, *, required=False, completed_audit=None):
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
    _check_sources(kernel, repo, task, snap["source_versions"], input_ref, completed_audit=completed_audit)
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
        saved, snapshot = load_snapshot(kernel, repo, task, audit["input_ref"], required=True, completed_audit=audit)
        require(ref == saved and all(snapshot.get(k) == audit.get(k) for k in ("dynamic_assignment_ref", "adaptive_instruction_ref")),
                "TASK_REBIND_REQUIRED", "Candidate used a different creative snapshot")
