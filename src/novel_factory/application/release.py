"""Fixed-cut parallel writing, explicit handoffs, and internal release freezing.

No world event is applied here. A release is a local immutable manifest and a
compare-and-swap pointer; it never means delivery to an external platform.
"""
from __future__ import annotations

from copy import deepcopy
from uuid import uuid4

from novel_factory.application.authorization import Principal
from novel_factory.application.kernel import require
from novel_factory.application.planning import validate_task_plan_binding
from novel_factory.application.query_policy import check_profile
from novel_factory.contracts.registry import validate_contract
from novel_factory.domain.canonical import canonical_bytes, fingerprint
from novel_factory.domain.releasing import (
    manifest_digest,
    release_diff,
    scene_readiness,
    validate_release_order,
    validate_scene_slots,
)
from novel_factory.domain.temporal import point_key


def _id():
    return str(uuid4())


def _payload(repo, kernel, ref, work_id, kind, contract=None):
    row = kernel.artifact(repo, ref, work_id, kind=kind)
    value = row["payload"]
    require(value is not None and fingerprint(value) == ref["sha256"],
            "PROJECTION_CORRUPT", "Immutable artifact payload does not match its digest")
    kernel.objects.verify(ref["sha256"])
    if contract:
        validate_contract(contract, value, "release")
    return value


def validate_scene_batch(repo, kernel, scene_payload):
    """Called by NarrativeService before registering a scene with a batch."""
    ref = scene_payload.get("batch_ref")
    if ref is None:
        return None
    scene = scene_payload["scene_contract"]
    wr = scene["input_world_ref"]
    batch = _payload(repo, kernel, ref, wr["work_id"], "WRITING_BATCH", "WritingBatch")
    slots = validate_scene_slots(batch["scene_slots"])
    slot = slots.get(scene["scene_id"])
    require(slot is not None, "BATCH_BINDING_MISMATCH", "Scene is not assigned to this writing batch")
    require(batch["world_ref"] == wr and batch["plan_ref"] == scene_payload["plan_ref"]
            and all(batch[field] == scene[field] for field in ("style_refs", "narrative_revision_id", "arrangement_version")),
            "BATCH_BINDING_MISMATCH", "Scene does not use the fixed batch inputs")
    require(slot["story_interval"] == scene["story_interval"]
            and slot["reader_cut_ordinal"] == scene["reader_cut_ordinal"]
            and slot["required_predecessor_scene_ids"] == scene_payload["required_predecessor_scene_ids"],
            "BATCH_BINDING_MISMATCH", "Scene slot or handoff contract differs from its batch")
    return batch


def _load_acceptance(repo, kernel, principal, ref, wr, *, revalidate=True, scoped_handoff=False):
    acceptance = _payload(repo, kernel, ref, wr["work_id"], "NARRATIVE_ACCEPTANCE")
    require(acceptance["world_ref"] == wr and acceptance["world_effects_applied"] == 0,
            "NARRATIVE_ALIGNMENT_REQUIRED", "Narrative acceptance uses a different adopted world cut")
    if revalidate:
        from novel_factory.application.narrative import NarrativeService
        service = NarrativeService(kernel)
        if scoped_handoff:
            # Internal service collaboration only: the caller has already
            # checked this exact reference in the writer's fixed task inputs.
            checked = service._validate_for_acceptance(
                repo, acceptance["draft_ref"], acceptance["reconciliation_ref"], wr)
        else:
            checked = service.validate_for_acceptance(
                repo, principal, acceptance["draft_ref"], acceptance["reconciliation_ref"], wr)
        require(checked["draft"]["text_ref"] == acceptance["text_ref"]
                and checked["draft"]["scene_ref"] == acceptance["scene_ref"]
                and checked["alignment_ref"] == acceptance["alignment_ref"],
                "NARRATIVE_ALIGNMENT_REQUIRED", "Acceptance does not bind the validated narrative evidence")
    else:
        checked = None
    return acceptance, checked


def validate_writing_task_binding(repo, kernel, task, scene_ref):
    """A fixed task generation cannot skip or silently replace predecessor text.

    Stage/store_draft calls this even if the caller bypasses the normal task
    dispatcher. No binding is required for legacy standalone scene drafts.
    """
    scene = _payload(repo, kernel, scene_ref, task["work_id"], "SCENE_PLAN")
    batch = validate_scene_batch(repo, kernel, scene)
    if batch is None:
        return None
    pointer = repo.one("selections", {"work_id": task["work_id"],
        "selection_kind": "WRITING_TASK:" + task["task_id"] + ":" + str(task["generation"])})
    require(pointer is not None, "SCENE_DEPENDENCY_PENDING", "Writing task has no accepted batch handoff")
    binding = _payload(repo, kernel, pointer["artifact_ref"], task["work_id"], "WRITING_TASK_BINDING", "WritingTaskBinding")
    require(binding["task_id"] == task["task_id"] and binding["generation"] == task["generation"]
            and binding["scene_ref"] == scene_ref and binding["batch_ref"] == scene["batch_ref"]
            and binding["world_ref"] == batch["world_ref"] and binding["plan_ref"] == batch["plan_ref"],
            "TASK_REBIND_REQUIRED", "Writing task binding does not match its immutable input")
    spec = repo.one("task_inputs", {"task_id": task["task_id"], "generation": task["generation"]})["spec"]
    require(spec["world_ref"] == binding["world_ref"] and all(
        item in spec["input_artifacts"] for item in (scene_ref, binding["batch_ref"], binding["plan_ref"])),
        "TASK_REBIND_REQUIRED", "Scene, batch and plan must all be fixed task inputs")
    require(all(ref in spec["input_artifacts"] for ref in binding["predecessor_acceptance_refs"]),
            "TASK_REBIND_REQUIRED", "Predecessor handoffs are absent from immutable task inputs")
    validate_task_plan_binding(repo, kernel, task, spec)
    plan_pointer = repo.one("selections", {"work_id": task["work_id"],
        "selection_kind": "PLAN_TASK:" + task["task_id"] + ":" + str(task["generation"])})
    require(plan_pointer is not None, "STALE_PLAN", "Batch writer requires a plan node binding")
    plan_binding = _payload(repo, kernel, plan_pointer["artifact_ref"], task["work_id"], "PLAN_TASK_BINDING")
    require(plan_binding["plan_ref"] == binding["plan_ref"]
            and set(plan_binding["node_ids"]) == set(scene["plan_node_ids"]),
            "STALE_PLAN", "Writer plan binding differs from the scene contract")
    principal = Principal(task["grant_id"], task["principal_id"])
    parents = []
    for ref in binding["predecessor_acceptance_refs"]:
        _, checked = _load_acceptance(repo, kernel, principal, ref, batch["world_ref"], scoped_handoff=True)
        parent = checked["scene"]
        require(parent["batch_ref"] == scene["batch_ref"], "BATCH_BINDING_MISMATCH", "Predecessor uses another writing batch")
        parents.append(parent["scene_contract"]["scene_id"])
    require(len(parents) == len(set(parents)) and set(parents) == set(scene["required_predecessor_scene_ids"]),
            "SCENE_DEPENDENCY_PENDING", "Writing task must bind exactly its accepted predecessors")
    return binding


class ReleaseService:
    def __init__(self, kernel):
        self.kernel = kernel

    @staticmethod
    def _key(key):
        require(isinstance(key, str) and 0 < len(key) <= 200, "INVALID_ARGUMENT", "Invalid idempotency key")

    def _authorize(self, repo, principal, wr, capability="READ", *, require_plan=True):
        grant = self.kernel.authorize(repo, principal, wr["work_id"], capability, wr["branch_id"])
        check_profile(grant, "WORLD")
        if require_plan:
            check_profile(grant, "PLAN")
        require(grant["actor_id"] is None, "VIEW_DENIED", "Release control is outside an actor's private view")
        revision = self.kernel.revision(repo, wr)
        require(all(point_key(p) <= point_key(grant["max_story_time"]) for p in revision["frontiers"].values()),
                "VIEW_DENIED", "Release cut exceeds the authorized story horizon")
        return grant, revision

    def _plan(self, repo, wr, plan_ref, volume_id):
        plan = _payload(repo, self.kernel, plan_ref, wr["work_id"], "PLAN_REVISION")
        require(plan["volume_id"] == volume_id and repo.ancestor(plan["input_world_ref"], wr),
                "STALE_PLAN", "Release plan and adopted world do not share an accepted lineage")
        pointer = repo.one("selections", {"work_id": wr["work_id"], "selection_kind": "PLAN:" + volume_id})
        require(pointer is not None and pointer["artifact_ref"] == plan_ref,
                "PLAN_SELECTION_CONFLICT", "This volume plan is no longer selected")
        return plan

    def create_writing_batch(self, principal, payload, idempotency_key):
        request = validate_contract("WritingBatchRequest", payload, "release")
        self._key(idempotency_key)
        wr = request["world_ref"]
        validate_scene_slots(request["scene_slots"])

        def check(repo):
            grant, _ = self._authorize(repo, principal, wr, "ARTIFACT_WRITE")
            plan = self._plan(repo, wr, request["plan_ref"], request["volume_id"])
            require(plan["input_world_ref"] == wr, "TASK_REBIND_REQUIRED", "Batch and plan must pin the same input cut")
            require(all(point_key(s["story_interval"]["end"]) <= point_key(grant["max_story_time"])
                        for s in request["scene_slots"]), "VIEW_DENIED", "Scene extends beyond the current grant")
            for ref in request["style_refs"]:
                self.kernel.artifact(repo, ref, wr["work_id"])
                self.kernel.objects.verify(ref["sha256"])

        with self.kernel.db.uow(write=False, repeatable_read=True) as repo:
            check(repo)
            prior = self.kernel._prior(repo, principal, wr["work_id"], "WRITING_BATCH", idempotency_key, request)
            if prior:
                return prior
            batch = {**request, "batch_id": _id(), "created_by": principal.principal_id, "recorded_at": repo.now().isoformat()}
        validate_contract("WritingBatch", batch, "release")
        ref = self.kernel._blob(batch)
        with self.kernel.objects.lock(), self.kernel.db.uow() as repo:
            repo.one("works", {"work_id": wr["work_id"]}, lock="update")
            check(repo)
            prior = self.kernel._prior(repo, principal, wr["work_id"], "WRITING_BATCH", idempotency_key, request)
            if prior:
                return prior
            self.kernel._register_artifact(repo, ref, wr["work_id"], batch, "WRITING_BATCH", principal.principal_id)
            self.kernel._receipt(repo, principal, wr, "WRITING_BATCH", idempotency_key, request, ref)
        return ref

    def ready_scenes(self, principal, batch_ref, predecessor_acceptance_refs=()):
        validate_contract("ArtifactRef", batch_ref)
        require(isinstance(predecessor_acceptance_refs, (list, tuple)) and len(predecessor_acceptance_refs) <= 200,
                "INVALID_ARGUMENT", "Invalid predecessor handoff list")
        for ref in predecessor_acceptance_refs:
            validate_contract("ArtifactRef", ref)
        with self.kernel.db.uow(write=False, repeatable_read=True) as repo:
            require(isinstance(principal, Principal), "VIEW_DENIED", "Authenticated identity required")
            identity = repo.one("grants", {"grant_id": principal.grant_id})
            require(identity is not None, "VIEW_DENIED", "Writing batch is unavailable")
            grant = self.kernel.authorize(repo, principal, identity["work_id"], "READ")
            # Reject unauthorized planes before artifact lookup; otherwise an
            # actor could distinguish a hidden batch from an absent one.
            check_profile(grant, "WORLD")
            check_profile(grant, "PLAN")
            require(grant["actor_id"] is None, "VIEW_DENIED", "Release control is outside an actor's private view")
            row = repo.one("artifacts", {"artifact_revision_id": batch_ref["artifact_revision_id"], "work_id": identity["work_id"]})
            require(row is not None, "VIEW_DENIED", "Writing batch is unavailable")
            wr = row["payload"]["world_ref"] if row["kind"] == "WRITING_BATCH" else None
            require(wr is not None, "VIEW_DENIED", "Writing batch is unavailable")
            self._authorize(repo, principal, wr)
            batch = _payload(repo, self.kernel, batch_ref, wr["work_id"], "WRITING_BATCH", "WritingBatch")
            self._plan(repo, wr, batch["plan_ref"], batch["volume_id"])
            accepted = []
            for ref in predecessor_acceptance_refs:
                _, checked = _load_acceptance(repo, self.kernel, principal, ref, wr)
                require(checked["scene"]["batch_ref"] == batch_ref, "BATCH_BINDING_MISMATCH", "Handoff uses another batch")
                accepted.append(checked["scene"]["scene_contract"]["scene_id"])
            require(len(accepted) == len(set(accepted)), "INVALID_ARGUMENT", "Duplicate accepted scene handoff")
            return {"batch_ref": batch_ref, "world_ref": wr, **scene_readiness(batch["scene_slots"], accepted)}

    def bind_writer_task(self, principal, task_id, generation, scene_ref, predecessor_acceptance_refs, idempotency_key):
        self._key(idempotency_key)
        validate_contract("UUID", task_id)
        validate_contract("ArtifactRef", scene_ref)
        require(type(generation) is int and generation > 0, "INVALID_ARGUMENT", "Invalid generation")
        require(isinstance(predecessor_acceptance_refs, list) and len(predecessor_acceptance_refs) <= 200,
                "INVALID_ARGUMENT", "Invalid predecessor handoff list")
        for ref in predecessor_acceptance_refs:
            validate_contract("ArtifactRef", ref)
        request = {"task_id": task_id, "generation": generation, "scene_ref": scene_ref,
                   "predecessor_acceptance_refs": predecessor_acceptance_refs}
        with self.kernel.db.uow(write=False, repeatable_read=True) as repo:
            task, _ = self.kernel._task_owner(repo, principal, task_id, "ARTIFACT_WRITE")
            spec = repo.one("task_inputs", {"task_id": task_id, "generation": generation})
            require(spec is not None and task["generation"] == generation, "STALE_GENERATION", "Task generation is no longer current")
            wr = spec["spec"]["world_ref"]
            self._authorize(repo, principal, wr, "ARTIFACT_WRITE", require_plan=False)
            require(scene_ref in spec["spec"]["input_artifacts"], "VIEW_DENIED", "Scene is outside the assigned task inputs")
            require(all(ref in spec["spec"]["input_artifacts"] for ref in predecessor_acceptance_refs),
                    "TASK_REBIND_REQUIRED", "Predecessor handoffs must be fixed before task creation")
            prior = self.kernel._prior(repo, principal, wr["work_id"], "WRITING_BIND", idempotency_key, request)
            if prior:
                return prior
            scene = _payload(repo, self.kernel, scene_ref, wr["work_id"], "SCENE_PLAN")
            require(scene.get("batch_ref") is not None, "BATCH_BINDING_MISMATCH", "Scene does not belong to a writing batch")
            validate_scene_batch(repo, self.kernel, scene)
            binding = {"schema_version": "2.2", "task_id": task_id, "generation": generation,
                       "batch_ref": scene["batch_ref"], "scene_ref": scene_ref, "world_ref": wr,
                       "plan_ref": scene["plan_ref"], "predecessor_acceptance_refs": predecessor_acceptance_refs,
                       "created_by": principal.principal_id, "recorded_at": repo.now().isoformat()}
        validate_contract("WritingTaskBinding", binding, "release")
        ref = self.kernel._blob(binding)
        with self.kernel.objects.lock(), self.kernel.db.uow() as repo:
            repo.one("works", {"work_id": wr["work_id"]}, lock="update")
            self._authorize(repo, principal, wr, "ARTIFACT_WRITE", require_plan=False)
            prior = self.kernel._prior(repo, principal, wr["work_id"], "WRITING_BIND", idempotency_key, request)
            if prior:
                return prior
            task, _ = self.kernel._task_binding(repo, principal, task_id, generation)
            key = "WRITING_TASK:" + task_id + ":" + str(generation)
            require(repo.one("selections", {"work_id": wr["work_id"], "selection_kind": key}) is None,
                    "TASK_REBIND_REQUIRED", "Writer generation already has an immutable handoff")
            self.kernel._register_artifact(repo, ref, wr["work_id"], binding, "WRITING_TASK_BINDING", principal.principal_id)
            repo.add("selections", {"work_id": wr["work_id"], "selection_kind": key, "artifact_ref": ref, "epoch": 1})
            # Any failure rolls back both the artifact row and the selection.
            validate_writing_task_binding(repo, self.kernel, task, scene_ref)
            self.kernel._receipt(repo, principal, wr, "WRITING_BIND", idempotency_key, request, ref)
        return ref

    def _collect(self, repo, principal, request):
        from novel_factory.application.continuity import guard_serial_prefix
        guard_serial_prefix(repo, request)
        wr, wid = request["world_ref"], request["world_ref"]["work_id"]
        self._authorize(repo, principal, wr, "APPROVE")
        plan = self._plan(repo, wr, request["plan_ref"], request["volume_id"])
        work = repo.one("works", {"work_id": wid})
        # Promotion deliberately preserves the branch's creation kind (REPAIR)
        # and moves the authoritative work pointer. Canon authority therefore
        # comes from that pointer, not the historical branch-origin label.
        require(work["canon_branch_id"] == wr["branch_id"],
                "BRANCH_CHANGED", "Formal internal releases require the current canonical branch")
        batches, slot_ids = {}, []
        for ref in request["batch_refs"]:
            batch = _payload(repo, self.kernel, ref, wid, "WRITING_BATCH", "WritingBatch")
            require(batch["volume_id"] == request["volume_id"]
                    and repo.ancestor(batch["world_ref"], wr)
                    and all(batch[field] == request[field] for field in ("style_refs", "narrative_revision_id", "arrangement_version")),
                    "BATCH_BINDING_MISMATCH", "Writing batch does not match the release inputs")
            batches[ref["artifact_revision_id"]] = batch
            slot_ids.extend(validate_scene_slots(batch["scene_slots"]))
        require(len(slot_ids) == len(set(slot_ids)), "RELEASE_INCOMPLETE", "Selected batches repeat a scene slot")
        entries = []
        selected_nodes = {node["node_id"]: node for node in plan["nodes"]}
        for ref in request["ordered_acceptance_refs"]:
            acceptance, checked = _load_acceptance(repo, self.kernel, principal, ref, wr)
            draft, scene = checked["draft"], checked["scene"]
            contract = scene["scene_contract"]
            selected_acceptance = repo.one("selections", {"work_id": wid,
                "selection_kind": "NARRATIVE_SCENE:" + contract["scene_id"]})
            require(selected_acceptance is not None and selected_acceptance["artifact_ref"] == ref,
                    "NARRATIVE_SELECTION_CONFLICT", "Release uses a superseded narrative acceptance")
            validate_scene_batch(repo, self.kernel, scene)
            require(scene.get("batch_ref") in request["batch_refs"], "BATCH_BINDING_MISMATCH", "Scene's batch was not selected for this release")
            require(all(contract[field] == request[field] for field in ("style_refs", "narrative_revision_id", "arrangement_version")),
                    "RELEASE_BINDING_MISMATCH", "Scene uses another narrative, style or arrangement version")
            original_plan = _payload(repo, self.kernel, scene["plan_ref"], wid, "PLAN_REVISION")
            original_nodes = {node["node_id"]: node for node in original_plan["nodes"]}
            require(original_plan["volume_id"] == request["volume_id"] and all(
                node in original_nodes and original_nodes[node] == selected_nodes.get(node) for node in scene["plan_node_ids"]),
                "STALE_PLAN", "Adopted plan changed a scene's required nodes")
            self.kernel.artifact(repo, draft["text_ref"], wid, kind="TEXT")
            self.kernel.objects.verify(draft["text_ref"]["sha256"])
            task = repo.one("tasks", {"task_id": draft["task_id"]})
            require(task is not None and task["generation"] == draft["generation"] and task["state"] == "ACCEPTED",
                    "TASK_NOT_ACCEPTABLE", "Published draft requires a completed narrative acceptance")
            binding = validate_writing_task_binding(repo, self.kernel, task, draft["scene_ref"])
            entries.append({"acceptance": acceptance, "draft": draft, "scene": scene, "binding": binding})
        validate_release_order([entry["scene"] for entry in entries], slot_ids)
        by_scene = {entry["scene"]["scene_contract"]["scene_id"]: entry for entry in entries}
        for entry in entries:
            for prior_ref in entry["binding"]["predecessor_acceptance_refs"]:
                parent = _payload(repo, self.kernel, prior_ref, wid, "NARRATIVE_ACCEPTANCE")
                parent_scene = _payload(repo, self.kernel, parent["scene_ref"], wid, "SCENE_PLAN")["scene_contract"]["scene_id"]
                selected = by_scene[parent_scene]["acceptance"]
                require(all(parent[field] == selected[field] for field in ("draft_ref", "scene_ref", "text_ref")),
                        "STALE_SCENE_HANDOFF", "Successor was written against a different predecessor text or contract")
        from novel_factory.application.editorial import ReviewService
        approval = ReviewService(self.kernel).validate_editorial_approval(
            repo, principal, request["editorial_approval_ref"], wr,
            [entry["acceptance"]["draft_ref"] for entry in entries],
            [entry["acceptance"]["reconciliation_ref"] for entry in entries],
            request["narrative_revision_id"], request["arrangement_version"], request["style_refs"], request["literary_refs"],
            plan_ref=request["plan_ref"], ordered_acceptance_refs=request["ordered_acceptance_refs"],
            disclosure_revision_id=request["disclosure_revision_id"])
        require(approval["approval"]["disposition"] == "ACCEPTED" and not approval["approval"]["conditions"],
                "APPROVAL_REQUIRED", "Release requires unconditional current editorial approval")
        prior_record = prior_manifest = None
        if request["prior_release_ref"] is not None:
            prior_record = _payload(repo, self.kernel, request["prior_release_ref"], wid, "RELEASE_RECORD", "ReleaseRecord")
            require(prior_record["volume_id"] == request["volume_id"], "RELEASE_SELECTION_CONFLICT", "Prior release belongs to another volume")
            prior_manifest = _payload(repo, self.kernel, prior_record["manifest_ref"], wid, "RELEASE_MANIFEST")
            require(request["revision_reason"] is not None, "RELEASE_REVISION_REQUIRED", "A new version requires an explicit revision reason")
            old_order = [(item["scene_id"], item["ordinal"]) for item in prior_manifest["ordered_artifacts"]]
            new_order = [(entry["scene"]["scene_contract"]["scene_id"], entry["scene"]["scene_contract"]["reader_cut_ordinal"]) for entry in entries]
            if old_order != new_order:
                require(all(request[field] != prior_manifest[field] for field in
                            ("narrative_revision_id", "arrangement_version", "disclosure_revision_id")),
                        "RELEASE_REVISION_REQUIRED", "Changed reading order requires new narrative, arrangement and disclosure versions")
        else:
            require(request["revision_reason"] is None, "INVALID_ARGUMENT", "An initial release has no revision reason")
        return {"entries": entries, "approval": approval, "prior_record": prior_record, "prior_manifest": prior_manifest,
                "policy_version": work["policy_version"]}

    def freeze(self, principal, payload, idempotency_key, *, fault=None):
        request = validate_contract("ReleaseRequest", payload, "release")
        self._key(idempotency_key)
        require(len(canonical_bytes(request)) <= 2_000_000, "CAPACITY_LIMIT", "Release request is too large")
        wr, wid = request["world_ref"], request["world_ref"]["work_id"]
        with self.kernel.objects.lock():
            with self.kernel.db.uow(write=False, repeatable_read=True) as repo:
                self._authorize(repo, principal, wr, "APPROVE")
                prior = self.kernel._prior(repo, principal, wid, "RELEASE_FREEZE", idempotency_key, request)
                if prior:
                    return prior
                checked = self._collect(repo, principal, request)
                now = repo.now().isoformat()
            entries = checked["entries"]
            release_id = _id()
            manifest = {
                "schema_version": "2.0", "release_id": release_id, "world_ref": wr,
                "narrative_revision_id": request["narrative_revision_id"], "arrangement_version": request["arrangement_version"],
                "ordered_artifacts": [{"ordinal": entry["scene"]["scene_contract"]["reader_cut_ordinal"],
                    "scene_id": entry["scene"]["scene_contract"]["scene_id"], "artifact_ref": entry["draft"]["text_ref"]} for entry in entries],
                "disclosure_revision_id": request["disclosure_revision_id"], "style_refs": request["style_refs"],
                "literary_refs": request["literary_refs"], "approval_refs": [checked["approval"]["approval"]["approval_id"]],
                "prior_release_id": checked["prior_manifest"]["release_id"] if checked["prior_manifest"] else None,
                "policy_version": checked["policy_version"], "dependency_fingerprint": fingerprint(request), "recorded_at": now,
            }
            manifest["manifest_sha256"] = manifest_digest(manifest)
            validate_contract("ReleaseManifest", manifest)
            manifest_ref = self.kernel._blob(manifest)
            record = {
                "schema_version": "2.2", "release_id": release_id, "volume_id": request["volume_id"],
                "manifest_ref": manifest_ref, "plan_ref": request["plan_ref"], "batch_refs": request["batch_refs"],
                "ordered_acceptance_refs": request["ordered_acceptance_refs"],
                "ordered_draft_refs": [entry["acceptance"]["draft_ref"] for entry in entries],
                "ordered_scene_refs": [entry["acceptance"]["scene_ref"] for entry in entries],
                "reconciliation_refs": [entry["acceptance"]["reconciliation_ref"] for entry in entries],
                "alignment_refs": [entry["acceptance"]["alignment_ref"] for entry in entries if entry["acceptance"]["alignment_ref"] is not None],
                "editorial_approval_ref": request["editorial_approval_ref"], "prior_release_ref": request["prior_release_ref"],
                "revision_reason": request["revision_reason"], "freeze_subject_sha256": fingerprint(request),
                "world_effects_applied": 0, "delivery_status": "INTERNAL_ONLY", "created_by": principal.principal_id, "recorded_at": now,
            }
            record["diff"] = release_diff(checked["prior_manifest"], checked["prior_record"], manifest, record)
            validate_contract("ReleaseRecord", record, "release")
            release_ref = self.kernel._blob(record)
            if fault:
                fault("after_objects")
            with self.kernel.db.uow() as repo:
                # Work lock covers an initially absent selection row too.
                repo.one("works", {"work_id": wid}, lock="update")
                self._authorize(repo, principal, wr, "APPROVE")
                repo.one("branches", {"branch_id": wr["branch_id"]}, lock="update")
                prior = self.kernel._prior(repo, principal, wid, "RELEASE_FREEZE", idempotency_key, request)
                if prior:
                    return prior
                key = "RELEASE:" + request["volume_id"]
                pointer = repo.one("selections", {"work_id": wid, "selection_kind": key}, lock="update")
                require((pointer["artifact_ref"] if pointer else None) == request["prior_release_ref"],
                        "RELEASE_SELECTION_CONFLICT", "Active release changed; review the intervening version")
                self._collect(repo, principal, request)
                self.kernel._register_artifact(repo, manifest_ref, wid, manifest, "RELEASE_MANIFEST", principal.principal_id)
                self.kernel._register_artifact(repo, release_ref, wid, record, "RELEASE_RECORD", principal.principal_id)
                epoch = pointer["epoch"] + 1 if pointer else 1
                if pointer:
                    repo.change("selections", {"work_id": wid, "selection_kind": key}, {"artifact_ref": release_ref, "epoch": epoch})
                else:
                    repo.add("selections", {"work_id": wid, "selection_kind": key, "artifact_ref": release_ref, "epoch": epoch})
                scope = "ARTIFACT_SELECTION:" + key
                known = repo.scope_map(wid, wr["branch_id"])
                scopes = [scope] + ([] if scope in known else ["SCOPE_REGISTRY:world"])
                updated = repo.bump(wid, wr["branch_id"], scopes)
                for changed in scopes:
                    repo.add("invalidations", {"invalidation_id": _id(), **wr,
                        "scope_key": changed, "version": updated[changed]})
                result = {"release_ref": release_ref, "manifest_ref": manifest_ref, "manifest": manifest,
                          "selection_epoch": epoch, "world_effects_applied": 0, "external_delivery": "NOT_REQUESTED"}
                validate_contract("ReleaseReceipt", result, "release")
                self.kernel._receipt(repo, principal, wr, "RELEASE_FREEZE", idempotency_key, request, result)
                repo.outbox("release.frozen", "release", release_id, result, "release:" + release_id)
                if fault:
                    fault("before_commit")
            if fault:
                fault("after_commit")
        return result

    def get_release(self, principal, work_id, release_ref):
        validate_contract("UUID", work_id)
        validate_contract("ArtifactRef", release_ref)
        with self.kernel.db.uow(write=False, repeatable_read=True) as repo:
            grant = self.kernel.authorize(repo, principal, work_id, "READ")
            check_profile(grant, "WORLD")
            check_profile(grant, "PLAN")
            require(grant["actor_id"] is None, "VIEW_DENIED", "Release records are outside an actor's private view")
            record = _payload(repo, self.kernel, release_ref, work_id, "RELEASE_RECORD", "ReleaseRecord")
            manifest = _payload(repo, self.kernel, record["manifest_ref"], work_id, "RELEASE_MANIFEST")
            self._authorize(repo, principal, manifest["world_ref"])
            require(manifest_digest(manifest) == manifest["manifest_sha256"], "PROJECTION_CORRUPT", "Release manifest digest differs")
            return {"release_ref": release_ref, "record": deepcopy(record), "manifest": deepcopy(manifest)}

    def export_release(self, principal, work_id, release_ref):
        """Return exact UTF-8 text for a local export; perform no external send."""
        with self.kernel.objects.lock():
            result = self.get_release(principal, work_id, release_ref)
            texts = []
            for item in result["manifest"]["ordered_artifacts"]:
                content = self.kernel.objects.read(item["artifact_ref"]["sha256"]).decode("utf-8")
                texts.append({"scene_id": item["scene_id"], "ordinal": item["ordinal"], "text_ref": item["artifact_ref"], "text": content})
            # Recheck current permission after potentially large object reads.
            self.get_release(principal, work_id, release_ref)
        return {"release_ref": release_ref, "manifest": result["manifest"], "scenes": texts,
                "text": "\n\n".join(item["text"] for item in texts), "external_delivery": "NOT_REQUESTED"}
