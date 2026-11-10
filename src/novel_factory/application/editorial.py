"""Evidence-bound literary memory, multiscale review and volume editing.

These immutable artifacts confer no world-write authority. Reading receipts
prove delivery of exact original text, never comprehension or aesthetic merit.
The only mutable records are compare-and-swap editorial selection pointers.
"""
from __future__ import annotations

from uuid import uuid4

from novel_factory.application.kernel import require
from novel_factory.application.queries import WorldQueryService
from novel_factory.application.query_policy import check_profile
from novel_factory.contracts.registry import validate_contract
from novel_factory.domain.canonical import canonical_bytes, fingerprint
from novel_factory.domain.editorial import (
    check_review_matrix, evidence_was_delivered, issue_fingerprint,
    next_edit_phase, reading_coverage, validate_issue, verify_span,
)
from novel_factory.domain.temporal import point_key


def _id():
    return str(uuid4())


def _ref(row):
    return {key: row[key] for key in ("artifact_id", "artifact_revision_id", "sha256")}


def memory_dependency(candidate, source_draft_refs):
    """Exact, documented dependency calculation for a derived memory candidate."""
    return fingerprint({"world_ref": candidate["world_ref"], "source_draft_refs": source_draft_refs,
        "source_refs": candidate["source_refs"], "counterevidence_refs": candidate.get("counterevidence_refs", []),
        "method_revision_sha256": candidate["method_revision_sha256"]})


class ReviewService:
    def __init__(self, kernel, *, narrative=None):
        self.kernel = kernel
        self._narrative = narrative

    @property
    def narrative(self):
        if self._narrative is None:
            from novel_factory.application.narrative import NarrativeService
            self._narrative = NarrativeService(self.kernel)
        return self._narrative

    @staticmethod
    def _key(value):
        require(isinstance(value, str) and 0 < len(value) <= 200,
                "INVALID_ARGUMENT", "Invalid idempotency key")

    def _artifact(self, repo, ref, work_id, kind=None):
        row = self.kernel.artifact(repo, ref, work_id, kind=kind)
        self.kernel.objects.verify(ref["sha256"])
        if row["payload"] is not None:
            require(fingerprint(row["payload"]) == ref["sha256"], "OBJECT_CORRUPT", "Artifact payload integrity failed")
        return row

    def _text(self, repo, ref, work_id):
        self._artifact(repo, ref, work_id, "TEXT")
        return self.kernel.objects.read(ref["sha256"]).decode("utf-8", "strict")

    def _task(self, repo, principal, task_id, generation, *, context_bundle_id=None):
        task, grant, spec = WorldQueryService(self.kernel)._task(repo, principal, task_id, generation)
        from novel_factory.application.planning import validate_task_plan_binding
        validate_task_plan_binding(repo, self.kernel, task, spec)
        require(grant["role_id"] in {"reviewer", "literary_editor", "chief_editor", "evidence_maintainer", "writer", "control_operator"},
                "VIEW_DENIED", "This role cannot read or review literary originals")
        context = repo.one("contexts", {"context_bundle_id": task["context_bundle_id"]})
        require(context and context["task_id"] == task_id and context["generation"] == generation
                and context["grant_id"] == principal.grant_id and context["principal_id"] == principal.principal_id
                and context["read_cut"]["permission_epoch"] == grant["epoch"]
                and context["payload"]["world_ref"] == spec["world_ref"] == context["read_cut"]["world_ref"]
                and context["payload"]["policy_version"] == spec["policy_version"]
                and context["dependency_fingerprint"] == task["dependency_fingerprint"],
                "TASK_REBIND_REQUIRED", "A current compiled context is required")
        if context_bundle_id is not None:
            require(task["context_bundle_id"] == context_bundle_id, "STALE_GENERATION", "Review context does not belong to this task")
        return task, grant, spec

    def _controller(self, repo, principal, wr):
        grant = self.kernel.authorize(repo, principal, wr["work_id"], "APPROVE", wr["branch_id"])
        require(grant["role_id"] in {"control_operator", "chief_editor", "literary_editor"},
                "VIEW_DENIED", "Editorial decisions require an explicitly authorized editor")
        revision = self.kernel.revision(repo, wr)
        require(all(point_key(frontier) <= point_key(grant["max_story_time"]) for frontier in revision["frontiers"].values()),
                "VIEW_DENIED", "Editorial world cut exceeds the authorized story horizon")
        return grant

    def _draft(self, repo, work_id, ref):
        return self._artifact(repo, ref, work_id, "NARRATIVE_DRAFT")["payload"]

    def read_text(self, principal, task_id, generation, draft_ref, start=None, end=None,
                  *, read_mode="FULL_TEXT", idempotency_key):
        """Return actual original text and its server-signed delivery coverage.

        A retry returns the original range again. It cannot mint coverage for
        model-reported ranges, summaries, another reader, or another text hash.
        """
        self._key(idempotency_key)
        require(read_mode in {"FULL_TEXT", "EXCERPT"}, "INVALID_ARGUMENT", "Original delivery has no summary mode")
        request = {"task_id": task_id, "generation": generation, "draft_ref": draft_ref,
                   "start": start, "end": end, "read_mode": read_mode}
        with self.kernel.db.uow(write=False) as repo:
            task, _, _ = self._task(repo, principal, task_id, generation)
            self.kernel._prior(repo, principal, task["work_id"], "EDITORIAL_READ", idempotency_key, request)
        delivered = self.narrative.read_text(principal, task_id, generation, draft_ref, start, end)
        wr = delivered["world_ref"]
        with self.kernel.objects.lock(), self.kernel.db.uow() as repo:
            task, grant, spec = self._task(repo, principal, task_id, generation)
            repo.one("tasks", {"task_id": task_id}, lock="update")
            task, grant, spec = self._task(repo, principal, task_id, generation)
            require(draft_ref in spec["input_artifacts"] and spec["world_ref"] == wr,
                    "VIEW_DENIED", "Original is outside the immutable task manifest")
            draft = self._draft(repo, task["work_id"], draft_ref)
            require(draft["text_ref"] == delivered["text_ref"] and delivered["draft_ref"] == draft_ref,
                    "INVALID_READING_COVERAGE", "Delivery refers to a different draft")
            text = self._text(repo, draft["text_ref"], task["work_id"])
            verify_span(text, delivered["span"])
            span = delivered["span"]
            require(delivered["text"] == text[span["start"]:span["end"]],
                    "INVALID_READING_COVERAGE", "Only the actual returned original range earns coverage")
            delivery_record = self._artifact(repo, delivered["delivery_ref"], task["work_id"], "TEXT_DELIVERY")["payload"]
            require(delivery_record["task_id"] == task_id and delivery_record["generation"] == generation
                    and delivery_record["context_bundle_id"] == task["context_bundle_id"]
                    and all(delivery_record[key] == delivered[key] for key in ("draft_ref", "text_ref", "world_ref", "span")),
                    "INVALID_READING_COVERAGE", "Original delivery receipt belongs to a different task or range")
            old = self.kernel._prior(repo, principal, task["work_id"], "EDITORIAL_READ", idempotency_key, request)
            if old:
                return {**delivered, "coverage_ref": old["coverage_ref"]}
            provenance = {"schema_version": "2.1", "reader_task_id": task_id, "generation": generation,
                "principal_id": principal.principal_id, "grant_id": principal.grant_id, "grant_epoch": grant["epoch"],
                "world_ref": wr, "draft_ref": draft_ref, "text_ref": draft["text_ref"],
                "delivery_ref": delivered["delivery_ref"], "span": span, "read_mode": read_mode}
            validate_contract("ReadingProvenance", provenance, "editorial")
            provenance_ref = self.kernel._blob(provenance)
            coverage = {"schema_version": "2.0", "coverage_id": _id(), "reader_task_id": task_id,
                "generation": generation, "server_receipt_id": provenance_ref["artifact_revision_id"],
                "artifact_ref": draft["text_ref"], "source_length_codepoints": len(text),
                "read_mode": read_mode, "spans": [span], "recorded_at": repo.now().isoformat()}
            validate_contract("ReadingCoverage", coverage, "literary")
            coverage_ref = self.kernel._blob(coverage)
            self.kernel._register_artifact(repo, provenance_ref, wr["work_id"], provenance, "READING_PROVENANCE", principal.principal_id)
            self.kernel._register_artifact(repo, coverage_ref, wr["work_id"], coverage, "READING_COVERAGE", principal.principal_id)
            self.kernel._receipt(repo, principal, wr, "EDITORIAL_READ", idempotency_key, request, {"coverage_ref": coverage_ref})
        return {**delivered, "coverage_ref": coverage_ref}

    def _coverage(self, repo, principal, task, grant, refs):
        records = []
        spec = repo.one("task_inputs", {"task_id": task["task_id"], "generation": task["generation"]})["spec"]
        for ref in refs:
            row = self._artifact(repo, ref, task["work_id"], "READING_COVERAGE")
            record = validate_contract("ReadingCoverage", row["payload"], "literary")
            require(record["reader_task_id"] == task["task_id"] and record["generation"] == task["generation"]
                    and row["created_by"] == principal.principal_id,
                    "INVALID_READING_COVERAGE", "Coverage belongs to a different reader or generation")
            proof_row = repo.one("artifacts", {"artifact_revision_id": record["server_receipt_id"], "work_id": task["work_id"]})
            require(proof_row is not None, "INVALID_READING_COVERAGE", "Server delivery receipt is absent")
            proof = self._artifact(repo, _ref(proof_row), task["work_id"], "READING_PROVENANCE")["payload"]
            validate_contract("ReadingProvenance", proof, "editorial")
            require(proof["reader_task_id"] == task["task_id"] and proof["generation"] == task["generation"]
                    and proof["principal_id"] == principal.principal_id and proof["grant_id"] == principal.grant_id
                    and proof["grant_epoch"] == grant["epoch"] and proof["text_ref"] == record["artifact_ref"]
                    and proof["world_ref"] == spec["world_ref"] and proof["draft_ref"] in spec["input_artifacts"]
                    and proof["read_mode"] == record["read_mode"] and [proof["span"]] == record["spans"],
                    "INVALID_READING_COVERAGE", "Coverage does not match its original delivery")
            delivery = self._artifact(repo, proof["delivery_ref"], task["work_id"], "TEXT_DELIVERY")["payload"]
            require(delivery["task_id"] == task["task_id"] and delivery["generation"] == task["generation"]
                    and all(delivery[key] == proof[key] for key in ("draft_ref", "text_ref", "world_ref", "span")),
                    "INVALID_READING_COVERAGE", "Original delivery receipt differs from coverage provenance")
            records.append(record)
        return records

    def _evidence(self, repo, evidence, wr, *, allowed_refs, texts, records, grant=None):
        if evidence["kind"] == "EVENT":
            require(evidence["world_ref"] == wr, "INVALID_EVIDENCE", "Evidence belongs to another world cut")
            if grant and grant["role_id"] != "control_operator":
                check_profile(grant, "WORLD")
            revision = self.kernel.revision(repo, wr)
            event = evidence["event_ref"]
            require(revision["selection"].get(event["occurrence_id"]) == event["event_version_id"],
                    "INVALID_EVIDENCE", "Evidence event is not selected in this world cut")
            return
        ref = evidence["artifact_ref"]
        require(ref in allowed_refs, "VIEW_DENIED", "Evidence is outside the authorized source manifest")
        artifact = self._artifact(repo, ref, wr["work_id"])
        if ref["artifact_revision_id"] in texts:
            require("span" in evidence, "REVIEW_EVIDENCE_REQUIRED", "Original-text evidence needs an exact quoted range")
            verify_span(texts[ref["artifact_revision_id"]], evidence["span"])
            require(evidence_was_delivered(ref, evidence["span"], records),
                    "INVALID_READING_COVERAGE", "Quoted evidence was not delivered to this reader")
        else:
            rules = repo.one("works", {"work_id": wr["work_id"]})["rules_ref"]
            require(evidence["kind"] == "RULE" and artifact["kind"] == "RULESET" and ref == rules,
                    "VIEW_DENIED", "Artifact evidence needs authorized original text or the work's exact ruleset")
            if grant and grant["role_id"] != "control_operator":
                check_profile(grant, "WORLD")

    def _binding(self, repo, binding, *, require_acceptances=False):
        binding = validate_contract("EditorialBinding", binding, "editorial")
        wr, wid = binding["world_ref"], binding["world_ref"]["work_id"]
        self.kernel.revision(repo, wr)
        refs = binding["ordered_draft_refs"]
        require(len({r["artifact_revision_id"] for r in refs}) == len(refs)
                and len(binding["reconciliation_refs"]) == len(refs),
                "EDITORIAL_BINDING_MISMATCH", "Ordered drafts and reconciliation reports must match one to one")
        if binding["plan_ref"] is not None:
            self._artifact(repo, binding["plan_ref"], wid, "PLAN_REVISION")
        drafts, scene_plans = [], []
        for draft_ref, reconciliation_ref in zip(refs, binding["reconciliation_refs"]):
            draft = self._draft(repo, wid, draft_ref)
            scene = self._artifact(repo, draft["scene_ref"], wid, "SCENE_PLAN")["payload"]
            if scene["plan_ref"] != binding["plan_ref"]:
                old_plan = self._artifact(repo, scene["plan_ref"], wid, "PLAN_REVISION")["payload"]
                new_plan = self._artifact(repo, binding["plan_ref"], wid, "PLAN_REVISION")["payload"]
                old_nodes = {n["node_id"]: n for n in old_plan["nodes"]}
                new_nodes = {n["node_id"]: n for n in new_plan["nodes"]}
                require(old_plan["volume_id"] == new_plan["volume_id"]
                        and old_plan["volume_contract_ref"] == new_plan["volume_contract_ref"]
                        and repo.ancestor(old_plan["input_world_ref"], new_plan["input_world_ref"])
                        and all(old_nodes.get(node_id) == new_nodes.get(node_id) and node_id in old_nodes
                                for node_id in scene["plan_node_ids"]),
                        "EDITORIAL_BINDING_MISMATCH", "Adopted plan changed this scene's planning obligations")
            contract = scene["scene_contract"]
            require(contract["narrative_revision_id"] == binding["narrative_revision_id"]
                    and contract["arrangement_version"] == binding["arrangement_version"]
                    and contract["style_refs"] == binding["style_refs"],
                    "EDITORIAL_BINDING_MISMATCH", "Scene uses a different narrative, arrangement or style revision")
            report = self._artifact(repo, reconciliation_ref, wid, "RECONCILIATION_REPORT")["payload"]
            require(report["draft_ref"] == draft_ref and report["text_ref"] == draft["text_ref"]
                    and report["adopted_world_ref"] == wr, "EDITORIAL_BINDING_MISMATCH", "Reconciliation refers to a different text or world")
            self._text(repo, draft["text_ref"], wid)
            drafts.append(draft)
            scene_plans.append(scene["plan_ref"])
        accepts = binding["ordered_acceptance_refs"]
        require(not require_acceptances or len(accepts) == len(refs),
                "NARRATIVE_ACCEPTANCE_REQUIRED", "Final editorial approval needs every narrative acceptance")
        require(not accepts or len(accepts) == len(refs), "EDITORIAL_BINDING_MISMATCH", "Acceptance manifest is incomplete")
        for index, acceptance_ref in enumerate(accepts):
            accepted = self._artifact(repo, acceptance_ref, wid, "NARRATIVE_ACCEPTANCE")["payload"]
            require(accepted["draft_ref"] == refs[index] and accepted["reconciliation_ref"] == binding["reconciliation_refs"][index]
                    and accepted["world_ref"] == wr and accepted["plan_ref"] == scene_plans[index]
                    and accepted["narrative_revision_id"] == binding["narrative_revision_id"]
                    and accepted["arrangement_version"] == binding["arrangement_version"],
                    "EDITORIAL_BINDING_MISMATCH", "Narrative acceptance is not the ordered reviewed revision")
        for ref in binding["style_refs"]:
            style = self._artifact(repo, ref, wid, "STYLE_GUIDE")["payload"]
            require(repo.ancestor(style["world_ref"], wr), "EDITORIAL_BINDING_MISMATCH", "Style guide is outside the adopted world lineage")
        for ref in binding["literary_refs"]:
            memory = self._artifact(repo, ref, wid, "LITERARY_MEMORY")["payload"]
            require(memory["candidate"]["world_ref"] == wr
                    and memory["candidate"].get("narrative_revision_id") == binding["narrative_revision_id"]
                    and all(ref in refs for ref in memory["source_draft_refs"]),
                    "EDITORIAL_BINDING_MISMATCH", "Memory needs explicit adoption to these exact narrative sources")
        return drafts

    def _review_mode(self, grant, spec, dimension):
        role = grant["role_id"]
        if role == "control_operator" and "ADMIN" in grant["capabilities"]:
            return
        if role == "reviewer":
            allowed = {"fact": "FACT", "epistemic": "EPISTEMIC", "continuity": "CONTINUITY", "literary": "LITERARY"}
            require(allowed.get(spec["task_kind"]) == dimension, "VIEW_DENIED", "Review dimension is outside this task mode")
        else:
            require(role in {"literary_editor", "chief_editor"} and dimension in {"LITERARY", "STYLE", "CONTINUITY"},
                    "VIEW_DENIED", "This task cannot attest a fact or epistemic review")

    def submit_review(self, principal, request):
        request = validate_contract("ReviewSubmission", request, "editorial")
        wr, key = request["binding"]["world_ref"], request["idempotency_key"]
        with self.kernel.objects.lock(), self.kernel.db.uow() as repo:
            task, grant, spec = self._task(repo, principal, request["task_id"], request["generation"],
                                            context_bundle_id=request["context_bundle_id"])
            self.kernel.authorize(repo, principal, task["work_id"], "ARTIFACT_WRITE", task["branch_id"])
            repo.one("tasks", {"task_id": task["task_id"]}, lock="update")
            task, grant, spec = self._task(repo, principal, request["task_id"], request["generation"])
            self._review_mode(grant, spec, request["dimension"])
            if "agent_output_ref" in request:
                output = self._artifact(repo, request["agent_output_ref"], task["work_id"], "AGENT_CANDIDATE")["payload"]
                result = repo.one("task_results", {"result_id": task["current_result_id"]}) if task["current_result_id"] else None
                require(result and result["task_id"] == task["task_id"] and result["generation"] == task["generation"]
                        and request["agent_output_ref"] in result["payload"]["artifacts"]
                        and all(output.get(key) == request[key] for key in ("summary", "issues", "counterinterpretations")),
                        "INVALID_EVIDENCE", "Review differs from this task's exact stored model candidate")
            require(spec["world_ref"] == wr and all(r in spec["input_artifacts"] for r in request["binding"]["ordered_draft_refs"]),
                    "VIEW_DENIED", "Review manifest is outside this fixed task input")
            drafts = self._binding(repo, request["binding"])
            ordered, scope = request["binding"]["ordered_draft_refs"], request["scope_draft_refs"]
            require(all(ref in ordered for ref in scope) and len({r["artifact_revision_id"] for r in scope}) == len(scope),
                    "INVALID_ARGUMENT", "Review scope is not an ordered manifest subset")
            indices = [ordered.index(ref) for ref in scope]
            require(indices == sorted(indices), "INVALID_ARGUMENT", "Review scope must preserve narrative order")
            if request["scale"] == "SCENE":
                require(len(scope) == 1, "INVALID_ARGUMENT", "A scene review identifies one draft")
            if request["scale"] == "SEAM":
                require(len(scope) == 2, "INVALID_ARGUMENT", "A seam review identifies an ordered pair")
            if request["scale"] == "VOLUME":
                require(scope == ordered, "INVALID_ARGUMENT", "Volume review must read the complete ordered manifest")
            records = self._coverage(repo, principal, task, grant, request["coverage_refs"])
            texts, text_refs = {}, []
            for index in indices:
                ref = drafts[index]["text_ref"]
                text = self._text(repo, ref, wr["work_id"])
                require(reading_coverage(text, ref, records)["complete"], "READING_COVERAGE_INCOMPLETE", "Exact original text has unread gaps")
                texts[ref["artifact_revision_id"]] = text; text_refs.append(ref)
            require(len({i["issue_id"] for i in request["issues"]}) == len(request["issues"]), "INVALID_ARGUMENT", "Review issue IDs must be unique")
            for issue in request["issues"]:
                validate_issue(issue)
                require(issue["subject_ref"] in scope + text_refs, "INVALID_EVIDENCE", "Issue subject is outside the read scope")
                for evidence in issue["evidence_refs"]:
                    self._evidence(repo, evidence, wr, allowed_refs=spec["input_artifacts"] + text_refs,
                                   texts=texts, records=records, grant=grant)
            old = self.kernel._prior(repo, principal, wr["work_id"], "REVIEW_SUBMIT", key, request)
            if old:
                return old
            report = {"schema_version": "2.1", "report_id": _id(), "submission": request,
                "binding_sha256": fingerprint(request["binding"]), "principal_id": principal.principal_id,
                "grant_id": principal.grant_id, "grant_epoch": grant["epoch"], "policy_version": spec["policy_version"],
                "coverage_complete": True, "recorded_at": repo.now().isoformat()}
            validate_contract("ReviewReport", report, "editorial")
            ref = self.kernel._blob(report)
            self.kernel._register_artifact(repo, ref, wr["work_id"], report, "REVIEW_REPORT", principal.principal_id)
            self.kernel._receipt(repo, principal, wr, "REVIEW_SUBMIT", key, request, ref)
        return ref

    def save_memory(self, principal, request):
        request = validate_contract("MemorySubmission", request, "editorial")
        candidate, key = request["candidate"], request["idempotency_key"]
        wr = candidate["world_ref"]
        with self.kernel.objects.lock(), self.kernel.db.uow() as repo:
            task, grant, spec = self._task(repo, principal, candidate["task_id"], candidate["generation"])
            self.kernel.authorize(repo, principal, task["work_id"], "ARTIFACT_WRITE", task["branch_id"])
            repo.one("tasks", {"task_id": task["task_id"]}, lock="update")
            task, grant, spec = self._task(repo, principal, candidate["task_id"], candidate["generation"])
            require(grant["role_id"] in {"evidence_maintainer", "literary_editor", "chief_editor", "control_operator"},
                    "VIEW_DENIED", "This role cannot maintain literary memory")
            require(candidate["memory_kind"] == "LITERARY_MEMORY" and candidate["plane"] == "LITERARY"
                    and candidate["audience"] in {"WRITER", "EDITOR", "CONTROL"} and candidate["world_ref"] == spec["world_ref"],
                    "INVALID_ARGUMENT", "Literary memory cannot masquerade as world fact or actor belief")
            require(candidate["dependency_fingerprint"] == memory_dependency(candidate, request["source_draft_refs"]),
                    "MEMORY_DEPENDENCY_MISMATCH", "Memory dependency manifest does not match its sources")
            records = self._coverage(repo, principal, task, grant, request["coverage_refs"])
            texts, text_refs = {}, []
            for draft_ref in request["source_draft_refs"]:
                require(draft_ref in spec["input_artifacts"], "VIEW_DENIED", "Memory source is outside the task")
                draft = self._draft(repo, wr["work_id"], draft_ref)
                text_ref = draft["text_ref"]; text = self._text(repo, text_ref, wr["work_id"])
                if candidate["source_coverage"] == "COMPLETE":
                    require(reading_coverage(text, text_ref, records)["complete"], "READING_COVERAGE_INCOMPLETE", "Memory claims unread original coverage")
                texts[text_ref["artifact_revision_id"]] = text; text_refs.append(text_ref)
            for evidence in candidate["source_refs"] + candidate.get("counterevidence_refs", []):
                self._evidence(repo, evidence, wr, allowed_refs=spec["input_artifacts"] + text_refs,
                               texts=texts, records=records, grant=grant)
            summary = self._artifact(repo, candidate["summary_ref"], wr["work_id"], "TEXT")
            require(summary["created_by"] == principal.principal_id or candidate["summary_ref"] in spec["input_artifacts"],
                    "VIEW_DENIED", "Summary was not supplied to this task")
            summary_text = self._text(repo, candidate["summary_ref"], wr["work_id"])
            require(0 < len(summary_text.encode("utf-8")) <= 1_000_000,
                    "CAPACITY_LIMIT", "Literary interpretation must be nonempty and bounded")
            if candidate["actor_id"] is not None:
                require(candidate["actor_id"] in self.kernel.revision(repo, wr)["snapshot"]["entities"],
                        "INVALID_EVIDENCE", "Memory subject is not a known identity")
            old = self.kernel._prior(repo, principal, wr["work_id"], "LITERARY_MEMORY", key, request)
            if old:
                return old
            ref = self.kernel._blob(request)
            self.kernel._register_artifact(repo, ref, wr["work_id"], request, "LITERARY_MEMORY", principal.principal_id)
            self.kernel._receipt(repo, principal, wr, "LITERARY_MEMORY", key, request, ref)
        return ref

    def read_memory_for_task(self, principal, task_id, generation, context_bundle_id, memory_ref):
        """Deliver only task-assigned memory backed by authorized source text.

        The controller's existing NARRATIVE_READ_SCOPE is the source authority;
        knowing a memory handle or listing it in TaskSpec grants nothing. A
        writer receives literary interpretation, never an actor belief update.
        """
        def check(repo):
            task, grant, spec = self._task(repo, principal, task_id, generation, context_bundle_id=context_bundle_id)
            require(memory_ref in spec["input_artifacts"], "VIEW_DENIED", "Memory is outside the fixed task input")
            payload = self._artifact(repo, memory_ref, task["work_id"], "LITERARY_MEMORY")["payload"]
            validate_contract("MemorySubmission", payload, "editorial")
            candidate = payload["candidate"]
            require(candidate["memory_kind"] == "LITERARY_MEMORY" and candidate["plane"] == "LITERARY"
                    and repo.ancestor(candidate["world_ref"], spec["world_ref"])
                    and candidate["dependency_fingerprint"] == memory_dependency(candidate, payload["source_draft_refs"]),
                    "MEMORY_REVALIDATION_REQUIRED", "Memory source revision or dependency binding changed")
            audiences = {"writer": {"WRITER"}, "reviewer": {"WRITER", "EDITOR"},
                "literary_editor": {"WRITER", "EDITOR"}, "chief_editor": {"WRITER", "EDITOR"},
                "evidence_maintainer": {"WRITER", "EDITOR"}, "control_operator": {"WRITER", "EDITOR", "CONTROL"}}
            require(candidate["audience"] in audiences.get(grant["role_id"], set()),
                    "VIEW_DENIED", "Memory was not prepared for this role's audience")
            texts = {}
            for draft_ref in payload["source_draft_refs"]:
                _, _, _, draft = self.narrative._reader_binding(repo, principal, task_id, generation, draft_ref)
                texts[draft["text_ref"]["artifact_revision_id"]] = self._text(repo, draft["text_ref"], task["work_id"])
            require(bool(texts), "INVALID_EVIDENCE", "Memory needs a nonempty exact source manifest")
            target = self.kernel.revision(repo, spec["world_ref"])
            for evidence in candidate["source_refs"] + candidate.get("counterevidence_refs", []):
                if evidence["kind"] == "EVENT":
                    event = evidence["event_ref"]
                    require(target["selection"].get(event["occurrence_id"]) == event["event_version_id"],
                            "MEMORY_REVALIDATION_REQUIRED", "Source event changed after memory was written")
                elif evidence["artifact_ref"]["artifact_revision_id"] in texts:
                    verify_span(texts[evidence["artifact_ref"]["artifact_revision_id"]], evidence["span"])
                else:
                    require(evidence["artifact_ref"] in spec["input_artifacts"],
                            "VIEW_DENIED", "Memory contains evidence outside this task's material scope")
            self._artifact(repo, candidate["summary_ref"], task["work_id"], "TEXT")
            return candidate

        with self.kernel.db.uow(write=False) as repo:
            candidate = check(repo)
        summary = self.kernel.objects.read(candidate["summary_ref"]["sha256"]).decode("utf-8")
        delivered = {"schema_version": "2.1", "memory_ref": memory_ref, "source_world_ref": candidate["world_ref"],
            "entry_kind": candidate["entry_kind"], "subject_id": candidate["actor_id"], "interpretation": summary,
            "source_refs": candidate["source_refs"], "counterevidence_refs": candidate.get("counterevidence_refs", []),
            "unresolved_items": candidate["unresolved_items"], "source_coverage": candidate["source_coverage"],
            "authority": "DERIVED_LITERARY_INTERPRETATION_NOT_WORLD_FACT_OR_ACTOR_BELIEF"}
        validate_contract("LiteraryMemoryDelivery", delivered, "editorial")
        with self.kernel.db.uow(write=False) as repo:
            check(repo)
        return canonical_bytes(delivered)

    def save_style(self, principal, payload, idempotency_key):
        payload = validate_contract("StyleGuide", payload, "editorial")
        self._key(idempotency_key); wr = payload["world_ref"]
        with self.kernel.objects.lock(), self.kernel.db.uow() as repo:
            self._controller(repo, principal, wr)
            # Style adoption is an explicit control-plane decision, not a new
            # universal rule or a runtime skill upgrade applied retroactively.
            for voice in payload["voices"]:
                if voice["actor_id"] is not None:
                    require(voice["actor_id"] in self.kernel.revision(repo, wr)["snapshot"]["entities"],
                            "INVALID_EVIDENCE", "Voice anchor subject is unavailable")
                text_anchor = False
                for evidence in voice["anchors"]:
                    if evidence["kind"] == "EVENT":
                        self._evidence(repo, evidence, wr, allowed_refs=[], texts={}, records=[])
                    else:
                        row = self._artifact(repo, evidence["artifact_ref"], wr["work_id"])
                        if row["kind"] == "TEXT":
                            require("span" in evidence, "REVIEW_EVIDENCE_REQUIRED", "Voice text anchor needs an exact range")
                            verify_span(self._text(repo, evidence["artifact_ref"], wr["work_id"]), evidence["span"])
                            text_anchor = True
                require(text_anchor, "REVIEW_EVIDENCE_REQUIRED", "Each voice profile needs an exact original text anchor")
            for ref in payload["method_refs"]:
                self._artifact(repo, ref, wr["work_id"])
            repo.one("branches", {"branch_id": wr["branch_id"]}, lock="update")
            old = self.kernel._prior(repo, principal, wr["work_id"], "STYLE_SAVE", idempotency_key, payload)
            if old:
                return old
            ref = self.kernel._blob(payload)
            self.kernel._register_artifact(repo, ref, wr["work_id"], payload, "STYLE_GUIDE", principal.principal_id)
            self.kernel._receipt(repo, principal, wr, "STYLE_SAVE", idempotency_key, payload, ref)
        return ref

    def _reports(self, repo, refs, binding, *, current=True):
        reports = []
        for ref in refs:
            report = self._artifact(repo, ref, binding["world_ref"]["work_id"], "REVIEW_REPORT")["payload"]
            validate_contract("ReviewReport", report, "editorial")
            require(report["submission"]["binding"] == binding and report["binding_sha256"] == fingerprint(binding),
                    "EDITORIAL_BINDING_MISMATCH", "Review covers a different exact editorial revision")
            if current:
                grant = repo.one("grants", {"grant_id": report["grant_id"]}, lock="share")
                work = repo.one("works", {"work_id": binding["world_ref"]["work_id"]})
                task = repo.one("tasks", {"task_id": report["submission"]["task_id"]}, lock="share")
                require(grant and grant["active"] and grant["epoch"] == report["grant_epoch"]
                        and grant["principal_id"] == report["principal_id"] and report["policy_version"] == work["policy_version"],
                        "REVIEW_REVOKED", "Review authority is no longer current")
                require(task and task["generation"] == report["submission"]["generation"]
                        and task["state"] not in {"CANCELLED", "FAILED", "STALE"},
                        "REVIEW_REVOKED", "Review task was cancelled or superseded")
            reports.append(report)
        return reports

    def start_editing(self, principal, request):
        request = validate_contract("EditSessionRequest", request, "editorial")
        wr, key = request["binding"]["world_ref"], request["idempotency_key"]
        with self.kernel.objects.lock(), self.kernel.db.uow() as repo:
            self._controller(repo, principal, wr)
            self._binding(repo, request["binding"])
            repo.one("branches", {"branch_id": wr["branch_id"]}, lock="update")
            old = self.kernel._prior(repo, principal, wr["work_id"], "EDIT_START", key, request)
            if old:
                return old
            payload = {"schema_version": "2.1", "session_id": _id(), "request": request,
                       "principal_id": principal.principal_id, "recorded_at": repo.now().isoformat()}
            validate_contract("EditSession", payload, "editorial")
            ref = self.kernel._blob(payload)
            self.kernel._register_artifact(repo, ref, wr["work_id"], payload, "EDITORIAL_SESSION", principal.principal_id)
            repo.add("selections", {"work_id": wr["work_id"], "selection_kind": "EDIT_SESSION:" + payload["session_id"],
                                    "artifact_ref": ref, "epoch": 0})
            self.kernel._receipt(repo, principal, wr, "EDIT_START", key, request, ref)
        return ref

    def record_decision(self, principal, request):
        request = validate_contract("EditDecisionRequest", request, "editorial")
        wr, key = request["after_binding"]["world_ref"], request["idempotency_key"]
        with self.kernel.objects.lock(), self.kernel.db.uow() as repo:
            grant = self._controller(repo, principal, wr)
            session = self._artifact(repo, request["session_ref"], wr["work_id"], "EDITORIAL_SESSION")["payload"]
            self._binding(repo, request["after_binding"])
            selection_key = {"work_id": wr["work_id"], "selection_kind": "EDIT_SESSION:" + session["session_id"]}
            selected = repo.one("selections", selection_key, lock="update")
            old = self.kernel._prior(repo, principal, wr["work_id"], "EDIT_DECIDE", key, request)
            if old:
                return old
            prior_ref = request["expected_prior_decision_ref"]
            require(selected and selected["artifact_ref"] == (prior_ref or request["session_ref"]),
                    "EDIT_CONFLICT", "Another editorial decision already advanced this session")
            prior = self._artifact(repo, prior_ref, wr["work_id"], "EDITORIAL_DECISION")["payload"] if prior_ref else None
            before = prior["request"]["after_binding"] if prior else session["request"]["binding"]
            require(before["world_ref"] == wr, "EDITORIAL_BINDING_MISMATCH", "A different world cut needs a new editorial session")
            next_edit_phase(prior["request"]["phase"] if prior else None, request["phase"])
            round_number = (prior["round"] if prior else 0) + 1
            require(round_number <= session["request"]["max_rounds"], "EDITORIAL_BUDGET_EXHAUSTED", "Editorial round budget reached; retain or escalate")
            reports = self._reports(repo, request["review_refs"], request["after_binding"])
            ordered_refs = request["after_binding"]["ordered_draft_refs"]
            if request["phase"] in {"STRUCTURE", "LANGUAGE"}:
                require(any(r["submission"]["scale"] == "VOLUME"
                            and r["submission"]["dimension"] in {"LITERARY", "STYLE"}
                            and r["submission"]["scope_draft_refs"] == ordered_refs for r in reports),
                        "READING_COVERAGE_INCOMPLETE", "Whole-volume original reading must precede this editorial phase")
            if request["phase"] == "SEAM":
                require(all(any(r["submission"]["scale"] == "SEAM"
                                and r["submission"]["scope_draft_refs"] == [left, right] for r in reports)
                            for left, right in zip(ordered_refs, ordered_refs[1:])),
                        "REVIEW_COVERAGE_INCOMPLETE", "Each adjacent seam must be reviewed before language editing")
            issues = {issue["issue_id"]: issue for report in reports for issue in report["submission"]["issues"]}
            old_fingerprints = prior["resolved_issue_fingerprints"] if prior else []
            resolved = list(old_fingerprints)
            require(len({d["issue_id"] for d in request["issue_decisions"]}) == len(request["issue_decisions"]),
                    "INVALID_ARGUMENT", "Issue decisions must be unique")
            for decision in request["issue_decisions"]:
                issue = issues.get(decision["issue_id"])
                require(issue is not None, "INVALID_EVIDENCE", "Decision has no corresponding current review issue")
                if issue["issue_kind"] == "HARD_CONFLICT" or issue["category"] == "HARD_CONSISTENCY":
                    require(decision["action"] in {"REVISE", "RESOLVED"}, "HARD_CONFLICT_UNRESOLVED", "Editorial preference cannot waive a world conflict")
                for ref in decision["replacement_draft_refs"]:
                    require(ref in request["after_binding"]["ordered_draft_refs"], "INVALID_EVIDENCE", "Replacement is outside the reviewed manifest")
                if decision["action"] == "RESOLVED":
                    require(bool(decision["evidence_refs"]), "REVIEW_EVIDENCE_REQUIRED", "A resolved issue needs evidence of its resolution")
                mark = fingerprint({"issue": issue_fingerprint(issue), "decision_evidence": decision["evidence_refs"]})
                require(mark not in old_fingerprints or before != request["after_binding"],
                        "EDITORIAL_NO_PROGRESS", "The unchanged objection was already decided; no new text or evidence")
                if mark not in resolved:
                    resolved.append(mark)
            require(len(resolved) <= session["request"]["max_issue_decisions"], "EDITORIAL_BUDGET_EXHAUSTED", "Issue-decision budget reached")
            if prior and prior["request"]["phase"] == request["phase"]:
                require(before != request["after_binding"] or bool(request["issue_decisions"]),
                        "EDITORIAL_NO_PROGRESS", "Repeated phase has no new text or issue decision")
            if request["phase"] == "STRUCTURE":
                require(len(request["considered_options"]) >= 2
                        and sum(o["disposition"] == "SELECTED" for o in request["considered_options"]) == 1,
                        "EDITORIAL_OPTIONS_REQUIRED", "Structural editing records the chosen option and a considered alternative")
                require([f["draft_ref"] for f in request["scene_functions"]] == request["after_binding"]["ordered_draft_refs"],
                        "SCENE_FUNCTIONS_REQUIRED", "Structural editing needs the ordered scene-function and choice map")
                require(all(f["choice_evidence_refs"] for f in request["scene_functions"]),
                        "REVIEW_EVIDENCE_REQUIRED", "Scene functions need evidence for the character-choice chain")
                for function in request["scene_functions"]:
                    draft = self._draft(repo, wr["work_id"], function["draft_ref"])
                    require(any(e["kind"] == "ARTIFACT" and e["artifact_ref"] == draft["text_ref"] and "span" in e
                                for e in function["choice_evidence_refs"]),
                            "REVIEW_EVIDENCE_REQUIRED", "Each scene function must cite its own exact original text")
            # All evidence is an actual immutable object/event at this cut;
            # original spans are checked even though the deciding controller
            # may inspect evidence supplied by several separate reviewers.
            for evidence in [e for d in request["issue_decisions"] for e in d["evidence_refs"]] + [e for f in request["scene_functions"] for e in f["choice_evidence_refs"]]:
                if evidence["kind"] == "EVENT":
                    self._evidence(repo, evidence, wr, allowed_refs=[], texts={}, records=[])
                else:
                    row = self._artifact(repo, evidence["artifact_ref"], wr["work_id"])
                    if row["kind"] == "TEXT":
                        require("span" in evidence, "REVIEW_EVIDENCE_REQUIRED", "Editorial prose evidence needs an exact range")
                        verify_span(self._text(repo, evidence["artifact_ref"], wr["work_id"]), evidence["span"])
            payload = {"schema_version": "2.1", "decision_id": _id(), "request": request, "before_binding": before,
                "round": round_number, "principal_id": principal.principal_id, "grant_id": principal.grant_id,
                "grant_epoch": grant["epoch"], "resolved_issue_fingerprints": resolved, "recorded_at": repo.now().isoformat()}
            validate_contract("EditDecision", payload, "editorial")
            ref = self.kernel._blob(payload)
            self.kernel._register_artifact(repo, ref, wr["work_id"], payload, "EDITORIAL_DECISION", principal.principal_id)
            repo.change("selections", selection_key, {"artifact_ref": ref, "epoch": selected["epoch"] + 1})
            self.kernel._receipt(repo, principal, wr, "EDIT_DECIDE", key, request, ref)
        return ref

    def _final_reviews(self, repo, principal, binding, review_refs, decision_refs):
        drafts = self._binding(repo, binding, require_acceptances=True)
        reports = self._reports(repo, review_refs, binding)
        check_review_matrix(binding, reports)
        require(all(r["submission"]["outcome"] != "UNDETERMINED" for r in reports),
                "REVIEW_UNRESOLVED", "An undecided review cannot attest approval")
        decisions, previous = [], None
        for ref in decision_refs:
            item = self._artifact(repo, ref, binding["world_ref"]["work_id"], "EDITORIAL_DECISION")["payload"]
            require(item["request"]["expected_prior_decision_ref"] == previous,
                    "EDITORIAL_BINDING_MISMATCH", "Editing decisions are not one complete ordered chain")
            if decisions:
                require(item["request"]["session_ref"] == decisions[0]["request"]["session_ref"],
                        "EDITORIAL_BINDING_MISMATCH", "Editing chain crosses sessions")
            decisions.append(item); previous = ref
        require(decisions and {d["request"]["phase"] for d in decisions} == {"STRUCTURE", "SEAM", "LANGUAGE"}
                and decisions[-1]["request"]["after_binding"] == binding and decisions[-1]["request"]["phase"] == "LANGUAGE",
                "EDITORIAL_INCOMPLETE", "Structure, seam and language decisions must finish at this exact manifest")
        session = self._artifact(repo, decisions[0]["request"]["session_ref"], binding["world_ref"]["work_id"], "EDITORIAL_SESSION")["payload"]
        selection = repo.one("selections", {"work_id": binding["world_ref"]["work_id"], "selection_kind": "EDIT_SESSION:" + session["session_id"]}, lock="share")
        require(selection and selection["artifact_ref"] == decision_refs[-1], "EDITORIAL_BINDING_MISMATCH", "Editing session advanced beyond this approval")
        # Later text changes need a fresh structural and seam decision; a
        # language pass may not smuggle in a different unreviewed arrangement.
        for phase in ("STRUCTURE", "SEAM"):
            require(any(d["request"]["phase"] == phase and d["request"]["after_binding"] == binding for d in decisions),
                    "EDITORIAL_INCOMPLETE", "Final text changed after an earlier editing phase")
        actions = {a["issue_id"]: a for d in decisions if d["request"]["after_binding"] == binding for a in d["request"]["issue_decisions"]}
        for report in reports:
            require(report["principal_id"] != principal.principal_id, "APPROVAL_SEPARATION_REQUIRED", "Approver cannot approve their own reviews")
            for issue in report["submission"]["issues"]:
                action = actions.get(issue["issue_id"])
                require(action is not None, "REVIEW_UNRESOLVED", "Every final review issue needs an explicit editorial decision")
                hard = issue["issue_kind"] == "HARD_CONFLICT" or issue["category"] == "HARD_CONSISTENCY"
                if hard:
                    # A still-present hard report requires a new independent
                    # report after remediation. An editor cannot label it fixed.
                    require(False, "HARD_CONFLICT_UNRESOLVED", "Hard conflicts require a corrected draft and a fresh independent review")
                require(action["action"] in {"RETAIN_WITH_REASON", "RESOLVED"},
                        "REVIEW_UNRESOLVED", "Pending rewrite or deferred issue blocks final approval")
        for draft_ref, reconciliation_ref in zip(binding["ordered_draft_refs"], binding["reconciliation_refs"]):
            row = self._artifact(repo, draft_ref, binding["world_ref"]["work_id"], "NARRATIVE_DRAFT")
            require(row["created_by"] != principal.principal_id, "APPROVAL_SEPARATION_REQUIRED", "Writer cannot approve their own final text")
            self.narrative.validate_for_acceptance(repo, principal, draft_ref, reconciliation_ref, binding["world_ref"])
        return drafts, reports, decisions

    def approve_editorial(self, principal, request):
        request = validate_contract("EditorialApprovalRequest", request, "editorial")
        binding, key = request["binding"], request["idempotency_key"]
        wr = binding["world_ref"]
        with self.kernel.objects.lock(), self.kernel.db.uow() as repo:
            grant = self._controller(repo, principal, wr)
            repo.one("branches", {"branch_id": wr["branch_id"]}, lock="update")
            old = self.kernel._prior(repo, principal, wr["work_id"], "EDITORIAL_APPROVE", key, request)
            if old:
                return old
            self._final_reviews(repo, principal, binding, request["review_refs"], request["edit_decision_refs"])
            approval = {"approval_id": _id(), "subject_type": "RELEASE", "subject_sha256": fingerprint(binding),
                "principal_id": principal.principal_id, "policy_version": repo.one("works", {"work_id": wr["work_id"]})["policy_version"],
                "dependency_fingerprint": fingerprint({"binding": binding, "review_refs": request["review_refs"], "edit_decision_refs": request["edit_decision_refs"]}),
                "disposition": "ACCEPTED", "recorded_at": repo.now().isoformat(), "conditions": []}
            payload = {"schema_version": "2.1", "approval": approval, "binding": binding,
                "review_refs": request["review_refs"], "edit_decision_refs": request["edit_decision_refs"],
                "unresolved_choices": request["unresolved_choices"], "grant_id": principal.grant_id, "grant_epoch": grant["epoch"]}
            validate_contract("EditorialApproval", payload, "editorial")
            ref = self.kernel._blob(payload)
            self.kernel._register_artifact(repo, ref, wr["work_id"], payload, "EDITORIAL_APPROVAL", principal.principal_id)
            repo.add("selections", {"work_id": wr["work_id"], "selection_kind": "EDITORIAL_APPROVAL_ACTIVE:" + approval["approval_id"],
                                    "artifact_ref": ref, "epoch": 0})
            self.kernel._receipt(repo, principal, wr, "EDITORIAL_APPROVE", key, request, ref)
        return ref

    def validate_editorial_approval(self, repo, principal, approval_ref, world_ref, ordered_draft_refs,
                                    reconciliation_refs, narrative_revision_id, arrangement_version,
                                    style_refs, literary_refs, plan_ref=None, ordered_acceptance_refs=None,
                                    disclosure_revision_id=None):
        self.kernel.authorize(repo, principal, world_ref["work_id"], "READ", world_ref["branch_id"])
        payload = self._artifact(repo, approval_ref, world_ref["work_id"], "EDITORIAL_APPROVAL")["payload"]
        validate_contract("EditorialApproval", payload, "editorial")
        expected = {"world_ref": world_ref, "ordered_draft_refs": ordered_draft_refs, "reconciliation_refs": reconciliation_refs,
            "narrative_revision_id": narrative_revision_id, "arrangement_version": arrangement_version,
            "style_refs": style_refs, "literary_refs": literary_refs, "plan_ref": plan_ref,
            "ordered_acceptance_refs": ordered_acceptance_refs if ordered_acceptance_refs is not None else payload["binding"]["ordered_acceptance_refs"],
            "disclosure_revision_id": disclosure_revision_id if disclosure_revision_id is not None else payload["binding"]["disclosure_revision_id"]}
        require(payload["binding"] == expected, "EDITORIAL_BINDING_MISMATCH", "Approval binds a different exact release input")
        approval = payload["approval"]
        active = repo.one("selections", {"work_id": world_ref["work_id"], "selection_kind": "EDITORIAL_APPROVAL_ACTIVE:" + approval["approval_id"]}, lock="share")
        grant = repo.one("grants", {"grant_id": payload["grant_id"]}, lock="share")
        work = repo.one("works", {"work_id": world_ref["work_id"]})
        require(active and active["artifact_ref"] == approval_ref and grant and grant["active"]
                and grant["principal_id"] == approval["principal_id"] and grant["epoch"] == payload["grant_epoch"]
                and "APPROVE" in grant["capabilities"] and approval["policy_version"] == work["policy_version"]
                and approval["disposition"] == "ACCEPTED" and not approval["conditions"]
                and approval["subject_sha256"] == fingerprint(expected)
                and approval["dependency_fingerprint"] == fingerprint({"binding": expected,
                    "review_refs": payload["review_refs"], "edit_decision_refs": payload["edit_decision_refs"]}),
                "EDITORIAL_APPROVAL_REVOKED", "Editorial approval authority is no longer valid")
        # Recheck report grants, task generations, active edit chain and exact
        # hard reconciliation inside the release transaction. The approver is
        # reconstructed only from the already authenticated stored grant.
        from novel_factory.application.authorization import Principal
        approver = Principal(payload["grant_id"], approval["principal_id"])
        self._final_reviews(repo, approver, expected, payload["review_refs"], payload["edit_decision_refs"])
        return payload

    def revoke_editorial(self, principal, world_ref, approval_ref, reason, idempotency_key):
        self._key(idempotency_key)
        require(isinstance(reason, str) and 0 < len(reason) <= 8000, "INVALID_ARGUMENT", "Revocation needs a bounded reason")
        request = {"world_ref": world_ref, "approval_ref": approval_ref, "reason": reason}
        with self.kernel.objects.lock(), self.kernel.db.uow() as repo:
            self._controller(repo, principal, world_ref)
            stored = self._artifact(repo, approval_ref, world_ref["work_id"], "EDITORIAL_APPROVAL")["payload"]
            require(stored["binding"]["world_ref"] == world_ref, "EDITORIAL_BINDING_MISMATCH", "Revocation needs the exact approved world cut")
            approval = stored["approval"]
            selector = {"work_id": world_ref["work_id"], "selection_kind": "EDITORIAL_APPROVAL_ACTIVE:" + approval["approval_id"]}
            selected = repo.one("selections", selector, lock="update")
            old = self.kernel._prior(repo, principal, world_ref["work_id"], "EDITORIAL_REVOKE", idempotency_key, request)
            if old:
                return old
            require(selected and selected["artifact_ref"] == approval_ref, "EDITORIAL_APPROVAL_REVOKED", "Approval is already inactive")
            payload = {**request, "principal_id": principal.principal_id, "recorded_at": repo.now().isoformat()}
            ref = self.kernel._blob(payload)
            self.kernel._register_artifact(repo, ref, world_ref["work_id"], payload, "EDITORIAL_REVOCATION", principal.principal_id)
            repo.change("selections", selector, {"artifact_ref": ref, "epoch": selected["epoch"] + 1})
            self.kernel._receipt(repo, principal, world_ref, "EDITORIAL_REVOKE", idempotency_key, request, ref)
        return ref
