"""Fixed-cut scene planning, immutable text and evidence-bound reconciliation.

The service never calls a world mutation. New important deltas are immutable
pending proposals for the existing Kernel, and existing occurrences are links.
Text acceptance is independent of literary approval and release publication.
"""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
from uuid import uuid4

from novel_factory.application.authorization import Principal
from novel_factory.application.kernel import require
from novel_factory.application.planning import validate_task_plan_binding
from novel_factory.application.queries import WorldQueryService
from novel_factory.application.query_policy import check_profile
from novel_factory.contracts.registry import validate_contract
from novel_factory.domain.canonical import canonical_bytes, fingerprint
from novel_factory.domain.errors import DomainError
from novel_factory.domain.reconciliation import evaluate, selected, span_for, verify_span
from novel_factory.domain.scheduling import state_from_revision
from novel_factory.domain.temporal import point_key


def _id():
    return str(uuid4())


class NarrativeService:
    def __init__(self, kernel):
        self.kernel = kernel

    @staticmethod
    def _key(key):
        require(isinstance(key, str) and 0 < len(key) <= 200, "INVALID_ARGUMENT", "Invalid idempotency key")

    def _controller(self, repo, principal, wr, capability="READ"):
        grant = self.kernel.authorize(repo, principal, wr["work_id"], capability, wr["branch_id"])
        require("ADMIN" in grant["capabilities"] or "PLAN_READ" in grant["capabilities"],
                "VIEW_DENIED", "Narrative control requires an authorized planning role")
        check_profile(grant, "PLAN")
        revision = self.kernel.revision(repo, wr)
        require(all(point_key(p) <= point_key(grant["max_story_time"]) for p in revision["frontiers"].values()),
                "VIEW_DENIED", "Narrative control cut exceeds the authorized story horizon")
        return grant

    def _artifact(self, repo, ref, wid, kind):
        record = self.kernel.artifact(repo, ref, wid, kind=kind)
        self.kernel.objects.verify(ref["sha256"])
        if record["payload"] is not None:
            require(fingerprint(record["payload"]) == ref["sha256"], "ARTIFACT_CORRUPT", "Artifact content binding failed")
        return record["payload"]

    def _task(self, repo, principal, task_id, generation, capability="READ", *, allow_accepted=False):
        self.kernel._task_owner(repo, principal, task_id, capability)
        repo.one("tasks", {"task_id": task_id}, lock="share")
        task, grant, spec = WorldQueryService(self.kernel)._task(repo, principal, task_id, generation, capability)
        require(task["state"] != "ACCEPTED" or allow_accepted, "TASK_NOT_ACCEPTABLE", "Task is already complete")
        require(task["context_bundle_id"] is not None, "CONTEXT_REQUIRED", "A fixed context is required")
        context = repo.one("contexts", {"context_bundle_id": task["context_bundle_id"], "task_id": task_id,
                                       "generation": generation, "grant_id": principal.grant_id})
        require(context and context["read_cut"]["permission_epoch"] == grant["epoch"]
                and context["payload"]["world_ref"] == spec["world_ref"]
                and context["payload"]["role_id"] == grant["role_id"]
                and context["dependency_fingerprint"] == task["dependency_fingerprint"],
                "VIEW_DENIED", "Task context is no longer authorized")
        require(point_key(spec["story_time"]) <= point_key(grant["max_story_time"]),
                "VIEW_DENIED", "Task is outside its authorized story horizon")
        validate_task_plan_binding(repo, self.kernel, task, spec)
        return task, grant, spec, context

    def _origin(self, repo, draft, *, live=True):
        task = repo.one("tasks", {"task_id": draft["task_id"]})
        require(task and task["generation"] == draft["generation"], "STALE_GENERATION", "Draft source generation is no longer current")
        principal = Principal(task["grant_id"], task["principal_id"])
        grant = self.kernel.authorize(repo, principal, task["work_id"], "ARTIFACT_WRITE", task["branch_id"])
        require(task["role_id"] in {"writer", "volume_director", "control_operator"}, "VIEW_DENIED", "Draft source role is not eligible")
        spec_row = repo.one("task_inputs", {"task_id": task["task_id"], "generation": task["generation"]})
        require(spec_row and fingerprint(spec_row["spec"]) == spec_row["input_hash"], "TASK_NOT_ACCEPTABLE", "Draft input integrity failed")
        spec = spec_row["spec"]
        require(spec["world_ref"] == draft["input_world_ref"] and task["context_bundle_id"] == draft["context_bundle_id"],
                "TASK_REBIND_REQUIRED", "Draft provenance differs from the immutable task input")
        context = repo.one("contexts", {"context_bundle_id": draft["context_bundle_id"], "task_id": task["task_id"],
                                       "generation": task["generation"], "grant_id": task["grant_id"]})
        require(context and context["read_cut"]["permission_epoch"] == grant["epoch"]
                and context["dependency_fingerprint"] == task["dependency_fingerprint"],
                "VIEW_DENIED", "Draft provenance authorization changed")
        require(spec["policy_version"] == repo.one("works", {"work_id": task["work_id"]})["policy_version"],
                "TASK_REBIND_REQUIRED", "Draft policy changed")
        if task["state"] != "ACCEPTED":
            self.kernel.tasks.guard_task(repo.conn, task["task_id"], task["generation"])
        if live:
            validate_task_plan_binding(repo, self.kernel, task, spec)
        return task, grant, spec, context

    def _scene(self, repo, scene_ref, wid):
        return self._artifact(repo, scene_ref, wid, "SCENE_PLAN")

    def _draft(self, repo, draft_ref, wid):
        return self._artifact(repo, draft_ref, wid, "NARRATIVE_DRAFT")

    def load_scene(self, principal, work_id, scene_ref):
        with self.kernel.db.uow(write=False) as repo:
            self.kernel.authorize(repo, principal, work_id, "PLAN_READ")
            scene = self._scene(repo, scene_ref, work_id)
            self._controller(repo, principal, scene["scene_contract"]["input_world_ref"])
            return deepcopy(scene)

    def load_draft(self, principal, work_id, draft_ref):
        with self.kernel.db.uow(write=False) as repo:
            self.kernel.authorize(repo, principal, work_id, "PLAN_READ")
            draft = self._draft(repo, draft_ref, work_id)
            self._controller(repo, principal, draft["input_world_ref"])
            return deepcopy(draft)

    def _invalidate(self, repo, wr, scope):
        known = repo.scope_map(wr["work_id"], wr["branch_id"])
        keys = [scope] + ([] if scope in known else ["SCOPE_REGISTRY:world"])
        changed = repo.bump(wr["work_id"], wr["branch_id"], keys)
        for key in keys:
            repo.add("invalidations", {"invalidation_id": _id(), **wr, "scope_key": key, "version": changed[key]})

    def _validate_scene(self, repo, scene):
        contract = scene["scene_contract"]; wr = contract["input_world_ref"]
        revision = self.kernel.revision(repo, wr)
        interval = contract["story_interval"]
        require(interval["end"] is not None and point_key(interval["start"]) < point_key(interval["end"]),
                "INVALID_ARGUMENT", "Writing scene needs a finite nonempty interval")
        plan = self._artifact(repo, scene["plan_ref"], wr["work_id"], "PLAN_REVISION")
        active = repo.one("selections", {"work_id": wr["work_id"], "selection_kind": "PLAN:" + plan["volume_id"]})
        require(active and active["artifact_ref"] == scene["plan_ref"] and plan["input_world_ref"] == wr,
                "PLAN_SELECTION_CONFLICT", "Scene must pin the selected plan and world")
        require(set(scene["plan_node_ids"]) <= {n["node_id"] for n in plan["nodes"]}, "INVALID_ARGUMENT", "Unknown scene plan node")
        for event_ref in [*contract["event_refs"], *scene["required_event_refs"]]:
            require(selected(revision, event_ref), "INVALID_EVIDENCE", "Scene event is not selected")
        require(all(e in contract["event_refs"] for e in scene["required_event_refs"]), "INVALID_ARGUMENT", "Required scene events must be declared")
        for ref in contract["style_refs"]:
            self.kernel.artifact(repo, ref, wr["work_id"], kind="STYLE_GUIDE")
            self.kernel.objects.verify(ref["sha256"])
        require(len({d["disclosure_id"] for d in scene["allowed_disclosures"]}) == len(scene["allowed_disclosures"]),
                "INVALID_ARGUMENT", "Disclosure identities must be unique")
        for disclosure in scene["allowed_disclosures"]:
            require(all(selected(revision, ref) for ref in disclosure["source_event_refs"]),
                    "INVALID_EVIDENCE", "Disclosure sources must be selected at the scene cut")
            if disclosure["truth_status"] == "WORLD_FACT":
                require(disclosure["world_check"] is not None and disclosure["world_check"]["plane"] == "WORLD"
                        and disclosure["source_event_refs"], "INVALID_EVIDENCE", "Factual reader disclosure requires typed world and event evidence")
        if contract["pov_actor_id"] is not None:
            entity = revision["snapshot"]["entities"].get(contract["pov_actor_id"])
            require(entity and entity["entity_kind"] == "CHARACTER", "INVALID_ARGUMENT", "POV identity is unavailable")
        for side in ("entry", "exit"):
            for check in scene[side + "_checks"]:
                require(point_key(interval["start"]) <= point_key(check["at"]) < point_key(interval["end"]),
                        "INVALID_ARGUMENT", "Critical checks must lie within the scene interval")
            for commitment in contract[side + "_commitments"]:
                require(any(c["subject_id"] == commitment["subject_id"] and c["predicate"] == commitment["predicate"]
                            and c["expected_status"] == "VALUE" and c["expected_value"] == commitment["expected_value"]
                            for c in scene[side + "_checks"]), "INVALID_ARGUMENT", "Each core commitment needs a typed direct check")
        if scene["batch_ref"] is not None:
            from novel_factory.application.release import validate_scene_batch
            validate_scene_batch(repo, self.kernel, scene)
        if scene["prior_scene_ref"] is not None:
            prior = self._scene(repo, scene["prior_scene_ref"], wr["work_id"])
            require(prior["scene_contract"]["scene_id"] == contract["scene_id"], "INVALID_ARGUMENT", "Scene revision identity differs")
        if scene["scene_contract_ref"] is not None:
            require(self._artifact(repo, scene["scene_contract_ref"], wr["work_id"], "SCENE_CONTRACT") == contract,
                    "INVALID_EVIDENCE", "Scene contract reference differs from its exact payload")

    def save_scene(self, principal, payload, idempotency_key):
        self._key(idempotency_key)
        value = deepcopy(payload); value.setdefault("scene_contract_ref", None)
        scene = validate_contract("ScenePlan", value, "narrative")
        wr = scene["scene_contract"]["input_world_ref"]; command = "NARRATIVE_SCENE"
        request = deepcopy(scene)
        with self.kernel.db.uow(write=False) as repo:
            self._controller(repo, principal, wr, "ARTIFACT_WRITE")
            old = self.kernel._prior(repo, principal, wr["work_id"], command, idempotency_key, request)
            if old:
                return old
            self._validate_scene(repo, scene)
        contract_ref = scene["scene_contract_ref"] or self.kernel._blob(scene["scene_contract"])
        scene["scene_contract_ref"] = contract_ref
        ref = self.kernel._blob(scene)
        with self.kernel.objects.lock(), self.kernel.db.uow() as repo:
            repo.one("works", {"work_id": wr["work_id"]}, lock="update")
            self._controller(repo, principal, wr, "ARTIFACT_WRITE")
            old = self.kernel._prior(repo, principal, wr["work_id"], command, idempotency_key, request)
            if old:
                return old
            if request["scene_contract_ref"] is None:
                self.kernel._register_artifact(repo, contract_ref, wr["work_id"], scene["scene_contract"], "SCENE_CONTRACT", principal.principal_id)
            self._validate_scene(repo, scene)
            key = "SCENE:" + scene["scene_contract"]["scene_id"]
            current = repo.one("selections", {"work_id": wr["work_id"], "selection_kind": key})
            require((current["artifact_ref"] if current else None) == scene["prior_scene_ref"],
                    "SCENE_SELECTION_CONFLICT", "Scene selection changed; create an explicit revision")
            self.kernel._register_artifact(repo, ref, wr["work_id"], scene, "SCENE_PLAN", principal.principal_id)
            if current:
                repo.change("selections", {"work_id": wr["work_id"], "selection_kind": key}, {"artifact_ref": ref, "epoch": current["epoch"] + 1})
            else:
                repo.add("selections", {"work_id": wr["work_id"], "selection_kind": key, "artifact_ref": ref, "epoch": 1})
            scope = "ARTIFACT_SELECTION:" + key
            self._invalidate(repo, wr, scope)
            self.kernel._receipt(repo, principal, wr, command, idempotency_key, request, ref)
        return ref

    def _writer_binding(self, repo, principal, task_id, generation, scene_ref):
        task, grant, spec, context = self._task(repo, principal, task_id, generation, "ARTIFACT_WRITE")
        require(grant["role_id"] in {"writer", "volume_director", "control_operator"}, "VIEW_DENIED", "Role cannot submit scene prose")
        require(scene_ref in spec["input_artifacts"], "VIEW_DENIED", "Scene is outside immutable task inputs")
        scene = self._scene(repo, scene_ref, task["work_id"])
        require(scene["scene_contract"]["input_world_ref"] == spec["world_ref"] and scene["plan_ref"] in spec["input_artifacts"],
                "TASK_REBIND_REQUIRED", "Writer scene and plan must share the fixed task cut")
        binding = repo.one("selections", {"work_id": task["work_id"], "selection_kind": "PLAN_TASK:" + task_id + ":" + str(generation)})
        require(binding is not None, "VIEW_DENIED", "Writer requires controller-authorized plan binding")
        bound = self._artifact(repo, binding["artifact_ref"], task["work_id"], "PLAN_TASK_BINDING")
        require(bound["plan_ref"] == scene["plan_ref"] and set(scene["plan_node_ids"]) <= set(bound["node_ids"]),
                "VIEW_DENIED", "Writer plan scope does not cover this scene")
        current = repo.one("selections", {"work_id": task["work_id"], "selection_kind": "SCENE:" + scene["scene_contract"]["scene_id"]})
        require(current and current["artifact_ref"] == scene_ref, "SCENE_SELECTION_CONFLICT", "Writer scene has changed")
        require(point_key(scene["scene_contract"]["story_interval"]["end"]) <= point_key(spec["story_time"]),
                "VIEW_DENIED", "Scene exceeds writer story horizon")
        for event_ref in scene["scene_contract"]["event_refs"]:
            event = repo.one("events", {"work_id": task["work_id"], **event_ref})
            require(event and event["payload"]["occurrence_time"]["kind"] == "EXACT"
                    and point_key(event["payload"]["occurrence_time"]["at"]) <= point_key(grant["max_story_time"]),
                    "VIEW_DENIED", "Scene evidence exceeds writer's authorized story horizon")
        if scene["batch_ref"] is not None:
            from novel_factory.application.release import validate_writing_task_binding
            validate_writing_task_binding(repo, self.kernel, task, scene_ref)
        return task, grant, spec, context, scene

    def read_scene_for_task(self, principal, task_id, generation, scene_ref):
        """Writing card only; a writer never receives the raw selected plan."""
        with self.kernel.db.uow(write=False) as repo:
            *_, scene = self._writer_binding(repo, principal, task_id, generation, scene_ref)
            return {"scene_contract": deepcopy(scene["scene_contract"]), "required_event_refs": deepcopy(scene["required_event_refs"]),
                    "literary_purpose": scene["literary_purpose"], "allowed_disclosures": deepcopy(scene["allowed_disclosures"]),
                    "entry_checks": deepcopy(scene["entry_checks"]), "exit_checks": deepcopy(scene["exit_checks"])}

    def store_draft(self, principal, task_id, generation, scene_ref, text, idempotency_key, *, title="Untitled",
                    declared_important_deltas=None, prior_draft_ref=None, handoff_refs=None, agent_output_ref=None):
        self._key(idempotency_key)
        require(isinstance(text, str) and 0 < len(text.encode("utf-8")) <= 1_000_000, "CAPACITY_LIMIT", "Text is empty or exceeds capacity")
        request = {"task_id": task_id, "generation": generation, "scene_ref": scene_ref, "text_sha256": sha256(text.encode()).hexdigest(),
                   "title": title, "declared_important_deltas": declared_important_deltas or [], "prior_draft_ref": prior_draft_ref,
                   "handoff_refs": handoff_refs or [], "agent_output_ref": agent_output_ref}

        def check(repo):
            task, grant, spec, context, scene = self._writer_binding(repo, principal, task_id, generation, scene_ref)
            if agent_output_ref is not None:
                output = self._artifact(repo, agent_output_ref, task["work_id"], "AGENT_CANDIDATE")
                result = repo.one("task_results", {"result_id": task["current_result_id"]}) if task["current_result_id"] else None
                require(result and result["disposition"] == "CURRENT" and fingerprint(result["payload"]) == result["payload_hash"]
                        and result["task_id"] == task_id and result["generation"] == generation
                        and agent_output_ref in result["payload"]["artifacts"], "INVALID_EVIDENCE", "Model output is not this task's current result")
                require(output.get("text") == text and output.get("important_deltas", []) == request["declared_important_deltas"],
                        "INVALID_EVIDENCE", "Draft text or declarations differ from the saved model candidate")
            if prior_draft_ref is not None:
                require(prior_draft_ref in spec["input_artifacts"], "VIEW_DENIED", "Prior draft is absent from explicit source qualification")
                prior = self._draft(repo, prior_draft_ref, task["work_id"])
                require(prior["scene_ref"] in [scene_ref, scene["prior_scene_ref"]]
                        and self._scene(repo, prior["scene_ref"], task["work_id"])["scene_contract"]["scene_id"] == scene["scene_contract"]["scene_id"],
                        "INVALID_ARGUMENT", "Prior draft belongs to another scene")
            if scene["batch_ref"] is not None:
                from novel_factory.application.release import validate_writing_task_binding
                bound = validate_writing_task_binding(repo, self.kernel, task, scene_ref)
                require(request["handoff_refs"] == bound["predecessor_acceptance_refs"],
                        "INVALID_EVIDENCE", "Draft must preserve the exact authorized predecessor handoff")
            for ref in request["handoff_refs"]:
                require(ref in spec["input_artifacts"], "VIEW_DENIED", "Handoff is not a frozen task input")
                self.kernel.artifact(repo, ref, task["work_id"], kind="NARRATIVE_ACCEPTANCE")
            return task, spec

        with self.kernel.db.uow(write=False) as repo:
            task, _ = self.kernel._task_owner(repo, principal, task_id, "ARTIFACT_WRITE")
            old = self.kernel._prior(repo, principal, task["work_id"], "NARRATIVE_DRAFT", idempotency_key, request)
            if old:
                return old
            task, spec = check(repo)
        text_ref = self.kernel._blob(text.encode("utf-8"))
        payload = validate_contract("DraftRecord", {"schema_version": "2.2", "draft_id": _id(), "scene_ref": scene_ref,
            "text_ref": text_ref, "input_world_ref": spec["world_ref"], "task_id": task_id, "generation": generation,
            "context_bundle_id": task["context_bundle_id"], "prior_draft_ref": prior_draft_ref, "title": title,
            "declared_important_deltas": request["declared_important_deltas"], "handoff_refs": request["handoff_refs"],
            "agent_output_ref": agent_output_ref}, "narrative")
        ref = self.kernel._blob(payload)
        with self.kernel.objects.lock(), self.kernel.db.uow() as repo:
            repo.one("works", {"work_id": task["work_id"]}, lock="update")
            self.kernel._task_owner(repo, principal, task_id, "ARTIFACT_WRITE")
            old = self.kernel._prior(repo, principal, task["work_id"], "NARRATIVE_DRAFT", idempotency_key, request)
            if old:
                return old
            check(repo)
            self.kernel._register_artifact(repo, text_ref, task["work_id"], text.encode("utf-8"), "TEXT", principal.principal_id)
            self.kernel._register_artifact(repo, ref, task["work_id"], payload, "NARRATIVE_DRAFT", principal.principal_id)
            from novel_factory.application.audit_log import LogService
            LogService.artifact(repo.conn, "NARRATIVE_DRAFT", ref["artifact_revision_id"], task["work_id"],
                                payload, principal.principal_id, text_characters=len(text))
            self.kernel._receipt(repo, principal, spec["world_ref"], "NARRATIVE_DRAFT", idempotency_key, request, ref)
            repo.outbox("narrative.draft", "artifact", ref["artifact_revision_id"], {"draft_ref": ref, "world_ref": spec["world_ref"]}, "draft:" + ref["artifact_revision_id"])
        return ref

    stage_draft = store_draft

    def bind_reader_task(self, principal, task_id, generation, draft_refs, idempotency_key):
        """Authorize exact text, not arbitrary TaskSpec artifact handles."""
        self._key(idempotency_key)
        request = {"task_id": task_id, "generation": generation, "draft_refs": draft_refs}

        def check(repo):
            task = repo.one("tasks", {"task_id": task_id})
            require(task, "VIEW_DENIED", "Task is unavailable")
            self.kernel.authorize(repo, principal, task["work_id"], "ADMIN", task["branch_id"])
            target = Principal(task["grant_id"], task["principal_id"])
            task, grant, spec, context = self._task(repo, target, task_id, generation)
            require(grant["role_id"] in {"writer", "reviewer", "evidence_maintainer", "literary_editor", "chief_editor", "volume_director", "control_operator"},
                    "VIEW_DENIED", "Role is outside literary reading scope")
            plane = context["read_cut"]["knowledge_cut"]["plane"]
            require(plane in {"WORLD", "PLAN", "LITERARY", "READER"}, "VIEW_DENIED", "A literary author cut is required")
            check_profile(grant, plane)
            for ref in draft_refs:
                require(ref in spec["input_artifacts"], "VIEW_DENIED", "Reading material is absent from fixed inputs")
                draft = self._draft(repo, ref, task["work_id"])
                require(repo.ancestor(draft["input_world_ref"], spec["world_ref"]), "VIEW_DENIED", "Text exceeds the assigned world cut")
                scene = self._scene(repo, draft["scene_ref"], task["work_id"])
                require(point_key(scene["scene_contract"]["story_interval"]["end"]) <= point_key(spec["story_time"]),
                        "VIEW_DENIED", "Text exceeds the assigned story horizon")
            return task, grant, spec

        with self.kernel.db.uow(write=False) as repo:
            task, grant, spec = check(repo)
            old = self.kernel._prior(repo, principal, task["work_id"], "NARRATIVE_READ_BIND", idempotency_key, request)
            if old:
                return old
        payload = validate_contract("ReadingScope", {"schema_version": "2.2", "task_id": task_id,
            "generation": generation, "world_ref": spec["world_ref"], "draft_refs": draft_refs,
            "grant_id": task["grant_id"], "permission_epoch": grant["epoch"]}, "narrative")
        ref = self.kernel._blob(payload)
        with self.kernel.objects.lock(), self.kernel.db.uow() as repo:
            repo.one("works", {"work_id": task["work_id"]}, lock="update")
            task, grant, spec = check(repo)
            old = self.kernel._prior(repo, principal, task["work_id"], "NARRATIVE_READ_BIND", idempotency_key, request)
            if old:
                return old
            require(grant["epoch"] == payload["permission_epoch"], "VIEW_DENIED", "Reader authorization changed")
            key = "NARRATIVE_READ:" + task_id + ":" + str(generation)
            require(repo.one("selections", {"work_id": task["work_id"], "selection_kind": key}) is None,
                    "TASK_REBIND_REQUIRED", "Reading scope is immutable in a task generation")
            self.kernel._register_artifact(repo, ref, task["work_id"], payload, "NARRATIVE_READ_SCOPE", principal.principal_id)
            repo.add("selections", {"work_id": task["work_id"], "selection_kind": key, "artifact_ref": ref, "epoch": 1})
            self.kernel._receipt(repo, principal, spec["world_ref"], "NARRATIVE_READ_BIND", idempotency_key, request, ref)
        return ref

    def _reader_binding(self, repo, principal, task_id, generation, draft_ref):
        task, grant, spec, context = self._task(repo, principal, task_id, generation)
        require(grant["role_id"] in {"writer", "reviewer", "evidence_maintainer", "literary_editor", "chief_editor", "volume_director", "control_operator"},
                "VIEW_DENIED", "Role cannot read literary source text")
        plane = context["read_cut"]["knowledge_cut"]["plane"]
        require(plane in {"WORLD", "PLAN", "LITERARY", "READER"}, "VIEW_DENIED", "Text is outside this knowledge plane")
        check_profile(grant, plane)
        scoped = repo.one("selections", {"work_id": task["work_id"], "selection_kind": "NARRATIVE_READ:" + task_id + ":" + str(generation)})
        # Read no draft payload until the request has a trusted material fence.
        own = repo.one("artifacts", {"work_id": task["work_id"], "artifact_revision_id": draft_ref["artifact_revision_id"],
                                      "created_by": principal.principal_id, "kind": "NARRATIVE_DRAFT"})
        own_current = own and own["payload"]["task_id"] == task_id and own["payload"]["generation"] == generation
        if not own_current:
            require(scoped is not None, "VIEW_DENIED", "Text is outside the authorized reading assignment")
            scope = self._artifact(repo, scoped["artifact_ref"], task["work_id"], "NARRATIVE_READ_SCOPE")
            require(scope["world_ref"] == spec["world_ref"] and scope["grant_id"] == principal.grant_id
                    and scope["permission_epoch"] == grant["epoch"] and draft_ref in scope["draft_refs"]
                    and draft_ref in spec["input_artifacts"], "VIEW_DENIED", "Text is outside the authorized reading assignment")
        draft = self._draft(repo, draft_ref, task["work_id"])
        scene = self._scene(repo, draft["scene_ref"], task["work_id"])
        require(repo.ancestor(draft["input_world_ref"], spec["world_ref"])
                and point_key(scene["scene_contract"]["story_interval"]["end"]) <= point_key(spec["story_time"]),
                "VIEW_DENIED", "Text is outside this task cut")
        self._artifact(repo, draft["text_ref"], task["work_id"], "TEXT")
        return task, grant, spec, draft

    def read_text(self, principal, task_id, generation, draft_ref, start=None, end=None):
        validate_contract("ArtifactRef", draft_ref)
        with self.kernel.db.uow(write=False) as repo:
            task, _, spec, draft = self._reader_binding(repo, principal, task_id, generation, draft_ref)
        content = self.kernel.objects.read(draft["text_ref"]["sha256"])
        text = content.decode("utf-8")
        span = span_for(text, 0 if start is None else start, end)
        delivery = validate_contract("TextDelivery", {"schema_version": "2.2", "task_id": task_id, "generation": generation,
            "context_bundle_id": task["context_bundle_id"], "draft_ref": draft_ref, "text_ref": draft["text_ref"],
            "scene_ref": draft["scene_ref"], "world_ref": spec["world_ref"], "span": span,
            "read_mode": "FULL_TEXT" if span["start"] == 0 and span["end"] == len(text) else "EXCERPT",
            "delivery_semantics": "EXACT_BYTES_OFFERED_NOT_UNDERSTANDING"}, "narrative")
        ref = self.kernel._blob(delivery)
        with self.kernel.objects.lock(), self.kernel.db.uow() as repo:
            self._reader_binding(repo, principal, task_id, generation, draft_ref)
            self.kernel._register_artifact(repo, ref, task["work_id"], delivery, "TEXT_DELIVERY", principal.principal_id)
        return {"text": text[span["start"]:span["end"]], "text_ref": draft["text_ref"], "draft_ref": draft_ref,
                "scene_ref": draft["scene_ref"], "world_ref": spec["world_ref"], "span": span, "delivery_ref": ref}

    def submit_extraction(self, principal, task_id, generation, draft_ref, delivery_ref, assertions, idempotency_key):
        self._key(idempotency_key)
        assertions = [validate_contract("AssertionCandidate", a, "narrative") for a in assertions]
        request = {"task_id": task_id, "generation": generation, "draft_ref": draft_ref, "delivery_ref": delivery_ref, "assertions": assertions}

        def check(repo):
            task, grant, spec, draft = self._reader_binding(repo, principal, task_id, generation, draft_ref)
            self.kernel.authorize(repo, principal, task["work_id"], "ARTIFACT_WRITE", task["branch_id"])
            require(grant["role_id"] in {"evidence_maintainer", "reviewer", "control_operator"}, "VIEW_DENIED", "Role cannot submit factual extraction")
            delivery = self._artifact(repo, delivery_ref, task["work_id"], "TEXT_DELIVERY")
            require(delivery["task_id"] == task_id and delivery["generation"] == generation and delivery["draft_ref"] == draft_ref
                    and delivery["text_ref"] == draft["text_ref"] and delivery["context_bundle_id"] == task["context_bundle_id"]
                    and delivery["read_mode"] == "FULL_TEXT", "INVALID_EVIDENCE", "Extraction needs full exact text delivered to this task")
            return task, spec, draft

        with self.kernel.db.uow(write=False) as repo:
            task, spec, draft = check(repo)
            old = self.kernel._prior(repo, principal, task["work_id"], "NARRATIVE_EXTRACTION", idempotency_key, request)
            if old:
                return old
        text = self.kernel.objects.read(draft["text_ref"]["sha256"]).decode("utf-8")
        for assertion in assertions:
            verify_span(text, assertion["span"])
        payload = validate_contract("ExtractionRecord", {"schema_version": "2.2", "extraction_id": _id(),
            "draft_ref": draft_ref, "text_ref": draft["text_ref"], "scene_ref": draft["scene_ref"], "task_id": task_id,
            "generation": generation, "context_bundle_id": task["context_bundle_id"], "delivery_ref": delivery_ref,
            "assertions": assertions, "coverage": "FULL_TEXT_DELIVERED", "semantic_completeness": "REQUIRES_INDEPENDENT_REVIEW"}, "narrative")
        ref = self.kernel._blob(payload)
        with self.kernel.objects.lock(), self.kernel.db.uow() as repo:
            repo.one("works", {"work_id": task["work_id"]}, lock="update")
            check(repo)
            old = self.kernel._prior(repo, principal, task["work_id"], "NARRATIVE_EXTRACTION", idempotency_key, request)
            if old:
                return old
            self.kernel._register_artifact(repo, ref, task["work_id"], payload, "ASSERTION_EXTRACTION", principal.principal_id)
            self.kernel._receipt(repo, principal, spec["world_ref"], "NARRATIVE_EXTRACTION", idempotency_key, request, ref)
        return ref

    def _extraction(self, repo, extraction_ref, draft_ref, draft):
        wid = draft["input_world_ref"]["work_id"]
        extraction = self._artifact(repo, extraction_ref, wid, "ASSERTION_EXTRACTION")
        require(extraction["draft_ref"] == draft_ref and extraction["text_ref"] == draft["text_ref"] and extraction["scene_ref"] == draft["scene_ref"],
                "INVALID_EVIDENCE", "Extraction does not bind this exact draft and text")
        task = repo.one("tasks", {"task_id": extraction["task_id"]})
        require(task and task["generation"] == extraction["generation"], "STALE_GENERATION", "Extraction generation is no longer current")
        principal = Principal(task["grant_id"], task["principal_id"])
        _, grant, _, _ = self._reader_binding(repo, principal, task["task_id"], task["generation"], draft_ref)
        require(grant["role_id"] in {"evidence_maintainer", "reviewer", "control_operator"}, "VIEW_DENIED", "Extraction source is not eligible")
        delivery = self._artifact(repo, extraction["delivery_ref"], wid, "TEXT_DELIVERY")
        require(delivery["task_id"] == task["task_id"] and delivery["generation"] == task["generation"]
                and delivery["context_bundle_id"] == extraction["context_bundle_id"]
                and delivery["draft_ref"] == draft_ref and delivery["text_ref"] == draft["text_ref"]
                and delivery["read_mode"] == "FULL_TEXT", "INVALID_EVIDENCE", "Extraction coverage is not full exact source text")
        return extraction, task, grant

    def _resolved(self, repo, revision, assertions, resolutions):
        ids = {a["assertion_id"]: a for a in assertions}
        seen, events = set(), []
        for resolution in resolutions:
            aid = resolution["assertion_id"]
            require(aid in ids and aid not in seen and ids[aid]["proposed_command"] is not None,
                    "INVALID_ARGUMENT", "Resolution must bind one unique proposed important change")
            seen.add(aid)
            event_ref = resolution["event_ref"]
            require(selected(revision, event_ref), "INVALID_EVIDENCE", "Resolved world event is not selected")
            event = repo.one("events", {"work_id": revision["work_id"], **event_ref})
            require(event, "INVALID_EVIDENCE", "Resolved event is unavailable")
            self.kernel.objects.verify(event["object_hash"])
            closure = self._artifact(repo, event["payload"]["adjudication_ref"], revision["work_id"], "CLOSURE")
            command = ids[aid]["proposed_command"]
            require(command["template_id"] != "REPAIR_EVENT" and closure.get("kind") == "CONTROL_PLANE_CLOSED_COMMAND"
                    and closure.get("template_id") == command["template_id"] and closure.get("args_sha256") == fingerprint(command["args"]),
                    "INVALID_EVIDENCE", "Accepted event does not implement this exact pending command")
            events.append(event_ref)
        return seen, events

    def _binding_data(self, repo, revision):
        wid = revision["work_id"]
        work = repo.one("works", {"work_id": wid})
        interpretation = self._artifact(repo, revision["interpretation_ref"], wid, "INTERPRETATION")
        genesis = repo.one("genesis", {"work_id": wid})
        rows = repo.many("events", {"work_id": wid, "event_version_id": list(revision["selection"].values())}) if revision["selection"] else []
        require(len(rows) == len(revision["selection"]), "INVALID_EVIDENCE", "Selected event manifest is incomplete")
        effects = []
        for row in rows:
            event = row["payload"]
            require(selected(revision, {"occurrence_id": event["occurrence_id"], "event_version_id": event["event_version_id"]})
                    and fingerprint(event) == row["object_hash"], "INVALID_EVIDENCE", "Selected event integrity failed")
            for effect in event["effects"]:
                effects.append({"occurrence_id": event["occurrence_id"], "event_version_id": event["event_version_id"],
                    "effect_group_id": event["effect_group_id"], "representation_id": event["representation_id"],
                    "effect_key": effect["effect_key"], "effect_sha256": fingerprint(effect)})
        effects.sort(key=lambda e: (e["occurrence_id"], e["event_version_id"], e["effect_key"]))
        schedule = state_from_revision(revision)
        return {"world_ref": {k: revision[k] for k in ("work_id", "branch_id", "world_revision_id")},
                "record_selection_manifest_sha256": fingerprint({"genesis_sha256": genesis["payload_hash"], "event_selection": revision["selection"]}),
                "effect_selection_manifest_sha256": fingerprint(effects), "projector_version": interpretation["projector"],
                "ruleset_refs": [work["rules_ref"]], "registry_version": fingerprint(schedule),
                "policy_version": work["policy_version"], "interpretation_ref": revision["interpretation_ref"]}

    def _world_binding(self, repo, revision, scene, proof_ref, extraction):
        binding = self._binding_data(repo, revision)
        coverage = []
        checks = [*scene["entry_checks"], *scene["exit_checks"],
                  *(d["world_check"] for d in scene["allowed_disclosures"] if d["world_check"] is not None),
                  *(a["world_check"] for a in extraction["assertions"] if a["world_check"] is not None)]
        points = {(c["subject_id"], c["at"]["tick"], c["at"]["phase"]) for c in checks}
        for check in checks:
            if check["plane"] == "BELIEF":
                points.add((check["actor_id"], check["known_at"]["tick"], check["known_at"]["phase"]))
        for assertion in extraction["assertions"]:
            use = assertion["knowledge_use"]
            if use is not None and use["mode"] in {"KNOWS", "BELIEVES"}:
                points.add((use["subject_id"], use["at"]["tick"], use["at"]["phase"]))
                points.add((use["actor_id"], use["at"]["tick"], use["at"]["phase"]))
        for subject, tick, phase in sorted(points):
            at = {"tick": tick, "phase": phase}
            end = {"tick": tick, "phase": phase + 1} if phase < 2147483647 else {"tick": tick + 1, "phase": 0}
            frontier = revision["frontiers"].get(subject)
            if frontier and point_key(frontier) >= point_key(end):
                coverage.append({"scope_key": "PROCESS_COVERAGE:" + subject,
                                 "story_interval": {"start": at, "end": end, "bounds": "[)"}, "proof_ref": proof_ref})
        binding["scope_coverage"] = coverage
        return validate_contract("WorldStateBinding", binding, "narrative")

    def _validate_knowledge_evidence(self, repo, revision, assertions):
        for assertion in assertions:
            use = assertion["knowledge_use"]
            if use is None:
                continue
            for evidence in use["evidence_refs"]:
                if evidence["kind"] == "EVENT":
                    source_wr, event_ref = evidence["world_ref"], evidence["event_ref"]
                    require(source_wr["work_id"] == revision["work_id"], "INVALID_EVIDENCE", "Cognition source belongs to another work")
                    source = self.kernel.revision(repo, source_wr)
                    require(selected(source, event_ref), "INVALID_EVIDENCE", "Cognition source event is unavailable at its declared world")
                    event = repo.one("events", {"work_id": revision["work_id"], **event_ref})
                    require(event, "INVALID_EVIDENCE", "Cognition source event is unavailable")
                    self.kernel.objects.verify(event["object_hash"])
                    moment = event["payload"]["occurrence_time"]
                    require(moment["kind"] == "EXACT" and point_key(moment["at"]) <= point_key(use["at"]),
                            "INVALID_EVIDENCE", "Cognition source is later than the claimed acquisition")
                else:
                    ref = evidence["artifact_ref"]
                    artifact = self.kernel.artifact(repo, ref, revision["work_id"])
                    self.kernel.objects.verify(ref["sha256"])
                    if evidence["kind"] == "RULE":
                        require(artifact["kind"] == "RULESET", "INVALID_EVIDENCE", "Rule evidence must be an accepted ruleset")
                    if "span" in evidence:
                        verify_span(self.kernel.objects.read(ref["sha256"]).decode("utf-8"), evidence["span"])

    def _report_inputs(self, repo, principal, draft_ref, extraction_ref, wr, resolutions):
        self._controller(repo, principal, wr, "ARTIFACT_WRITE")
        draft = self._draft(repo, draft_ref, wr["work_id"])
        origin, origin_grant, _, context = self._origin(repo, draft)
        require(repo.ancestor(draft["input_world_ref"], wr), "BRANCH_CHANGED", "Alignment requires a successor of the exact source branch")
        scene = self._scene(repo, draft["scene_ref"], wr["work_id"])
        extraction, extraction_task, extraction_grant = self._extraction(repo, extraction_ref, draft_ref, draft)
        revision = self.kernel.revision(repo, wr)
        require(revision["interpretation_ref"] == repo.one("works", {"work_id": wr["work_id"]})["interpretation_ref"],
                "NARRATIVE_ALIGNMENT_REQUIRED", "World interpretation requires renewed validation")
        self._validate_knowledge_evidence(repo, revision, extraction["assertions"])
        resolved, resolution_events = self._resolved(repo, revision, extraction["assertions"], resolutions)
        return draft, scene, extraction, revision, resolved, resolution_events, origin, origin_grant, extraction_task, extraction_grant, context

    @staticmethod
    def _issue(draft_ref, blocker):
        return {"issue_id": _id(), "category": "HARD_CONSISTENCY", "severity": "BLOCKER", "subject_ref": draft_ref,
                "description": blocker["message"], "evidence_refs": [], "suggested_change": "Revise the text or complete the authorized world and narrative review",
                "disposition": "OPEN", "issue_kind": "HARD_CONFLICT"}

    def reconcile(self, principal, draft_ref, extraction_ref, world_ref, idempotency_key, *, resolution_event_refs=None):
        self._key(idempotency_key)
        resolutions = [validate_contract("Resolution", r, "narrative") for r in (resolution_event_refs or [])]
        request = {"draft_ref": draft_ref, "extraction_ref": extraction_ref, "world_ref": world_ref, "resolutions": resolutions}
        with self.kernel.db.uow(write=False) as repo:
            self._controller(repo, principal, world_ref, "ARTIFACT_WRITE")
            old = self.kernel._prior(repo, principal, world_ref["work_id"], "NARRATIVE_RECONCILE", idempotency_key, request)
            if old:
                return old
            data = self._report_inputs(repo, principal, draft_ref, extraction_ref, world_ref, resolutions)
        draft, scene, extraction, revision, resolved, resolution_events, origin, origin_grant, ext_task, ext_grant, context = data
        text = self.kernel.objects.read(draft["text_ref"]["sha256"]).decode("utf-8")
        result = evaluate(revision, scene, draft, extraction["assertions"], text, resolved)
        linked = {(e["occurrence_id"], e["event_version_id"]): e for e in [*result["event_refs"], *resolution_events]}
        result["event_refs"] = [linked[key] for key in sorted(linked)]
        pending, staged = [], []
        for assertion in extraction["assertions"]:
            if assertion["proposed_command"] is not None and assertion["assertion_id"] not in resolved:
                delta = validate_contract("NarrativeDelta", {"schema_version": "2.2", "delta_id": _id(), "draft_ref": draft_ref,
                    "assertion_id": assertion["assertion_id"], "input_world_ref": draft["input_world_ref"],
                    "proposed_command": assertion["proposed_command"], "source_span": assertion["span"],
                    "status": "PENDING_WORLD_REVIEW", "world_effects_applied": 0}, "narrative")
                delta_ref = self.kernel._blob(delta); pending.append(delta_ref); staged.append((delta_ref, delta, "NARRATIVE_WORLD_DELTA"))
        proof = validate_contract("ReconciliationProof", {"schema_version": "2.2", "input_world_ref": draft["input_world_ref"],
            "adopted_world_ref": world_ref, "scene_ref": draft["scene_ref"], "draft_ref": draft_ref, "extraction_ref": extraction_ref,
            "interpretation_ref": revision["interpretation_ref"], "world_manifest_sha256": revision["manifest_hash"],
            "frontiers_sha256": fingerprint(revision["frontiers"]), "boundary_checks": result["checks"],
            "semantics": "SERVER_RECOMPUTED_AUTHORITATIVE_CHECKS"}, "narrative")
        proof_ref = self.kernel._blob(proof); staged.append((proof_ref, proof, "NARRATIVE_VERIFICATION_PROOF"))
        with self.kernel.db.uow(write=False) as repo:
            self._controller(repo, principal, world_ref, "ARTIFACT_WRITE")
            binding = self._world_binding(repo, revision, scene, proof_ref, extraction)
        status = "BLOCKED" if result["blockers"] else "CLEAR"
        report = validate_contract("ReconciliationReport", {"schema_version": "2.2", "report_id": _id(), "draft_ref": draft_ref,
            "text_ref": draft["text_ref"], "scene_ref": draft["scene_ref"], "extraction_ref": extraction_ref,
            "input_world_ref": draft["input_world_ref"], "adopted_world_ref": world_ref, "status": status,
            "event_refs": result["event_refs"], "pending_delta_refs": pending, "hard_blockers": result["blockers"],
            "resolutions": resolutions, "classified_assertion_ids": [a["assertion_id"] for a in extraction["assertions"]],
            "source_coverage": "FULL_TEXT_DELIVERED", "semantic_completeness": "REQUIRES_INDEPENDENT_REVIEW",
            "boundary_checks_sha256": fingerprint(result["checks"]), "world_binding": binding,
            "critical_commitment_checks": result["checks"], "world_effects_applied": 0}, "narrative")
        report_ref = self.kernel._blob(report); staged.append((report_ref, report, "RECONCILIATION_REPORT"))
        issues = [self._issue(draft_ref, b) for b in result["blockers"]]
        delta_by_assertion = {p[1]["assertion_id"]: p[0] for p in staged if p[2] == "NARRATIVE_WORLD_DELTA"}
        segments = []
        for assertion in extraction["assertions"]:
            aid = assertion["assertion_id"]
            refs = [assertion["event_ref"]] if assertion["event_ref"] else []
            refs += [r["event_ref"] for r in resolutions if r["assertion_id"] == aid]
            segments.append({"text_evidence": {"kind": "ARTIFACT", "artifact_ref": draft_ref, "span": assertion["span"]},
                "classification": assertion["classification"], "event_refs": refs,
                "proposed_change_refs": [delta_by_assertion[aid]] if aid in delta_by_assertion else [],
                "issues": [i for i, b in zip(issues, result["blockers"]) if b["assertion_id"] == aid]})
        contract = scene["scene_contract"]
        alignment = validate_contract("AlignmentReport", {"contract_version": "2.2", "report_id": _id(),
            "task_id": origin["task_id"], "generation": origin["generation"], "context_bundle_id": origin["context_bundle_id"],
            "dependency_fingerprint": context["dependency_fingerprint"],
            "source_artifact_refs": [draft_ref, draft["text_ref"], draft["scene_ref"], extraction_ref, proof_ref],
            "evidence_refs": [s["text_evidence"] for s in segments], "blocking_issues": issues,
            "unresolved_scopes": [c["check_id"] for c in result["checks"] if c["status"] == "UNRESOLVED"],
            "draft_ref": draft_ref, "scene_contract_ref": scene["scene_contract_ref"], "text_ref": draft["text_ref"],
            "scene_ref": draft["scene_ref"], "from_world_ref": draft["input_world_ref"], "to_world_ref": world_ref,
            "world_binding": binding, "narrative_revision_id": contract["narrative_revision_id"],
            "arrangement_version": contract["arrangement_version"], "reading_coverage_refs": [extraction["delivery_ref"]],
            "segments": segments, "critical_commitment_checks": result["checks"],
            "unmatched_critical_spans": [a["span"] for a in extraction["assertions"] if a["classification"] == "UNCERTAIN"],
            "open_change_refs": pending, "reconciliation_ref": report_ref, "status": status,
            "required_review_modes": ["fact", "epistemic", "continuity", "literary"], "world_effects_applied": 0}, "narrative")
        alignment_ref = self.kernel._blob(alignment); staged.append((alignment_ref, alignment, "ALIGNMENT_REPORT"))
        with self.kernel.db.uow(write=False) as repo:
            executed_at = repo.now().isoformat()
        execution = validate_contract("ReconciliationExecution", {"schema_version": "2.2", "reconciliation_ref": report_ref,
            "alignment_ref": alignment_ref, "proof_ref": proof_ref, "world_ref": world_ref, "origin_task_id": origin["task_id"],
            "origin_generation": origin["generation"], "origin_grant_id": origin["grant_id"], "origin_permission_epoch": origin_grant["epoch"],
            "extraction_task_id": ext_task["task_id"], "extraction_generation": ext_task["generation"],
            "extraction_grant_id": ext_task["grant_id"], "extraction_permission_epoch": ext_grant["epoch"], "executed_at": executed_at}, "narrative")
        execution_ref = self.kernel._blob(execution); staged.append((execution_ref, execution, "NARRATIVE_CHECK_EXECUTION"))
        receipt = {"reconciliation_ref": report_ref, "alignment_ref": alignment_ref, "status": status, "pending_delta_refs": pending}
        with self.kernel.objects.lock(), self.kernel.db.uow() as repo:
            repo.one("works", {"work_id": world_ref["work_id"]}, lock="update")
            self._controller(repo, principal, world_ref, "ARTIFACT_WRITE")
            old = self.kernel._prior(repo, principal, world_ref["work_id"], "NARRATIVE_RECONCILE", idempotency_key, request)
            if old:
                return old
            fresh = self._report_inputs(repo, principal, draft_ref, extraction_ref, world_ref, resolutions)
            require(fresh[7]["epoch"] == origin_grant["epoch"] and fresh[9]["epoch"] == ext_grant["epoch"],
                    "VIEW_DENIED", "Source permission changed during verification")
            require(self._world_binding(repo, fresh[3], scene, proof_ref, extraction) == binding, "NARRATIVE_ALIGNMENT_REQUIRED", "World interpretation changed during verification")
            for ref, payload, kind in staged:
                self.kernel._register_artifact(repo, ref, world_ref["work_id"], payload, kind, principal.principal_id)
            repo.add("selections", {"work_id": world_ref["work_id"], "selection_kind": "NARRATIVE_CHECK:" + report_ref["artifact_revision_id"],
                                    "artifact_ref": execution_ref, "epoch": 1})
            self.kernel._receipt(repo, principal, world_ref, "NARRATIVE_RECONCILE", idempotency_key, request, receipt)
        return receipt

    def build_alignment(self, principal, draft_ref, extraction_ref, target_world_ref, idempotency_key, *, resolution_event_refs=None):
        return self.reconcile(principal, draft_ref, extraction_ref, target_world_ref, idempotency_key, resolution_event_refs=resolution_event_refs)

    def _validate_for_acceptance(self, repo, draft_ref, reconciliation_ref, world_ref):
        """Internal transaction guard; caller must authorize its exact material.

        Used by release predecessor checks after their task/input fence, and by
        the public controller wrapper below. No model/network call or mutation.
        """
        wid = world_ref["work_id"]
        draft = self._draft(repo, draft_ref, wid)
        scene = self._scene(repo, draft["scene_ref"], wid)
        report = self._artifact(repo, reconciliation_ref, wid, "RECONCILIATION_REPORT")
        require(report["draft_ref"] == draft_ref and report["text_ref"] == draft["text_ref"] and report["scene_ref"] == draft["scene_ref"]
                and report["adopted_world_ref"] == world_ref, "NARRATIVE_ALIGNMENT_REQUIRED", "Reconciliation does not bind these exact text and world references")
        execution_selection = repo.one("selections", {"work_id": wid, "selection_kind": "NARRATIVE_CHECK:" + reconciliation_ref["artifact_revision_id"]})
        require(execution_selection is not None, "UNVERIFIED_REPORT", "Reconciliation has no trusted server execution")
        execution = self._artifact(repo, execution_selection["artifact_ref"], wid, "NARRATIVE_CHECK_EXECUTION")
        require(execution["reconciliation_ref"] == reconciliation_ref and execution["world_ref"] == world_ref,
                "UNVERIFIED_REPORT", "Reconciliation execution binding differs")
        alignment = self._artifact(repo, execution["alignment_ref"], wid, "ALIGNMENT_REPORT")
        require(alignment["reconciliation_ref"] == reconciliation_ref and alignment["world_binding"] == report["world_binding"]
                and alignment["draft_ref"] == draft_ref and alignment["scene_contract_ref"] == scene["scene_contract_ref"]
                and alignment["to_world_ref"] == world_ref, "NARRATIVE_ALIGNMENT_REQUIRED", "Alignment binding differs")
        require(report["status"] == alignment["status"] == "CLEAR" and not report["hard_blockers"] and not report["pending_delta_refs"]
                and not alignment["open_change_refs"] and not alignment["blocking_issues"] and not alignment["unmatched_critical_spans"],
                "NARRATIVE_BLOCKED", "Text has unresolved world, cognition, or disclosure changes")
        origin, grant, _, context = self._origin(repo, draft)
        extraction, extraction_task, extraction_grant = self._extraction(repo, report["extraction_ref"], draft_ref, draft)
        require(execution["origin_task_id"] == origin["task_id"] and execution["origin_generation"] == origin["generation"]
                and execution["origin_grant_id"] == origin["grant_id"] and execution["origin_permission_epoch"] == grant["epoch"]
                and execution["extraction_task_id"] == extraction_task["task_id"]
                and execution["extraction_generation"] == extraction_task["generation"]
                and execution["extraction_grant_id"] == extraction_task["grant_id"]
                and execution["extraction_permission_epoch"] == extraction_grant["epoch"],
                "VIEW_DENIED", "Source task authorization no longer matches verified execution")
        require(alignment["dependency_fingerprint"] == context["dependency_fingerprint"], "UNVERIFIED_REPORT", "Report source dependency differs")
        selected_scene = repo.one("selections", {"work_id": wid, "selection_kind": "SCENE:" + scene["scene_contract"]["scene_id"]})
        require(selected_scene and selected_scene["artifact_ref"] == draft["scene_ref"], "NARRATIVE_ALIGNMENT_REQUIRED", "Scene, style or arrangement selection changed")
        self._artifact(repo, scene["scene_contract_ref"], wid, "SCENE_CONTRACT")
        for style_ref in scene["scene_contract"]["style_refs"]:
            self._artifact(repo, style_ref, wid, "STYLE_GUIDE")
        if scene["batch_ref"] is not None:
            from novel_factory.application.release import validate_scene_batch, validate_writing_task_binding
            validate_scene_batch(repo, self.kernel, scene)
            validate_writing_task_binding(repo, self.kernel, origin, draft["scene_ref"])
        revision = self.kernel.revision(repo, world_ref)
        require(repo.ancestor(draft["input_world_ref"], world_ref), "BRANCH_CHANGED", "Text source is outside adopted history")
        proof = self._artifact(repo, execution["proof_ref"], wid, "NARRATIVE_VERIFICATION_PROOF")
        require(proof["world_manifest_sha256"] == revision["manifest_hash"] and proof["frontiers_sha256"] == fingerprint(revision["frontiers"])
                and self._world_binding(repo, revision, scene, execution["proof_ref"], extraction) == report["world_binding"],
                "NARRATIVE_ALIGNMENT_REQUIRED", "World record, effect, rule or coverage binding differs")
        self._validate_knowledge_evidence(repo, revision, extraction["assertions"])
        resolved, resolution_events = self._resolved(repo, revision, extraction["assertions"], report["resolutions"])
        text = self.kernel.objects.read(draft["text_ref"]["sha256"]).decode("utf-8")
        result = evaluate(revision, scene, draft, extraction["assertions"], text, resolved)
        links = {(e["occurrence_id"], e["event_version_id"]): e for e in [*result["event_refs"], *resolution_events]}
        require(not result["blockers"] and result["checks"] == report["critical_commitment_checks"] == proof["boundary_checks"]
                and fingerprint(result["checks"]) == report["boundary_checks_sha256"]
                and [links[key] for key in sorted(links)] == report["event_refs"], "UNVERIFIED_REPORT", "Saved report does not match fresh deterministic checks")
        head = self.kernel.head(repo, wid, world_ref["branch_id"])
        current = self.kernel.revision(repo, head); work = repo.one("works", {"work_id": wid})
        branch = repo.one("branches", {"branch_id": world_ref["branch_id"]})
        require(repo.ancestor(world_ref, head) and (branch["kind"] != "CANON" or work["canon_branch_id"] == world_ref["branch_id"])
                and current["interpretation_ref"] == revision["interpretation_ref"] == work["interpretation_ref"],
                "NARRATIVE_ALIGNMENT_REQUIRED", "Canonical history or interpretation changed")
        require(all(selected(current, ref) for ref in report["event_refs"]), "NARRATIVE_ALIGNMENT_REQUIRED", "Referenced world event is no longer current")
        # Unrelated HEAD progress is valid; actual text facts, cognition and
        # scene boundaries are synchronously checked against current history.
        current_result = evaluate(current, scene, draft, extraction["assertions"], text, resolved)
        require(not current_result["blockers"], "NARRATIVE_ALIGNMENT_REQUIRED", "Relevant accepted world changes require explicit realignment")
        return {"draft": draft, "scene": scene, "reconciliation": report, "alignment_ref": execution["alignment_ref"]}

    def validate_for_acceptance(self, repo, principal, draft_ref, reconciliation_ref, world_ref):
        self._controller(repo, principal, world_ref)
        return self._validate_for_acceptance(repo, draft_ref, reconciliation_ref, world_ref)

    def accept_narrative(self, principal, draft_ref, reconciliation_ref, world_ref, idempotency_key, *, alignment_ref=None):
        self._key(idempotency_key)
        request = {"draft_ref": draft_ref, "reconciliation_ref": reconciliation_ref, "world_ref": world_ref, "alignment_ref": alignment_ref}
        with self.kernel.db.uow(write=False) as repo:
            self._controller(repo, principal, world_ref, "ACCEPT")
            old = self.kernel._prior(repo, principal, world_ref["work_id"], "NARRATIVE_ACCEPT", idempotency_key, request)
            if old:
                return old
            checked = self.validate_for_acceptance(repo, principal, draft_ref, reconciliation_ref, world_ref)
        draft, scene = checked["draft"], checked["scene"]
        require(alignment_ref is None or alignment_ref == checked["alignment_ref"], "INVALID_EVIDENCE", "Requested alignment differs from executed report")
        if world_ref != draft["input_world_ref"]:
            require(alignment_ref == checked["alignment_ref"], "NARRATIVE_ALIGNMENT_REQUIRED", "Cross-world adoption requires its explicit alignment reference")
        contract = scene["scene_contract"]
        with self.kernel.db.uow(write=False) as repo:
            self._controller(repo, principal, world_ref, "ACCEPT")
            extraction = self._artifact(repo, checked["reconciliation"]["extraction_ref"], world_ref["work_id"], "ASSERTION_EXTRACTION")
            disclosure_ids = {a["disclosure_id"] for a in extraction["assertions"] if a["classification"] == "READER_DISCLOSURE_CHANGE"}
        payload = validate_contract("NarrativeAcceptance", {"schema_version": "2.2", "acceptance_id": _id(), "draft_ref": draft_ref,
            "text_ref": draft["text_ref"], "scene_ref": draft["scene_ref"], "world_ref": world_ref, "reconciliation_ref": reconciliation_ref,
            "alignment_ref": checked["alignment_ref"], "event_refs": checked["reconciliation"]["event_refs"],
            "task_id": draft["task_id"], "generation": draft["generation"], "plan_ref": scene["plan_ref"],
            "narrative_revision_id": contract["narrative_revision_id"], "arrangement_version": contract["arrangement_version"],
            "accepted_disclosures": [d for d in scene["allowed_disclosures"] if d["disclosure_id"] in disclosure_ids],
            "world_effects_applied": 0}, "narrative")
        ref = self.kernel._blob(payload)
        with self.kernel.objects.lock(), self.kernel.db.uow() as repo:
            repo.one("works", {"work_id": world_ref["work_id"]}, lock="update")
            self._controller(repo, principal, world_ref, "ACCEPT")
            repo.one("branches", {"branch_id": world_ref["branch_id"]}, lock="update")
            old = self.kernel._prior(repo, principal, world_ref["work_id"], "NARRATIVE_ACCEPT", idempotency_key, request)
            if old:
                return old
            self.validate_for_acceptance(repo, principal, draft_ref, reconciliation_ref, world_ref)
            key = "NARRATIVE_SCENE:" + contract["scene_id"]
            selection = repo.one("selections", {"work_id": world_ref["work_id"], "selection_kind": key})
            if selection:
                previous = self._artifact(repo, selection["artifact_ref"], world_ref["work_id"], "NARRATIVE_ACCEPTANCE")
                require(previous["draft_ref"] in [draft_ref, draft["prior_draft_ref"]], "NARRATIVE_SELECTION_CONFLICT", "A different draft is already accepted; explicit revision lineage is required")
                if previous["draft_ref"] == draft_ref and previous["reconciliation_ref"] == reconciliation_ref and previous["world_ref"] == world_ref:
                    self.kernel._receipt(repo, principal, world_ref, "NARRATIVE_ACCEPT", idempotency_key, request, selection["artifact_ref"])
                    return selection["artifact_ref"]
            self.kernel._register_artifact(repo, ref, world_ref["work_id"], payload, "NARRATIVE_ACCEPTANCE", principal.principal_id)
            if selection:
                repo.change("selections", {"work_id": world_ref["work_id"], "selection_kind": key}, {"artifact_ref": ref, "epoch": selection["epoch"] + 1})
            else:
                repo.add("selections", {"work_id": world_ref["work_id"], "selection_kind": key, "artifact_ref": ref, "epoch": 1})
            task = repo.one("tasks", {"task_id": draft["task_id"]})
            if task["state"] != "ACCEPTED":
                self.kernel.tasks.accept_in_transaction(repo.conn, draft["task_id"], draft["generation"])
            scope = "ARTIFACT_SELECTION:" + key
            self._invalidate(repo, world_ref, scope)
            repo.outbox("narrative.accepted", "artifact", ref["artifact_revision_id"], {"acceptance_ref": ref, "world_ref": world_ref, "world_effects_applied": 0}, "narrative-accepted:" + ref["artifact_revision_id"])
            self.kernel._receipt(repo, principal, world_ref, "NARRATIVE_ACCEPT", idempotency_key, request, ref)
        return ref

    def submit_draft(self, principal, payload):
        """Adapter preserving the original core 2.0 DraftSubmitRequest shape."""
        request = validate_contract("DraftSubmitRequest", payload)
        with self.kernel.db.uow(write=False) as repo:
            self._controller(repo, principal, request["world_ref"], "ACCEPT")
            alignment = self._artifact(repo, request["alignment_ref"], request["world_ref"]["work_id"], "ALIGNMENT_REPORT")
            require(alignment["draft_ref"] == request["draft_ref"] and alignment["scene_contract_ref"] == request["scene_contract_ref"]
                    and all(alignment[k] == request[k] for k in ("task_id", "generation", "context_bundle_id", "dependency_fingerprint")),
                    "INVALID_EVIDENCE", "DraftSubmitRequest does not match exact alignment provenance")
        return self.accept_narrative(principal, request["draft_ref"], alignment["reconciliation_ref"], request["world_ref"],
                                     request["idempotency_key"], alignment_ref=request["alignment_ref"])

    def read_agent_material(self, principal, task_id, generation, context_bundle_id, artifact_ref):
        try:
            return self._read_agent_material(principal, task_id, generation, context_bundle_id, artifact_ref)
        except DomainError as error:
            if error.code == "VIEW_DENIED":
                raise DomainError("VIEW_DENIED", "Literary material is unavailable for this assignment") from None
            raise

    def _read_agent_material(self, principal, task_id, generation, context_bundle_id, artifact_ref):
        """Purpose-specific material port. Unsupported kinds return None only.

        Gateway may fall back to Kernel.get_artifact for ordinary world
        evidence. Supported literary kinds always enforce their own exact task
        scope and must never fall back following an authorization failure.
        """
        with self.kernel.db.uow(write=False) as repo:
            task, grant, spec, _ = self._task(repo, principal, task_id, generation)
            require(grant["role_id"] in {"writer", "reviewer", "evidence_maintainer", "literary_editor", "chief_editor", "volume_director", "control_operator"},
                    "VIEW_DENIED", "Literary material is unavailable for this assignment")
            require(task["context_bundle_id"] == context_bundle_id, "VIEW_DENIED", "Material context differs")
            artifact = self.kernel.artifact(repo, artifact_ref, task["work_id"])
            kind = artifact["kind"]
            if kind == "LITERARY_MEMORY":
                from novel_factory.application.editorial import ReviewService
                return ReviewService(self.kernel).read_memory_for_task(principal, task_id, generation, context_bundle_id, artifact_ref)
            if kind not in {"SCENE_PLAN", "NARRATIVE_DRAFT", "TEXT", "PLAN_REVISION", "STYLE_GUIDE"}:
                return None
            if kind == "PLAN_REVISION":
                # No raw plan through this writing/reading material port.
                require(False, "VIEW_DENIED", "Raw plan is outside literary material scope")
            if kind in {"SCENE_PLAN", "STYLE_GUIDE"}:
                scenes = []
                for ref in spec["input_artifacts"]:
                    candidate = self.kernel.artifact(repo, ref, task["work_id"])
                    if candidate["kind"] == "SCENE_PLAN":
                        scenes.append((ref, candidate["payload"]))
                if kind == "SCENE_PLAN":
                    require(any(ref == artifact_ref for ref, _ in scenes), "VIEW_DENIED", "Scene is outside writer input")
                    scene_ref = artifact_ref
                else:
                    allowed = [(ref, scene) for ref, scene in scenes if artifact_ref in scene["scene_contract"]["style_refs"]]
                    require(len(allowed) == 1, "VIEW_DENIED", "Style is outside the selected writing card")
                    scene_ref = allowed[0][0]
                *_, scene = self._writer_binding(repo, principal, task_id, generation, scene_ref)
                styles = [{"source_ref": ref, "style": self._artifact(repo, ref, task["work_id"], "STYLE_GUIDE")}
                          for ref in scene["scene_contract"]["style_refs"]]
                if kind == "STYLE_GUIDE":
                    return canonical_bytes({"source_ref": artifact_ref, "style": next(s["style"] for s in styles if s["source_ref"] == artifact_ref)})
                card = {"source_ref": scene_ref, "scene_contract": scene["scene_contract"],
                        "required_event_refs": scene["required_event_refs"], "entry_checks": scene["entry_checks"],
                        "exit_checks": scene["exit_checks"], "allowed_disclosures": scene["allowed_disclosures"],
                        "literary_purpose": scene["literary_purpose"], "selected_styles": styles}
                return canonical_bytes(card)
            draft_ref = artifact_ref if kind == "NARRATIVE_DRAFT" else None
            if draft_ref is None:
                scope_row = repo.one("selections", {"work_id": task["work_id"], "selection_kind": "NARRATIVE_READ:" + task_id + ":" + str(generation)})
                require(scope_row is not None, "VIEW_DENIED", "Text has no reading assignment")
                scope = self._artifact(repo, scope_row["artifact_ref"], task["work_id"], "NARRATIVE_READ_SCOPE")
                matches = [ref for ref in scope["draft_refs"] if self._draft(repo, ref, task["work_id"])["text_ref"] == artifact_ref]
                require(len(matches) == 1, "VIEW_DENIED", "Text is not an unambiguous assigned draft")
                draft_ref = matches[0]
        return canonical_bytes(self.read_text(principal, task_id, generation, draft_ref))
