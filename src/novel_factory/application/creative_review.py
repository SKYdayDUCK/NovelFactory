"""Version-bound literary review and bounded revisions through NF's native services."""
from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid5

from novel_factory.application.authorization import Principal
from novel_factory.application.dynamic_runtime import refresh_task, verify_task
from novel_factory.application.editorial import ReviewService
from novel_factory.application.kernel import require
from novel_factory.application.narrative import NarrativeService
from novel_factory.application.review_independence import PROOF_KIND, ReviewIndependenceService, verify_editor_cases
from novel_factory.application.semantic_dependencies import check_task_dependencies
from novel_factory.contracts.registry import validate_contract
from novel_factory.domain.canonical import canonical_bytes, fingerprint
from novel_factory.domain.editorial import reading_coverage, verify_span


def _derived(ref, label):
    return str(uuid5(UUID(ref["artifact_revision_id"]), label))


def _artifact(kernel, repo, work, owner, kind, payload, *, revision=None):
    ref = kernel._blob(payload)
    if revision:
        ref["artifact_revision_id"] = revision
    kernel._register_artifact(repo, ref, work, payload, kind, owner.principal_id)
    return ref


def _scope(task, spec):
    return {"world_ref": spec["world_ref"], "task_id": task["task_id"], "generation": task["generation"]}


def _identity(principal, grant):
    return {"principal_id": principal.principal_id, "grant_id": principal.grant_id, "permission_epoch": grant["epoch"]}


def _task(kernel, repo, owner, task_id, generation, action, worker_id=None, *, live=True):
    task, grant, spec = ReviewService(kernel)._task(repo, owner, task_id, generation)
    kernel.authorize(repo, owner, task["work_id"], "ARTIFACT_WRITE", task["branch_id"])
    require(not live or (task["state"] == "RUNNING" and (worker_id is None or worker_id == task["lease_owner"])),
            "VIEW_DENIED", "The active task lease is required")
    check_task_dependencies(repo, task_id, generation)
    rows = repo.many("artifacts", {"work_id": task["work_id"], "kind": "AGENT_INPUT",
                     "artifact_revision_id": [r["artifact_revision_id"] for r in spec["input_artifacts"]]})
    matches = [r["payload"] for r in rows if r["payload"]["role_id"] == task["role_id"]
               and r["payload"]["task_kind"] == task["task_kind"]
               and r["payload"]["runtime_sha256"] == spec["runtime_contract_version"]]
    require(len(matches) == 1 and matches[0].get("creative_harness", {}).get("creative_harness", {}).get("enabled") is True,
            "VIEW_DENIED", "Creative Harness is disabled for this assignment")
    runtime = kernel.artifact(repo, matches[0]["runtime_ref"], task["work_id"], kind="AGENT_RUNTIME")["payload"]
    require(fingerprint(runtime) == spec["runtime_contract_version"] and runtime["role_id"] == grant["role_id"]
            and runtime["task_kind"] == task["task_kind"] and action in runtime.get("mcp_requested_tools", []),
            "VIEW_DENIED", "The frozen role and skill do not authorize this task action")
    roles = {"review_submit": {"reviewer", "literary_editor", "chief_editor"},
             "evidence_read": {"reviewer", "literary_editor", "chief_editor", "writer"}, "artifact_put": {"writer"}}
    require(grant["role_id"] in roles[action], "VIEW_DENIED", "Incompatible literary assignment")
    return task, grant, spec, matches[0], runtime


def _lock_work(repo, task_id):
    # Native narrative/editing mutations use work -> branch -> task lock order.
    row = repo.one("tasks", {"task_id": task_id})
    require(row is not None, "VIEW_DENIED", "Literary assignment unavailable")
    repo.one("works", {"work_id": row["work_id"]}, lock="update")
    return row


def _bound_draft(kernel, repo, owner, task, spec, draft_ref, *, allow_accepted=False):
    result = NarrativeService(kernel)._reader_binding(repo, owner, task["task_id"], task["generation"], draft_ref,
                                                         allow_accepted=allow_accepted)
    draft = result[-1]
    require(draft["input_world_ref"]["branch_id"] == spec["world_ref"]["branch_id"],
            "VIEW_DENIED", "Draft belongs to another branch")
    return draft


def _quota(repo, task, spec, quota, units, previous=None):
    validate_contract("Quota", quota, "harness")
    budget = repo.one("budgets", {"work_id": task["work_id"], "budget_id": quota["budget_id"]})
    job = repo.one("supervisor_jobs", {"task_id": task["task_id"]})
    require(budget and (not job or job["config"]["dispatch"]["budget_id"] == quota["budget_id"]),
            "VIEW_DENIED", "Review budget is unavailable")
    deadline = datetime.fromisoformat(quota["deadline"].replace("Z", "+00:00"))
    require(repo.now() < deadline <= task["deadline"],
            "DEADLINE_EXCEEDED", "Review deadline is outside the live assignment")
    total_units = units + (previous["local_input_bytes"] if previous else 0)
    require(quota["currency"] == spec["budget"]["currency"] == budget["currency"]
            and Decimal(quota["max_amount"]) <= min(Decimal(spec["budget"]["max_amount"]), budget["limit_amount"])
            and total_units <= quota["max_tokens"] <= spec["budget"]["token_budget"],
            "BUDGET_EXCEEDED", "Review exceeds the task's bounded local input or NF allowance")
    # A separately dispatched model call stays billed on the NF ledger, including UNKNOWN reservations.
    assignments = list(previous["assignments"]) if previous else []
    current = {"task_id": task["task_id"], "generation": task["generation"]}
    if current not in assignments:
        assignments.append(current)
    calls = [c for key in assignments for c in repo.many("call_attempts", key)]
    require(all(c["budget_id"] == budget["budget_id"] for c in calls) and len(calls) <= quota["max_attempts"],
            "BUDGET_EXCEEDED", "Review calls exceed the frozen attempt allowance")
    money = sum(c["reserved_amount"] if c["state"] in {"PREPARED", "SENT", "UNKNOWN"} else
                (c["actual_amount"] or Decimal(0)) for c in calls)
    tokens = sum(c["reserved_tokens"] if c["state"] in {"PREPARED", "SENT", "UNKNOWN"} else
                 (c["actual_tokens"] or 0) for c in calls)
    require(money <= Decimal(quota["max_amount"]) and tokens <= quota["max_tokens"],
            "BUDGET_EXCEEDED", "Original NF ledger usage exceeds the review allowance")
    return {"local_input_bytes": total_units, "assignments": assignments, "attempt_ids": [c["attempt_id"] for c in calls],
            "ledger_reserved_or_spent_amount": str(money), "ledger_reserved_or_spent_tokens": tokens,
            "model_attempts_added": 0, "local_model_cost": "0"}


def _part(repo, work, report_ref, label, kind, *, optional=False):
    row = repo.one("artifacts", {"work_id": work, "kind": kind, "artifact_revision_id": _derived(report_ref, label)})
    if optional and row is None:
        return None
    require(row and row["payload"]["report_ref"] == report_ref and fingerprint(row["payload"]) == row["sha256"],
            "INVALID_EVIDENCE", "Review history is unavailable")
    return row["payload"]


def _review_history(kernel, repo, work, ref):
    row = kernel.artifact(repo, ref, work, kind="CREATIVE_REVIEW_REPORT")
    require(fingerprint(row["payload"]) == ref["sha256"], "INVALID_EVIDENCE", "Review bytes differ")
    return row["payload"], _part(repo, work, ref, "details", "CREATIVE_REVIEW_DETAILS")


def _verified_checks(native, repo, spec, binding, target_ref, draft, dimension):
    """Only a server-executed assertion check can turn a reading into a hard conflict."""
    ref = binding["reconciliation_refs"][binding["ordered_draft_refs"].index(target_ref)]
    if ref not in spec["input_artifacts"]:
        return []
    narrative, wr = native.narrative, spec["world_ref"]
    work = wr["work_id"]
    report = narrative._artifact(repo, ref, work, "RECONCILIATION_REPORT")
    require((report["draft_ref"], report["text_ref"], report["adopted_world_ref"]) == (target_ref, draft["text_ref"], wr),
            "INVALID_EVIDENCE", "Reconciliation belongs to a different manuscript or world")
    selection = repo.one("selections", {"work_id": work, "selection_kind": "NARRATIVE_CHECK:" + ref["artifact_revision_id"]})
    require(selection, "UNVERIFIED_REPORT", "No native reconciliation execution for these checks")
    execution = narrative._artifact(repo, selection["artifact_ref"], work, "NARRATIVE_CHECK_EXECUTION")
    proof = narrative._artifact(repo, execution["proof_ref"], work, "NARRATIVE_VERIFICATION_PROOF")
    require(execution["world_ref"] == wr and execution["reconciliation_ref"] == ref and proof["draft_ref"] == target_ref
            and proof["extraction_ref"] == report["extraction_ref"] and proof["adopted_world_ref"] == wr
            and proof["boundary_checks"] == report["critical_commitment_checks"],
            "UNVERIFIED_REPORT", "Native verification proof does not bind this manuscript")
    extraction, extractor, grant = narrative._extraction(repo, report["extraction_ref"], target_ref, draft, allow_accepted=True)
    check_task_dependencies(repo, extractor["task_id"], extractor["generation"])
    require(execution["extraction_grant_id"] == grant["grant_id"] and execution["extraction_permission_epoch"] == grant["epoch"],
            "REVIEW_REVOKED", "Original extraction authority changed")
    checks = {c["check_id"]: c for c in proof["boundary_checks"] if c["status"] == "FAIL"}
    evidence = []
    for a in extraction["assertions"]:
        plane = "WORLD" if dimension == "FACT" else "BELIEF"
        suffix = "world" if plane == "WORLD" else "knowledge"
        subject = a["world_check"] if plane == "WORLD" else a["knowledge_use"]
        check = checks.get(a["assertion_id"] + ":" + suffix)
        if subject and check and (plane != "WORLD" or subject["plane"] == "WORLD"):
            evidence.append({"plane": plane, "text_ref": draft["text_ref"], "text_span": a["span"],
                             "evidence_refs": [{"kind": "ARTIFACT", "artifact_ref": ref}]})
    return evidence


def _native_issue(finding, issue_id, text_refs):
    hard = finding["judgment"] == "CONTRADICTION" and finding["dimension"] in {"FACT", "KNOWLEDGE"}
    category = ("HARD_CONSISTENCY" if hard else "CONTINUITY" if finding["dimension"] == "CONTINUITY"
                else "STYLE" if finding["dimension"] in {"STYLE", "VOICE"} else "LITERARY")
    return {"issue_id": issue_id, "category": category, "severity": "BLOCKER" if hard else
            "QUESTION" if finding["judgment"] == "UNCERTAIN" else "SUGGESTION", "subject_ref": finding["target_ref"],
            "description": finding["reason"], "evidence_refs": [e for e in finding["evidence_refs"] if e.get("artifact_ref") in text_refs],
            "suggested_change": finding["requested_change"] or "Retain for independent editorial judgment.",
            "disposition": "OPEN", "issue_kind": "HARD_CONFLICT" if hard else "LITERARY_CHOICE"}


class CreativeReviewService:
    def __init__(self, kernel):
        self.kernel = kernel

    def read(self, principal, task_id, generation, *, draft_ref, idempotency_key, worker_id=None):
        proof = refresh_task(self.kernel, principal, task_id, generation)
        with self.kernel.objects.lock(), self.kernel.db.uow() as repo:
            _lock_work(repo, task_id)
            task, _, spec, _, _ = _task(self.kernel, repo, principal, task_id, generation, "evidence_read", worker_id)
            verify_task(self.kernel, repo, principal, task, proof, required_tools=("evidence_read",),
                        required_capabilities=("ARTIFACT_WRITE",))
            _bound_draft(self.kernel, repo, principal, task, spec, draft_ref)
            service = ReviewService(self.kernel.in_transaction(repo))
            return service.read_text(principal, task_id, generation, draft_ref, idempotency_key=idempotency_key)

    def review(self, principal, task_id, generation, *, binding, target_ref, scope_draft_refs, coverage_refs, layer,
               observations, quota, rework_limit, independence_ref, idempotency_key, prior_report_ref=None, related_independence_refs=None, worker_id=None):
        from novel_factory.domain.creative_review import native_dimensions
        from .adaptive_runtime import resolve_for_input, review_with_guidance

        ReviewService._key(idempotency_key)
        validate_contract("EditorialBinding", binding, "editorial")
        related_refs = [] if related_independence_refs is None else related_independence_refs
        require(isinstance(related_refs, list) and len(related_refs) <= 127, "INVALID_ARGUMENT", "Invalid related independence proofs")
        for ref in [target_ref, independence_ref, *related_refs, *([prior_report_ref] if prior_report_ref else [])]:
            validate_contract("ArtifactRef", ref)
        require(type(rework_limit) is int and 0 <= rework_limit <= 32
                and isinstance(observations, list) and len(observations) <= 64
                and isinstance(scope_draft_refs, list) and 1 <= len(scope_draft_refs) <= 128
                and isinstance(coverage_refs, list) and 1 <= len(coverage_refs) <= 512
                and len(canonical_bytes(observations)) <= 200000,
                "INVALID_ARGUMENT", "Invalid review inputs or rework limit")
        ordered = binding["ordered_draft_refs"]
        require(scope_draft_refs == [r for r in ordered if r in scope_draft_refs] and target_ref in scope_draft_refs,
                "INVALID_ARGUMENT", "Reading scope must follow the manuscript manifest exactly")
        require(len(related_refs) == len(scope_draft_refs) - 1, "INDEPENDENT_REVIEW_REQUIRED",
                "Each manuscript in the review scope needs its own independence proof")
        if layer == "CHAPTER_SEAM":
            require(len(scope_draft_refs) == 2 and ordered.index(scope_draft_refs[1]) == ordered.index(scope_draft_refs[0]) + 1,
                    "INVALID_ARGUMENT", "A chapter seam requires adjacent manuscripts")
        if layer == "VOLUME":
            require(scope_draft_refs == ordered, "INVALID_ARGUMENT", "A volume review requires the complete manifest")
        request = {"task_id": task_id, "generation": generation, "binding": binding, "target_ref": target_ref,
                   "scope_draft_refs": scope_draft_refs, "coverage_refs": coverage_refs, "layer": layer,
                   "observations": observations, "quota": quota, "rework_limit": rework_limit,
                   "prior_report_ref": prior_report_ref, "independence_ref": independence_ref, "related_independence_refs": related_refs}
        with self.kernel.db.uow(write=False) as repo:
            task, _, spec, _, _ = _task(self.kernel, repo, principal, task_id, generation, "review_submit", worker_id)
            require(all(r in spec["input_artifacts"] for r in scope_draft_refs), "VIEW_DENIED", "Draft is outside the assigned scope")
            for ref in [independence_ref, *related_refs]:
                self.kernel.artifact(repo, ref, task["work_id"], kind=PROOF_KIND)
        live = self._refresh_many([independence_ref, *related_refs])
        with self.kernel.objects.lock(), self.kernel.db.uow() as repo:
            _lock_work(repo, task_id)
            task, grant, spec, agent_input, runtime = _task(self.kernel, repo, principal, task_id, generation,
                                                           "review_submit", worker_id)
            verify_task(self.kernel, repo, principal, task, live[independence_ref["artifact_revision_id"]].reviewer_runtime,
                        required_tools=("review_submit",), required_capabilities=("ARTIFACT_WRITE",))
            work, scope = task["work_id"], _scope(task, spec)
            native = ReviewService(self.kernel.in_transaction(repo))
            require(binding["world_ref"] == spec["world_ref"] and target_ref in scope_draft_refs
                    and all(r in binding["ordered_draft_refs"] for r in scope_draft_refs)
                    and all(r in spec["input_artifacts"] for r in binding["ordered_draft_refs"]),
                    "VIEW_DENIED", "Review exceeds the assigned literary scope")
            dimension = spec["task_kind"].upper() if spec["role_id"] == "reviewer" else "LITERARY"
            if spec["role_id"] != "reviewer" and layer == "CHAPTER_SEAM":
                dimension = "CONTINUITY"
            require(dimension in native_dimensions(layer), "VIEW_DENIED", "Review layer is outside this task mode")
            native._review_mode(grant, spec, dimension)
            draft = _bound_draft(self.kernel, repo, principal, task, spec, target_ref)
            producer_task, producer_grant, producer_spec, _ = native.narrative._origin(repo, draft)
            producer = Principal(producer_task["grant_id"], producer_task["principal_id"])
            require(producer.principal_id != principal.principal_id and producer.grant_id != principal.grant_id,
                    "VIEW_DENIED", "A producer cannot review their own manuscript")
            independent = self._independence(repo, target_ref, task, producer_task, independence_ref, live[independence_ref["artifact_revision_id"]])
            require(independent == {"producer": _identity(producer, producer_grant), "reviewer": _identity(principal, grant),
                                    "independence_receipt": independence_ref}, "REVIEW_REVOKED", "Review identities changed")
            round_number, previous_usage, prior_findings = 0, None, []
            origin = repo.one("artifacts", {"work_id": work, "kind": "CREATIVE_REVISION_ORIGIN",
                                            "artifact_revision_id": _derived(target_ref, "review-origin")})
            if prior_report_ref or origin:
                require(origin and prior_report_ref and origin["payload"]["previous_report_ref"] == prior_report_ref,
                        "INVALID_EVIDENCE", "Re-review requires the exact parent report and replacement manuscript")
                previous, extra = _review_history(self.kernel, repo, work, prior_report_ref)
                revision = _part(repo, work, prior_report_ref, "revision", "CREATIVE_REVIEW_REVISION")
                require(draft["prior_draft_ref"] == previous["target_ref"] and revision["draft_ref"] == target_ref
                        and previous["rework_limit"] == rework_limit and extra["layer"] == layer,
                        "INVALID_EVIDENCE", "Review round or revision lineage does not match")
                require(quota == extra["quota"], "BUDGET_EXCEEDED", "A review cycle retains its original quota and deadline")
                round_number = extra["round"] + 1
                previous_usage = revision["usage"]
                prior_findings = [{"finding_id": note["finding_id"], "judgment": finding["judgment"],
                                   "edited": note["finding_id"] in revision["finding_ids"]}
                                  for note, finding in zip(extra["findings"], previous["findings"], strict=True)]
            require(round_number <= rework_limit, "CAPACITY_LIMIT", "Review round limit reached")
            records = native._coverage(repo, principal, task, grant, coverage_refs)
            ordered_texts = []
            related_bindings = []
            proof_iter = iter(related_refs)
            units = len(canonical_bytes(observations))
            for draft_ref in scope_draft_refs:
                source = _bound_draft(self.kernel, repo, principal, task, spec, draft_ref)
                _current_manuscript(repo, work, task["branch_id"], draft_ref)
                if draft_ref != target_ref:
                    proof_ref = next(proof_iter)
                    source_task, _, _, _ = native.narrative._origin(repo, source)
                    proof = self._independence(repo, draft_ref, task, source_task, proof_ref, live[proof_ref["artifact_revision_id"]])
                    related_bindings.append({"draft_ref": draft_ref, "independence": proof})
                text = native._text(repo, source["text_ref"], work)
                units += len(text.encode("utf-8"))
                require(units <= min(spec["budget"]["token_budget"], 2_000_000),
                        "BUDGET_EXCEEDED", "Reading material exceeds the review input limit")
                require(reading_coverage(text, source["text_ref"], records)["complete"],
                        "READING_COVERAGE_INCOMPLETE", "Each manuscript needs its own full reading receipt")
                ordered_texts.append({"text_ref": source["text_ref"], "text": text})
            usage = _quota(repo, task, spec, quota, units, previous_usage)
            primary = ordered_texts[scope_draft_refs.index(target_ref)]
            verified = _verified_checks(native, repo, spec, binding, target_ref, draft, dimension) if layer == "FACT_KNOWLEDGE" else []
            old = self.kernel._prior(repo, principal, work, "CREATIVE_REVIEW", idempotency_key, request)
            if old:
                return old
            blocks = (resolve_for_input(self.kernel, repo, spec, agent_input, runtime)
                      if "adaptive_instruction_ref" in agent_input else [])
            evaluated = review_with_guidance(layer, primary["text_ref"], primary["text"], observations,
                                             related_texts=ordered_texts, verified_evidence=verified, blocks=blocks)
            findings, notes, issues = [], [], []
            for item in evaluated:
                finding = item["finding"]
                require(finding["target_ref"] == primary["text_ref"], "INVALID_EVIDENCE", "Wrong finding target")
                if layer == "FACT_KNOWLEDGE":
                    require(finding["dimension"] == ("FACT" if dimension == "FACT" else "KNOWLEDGE"),
                            "VIEW_DENIED", "Finding is outside this reviewer's fact or epistemic assignment")
                verify_span(primary["text"], finding["text_span"])
                issue_id = str(uuid5(UUID(target_ref["artifact_revision_id"]), fingerprint(
                    [layer, finding["text_span"], finding["dimension"], item.get("pattern"), finding["reason"]])))
                if any(previous["issue_id"] == issue_id for previous in issues):
                    continue
                findings.append(finding)
                notes.append({**{k: v for k, v in item.items() if k != "finding"}, "finding_id": issue_id,
                              "span": finding["text_span"]})
                issues.append(_native_issue(finding, issue_id, [item["text_ref"] for item in ordered_texts]))
            require(len(findings) <= 64, "CAPACITY_LIMIT", "Too many findings for one review")
            scale = "SEAM" if layer == "CHAPTER_SEAM" else "VOLUME" if layer == "VOLUME" else (
                "SCENE" if len(scope_draft_refs) == 1 else "CHAPTER")
            changes = any(f["requested_change"] and f["judgment"] not in {"SUPPORTED", "UNCERTAIN"} for f in findings)
            outcome = "CHANGES_REQUESTED" if changes else "UNDETERMINED" if findings else "PASS"
            native_report = native.submit_review(principal, {"schema_version": "2.1", "task_id": task_id,
                "generation": generation, "context_bundle_id": task["context_bundle_id"], "binding": binding,
                "scope_draft_refs": scope_draft_refs, "scale": scale, "dimension": dimension,
                "coverage_refs": coverage_refs, "issues": issues, "summary": f"{layer}: {len(findings)} anchored findings.",
                "counterinterpretations": list(dict.fromkeys(n["counterinterpretation"] for n in notes))[:32],
                "outcome": outcome, "idempotency_key": "h3:" + fingerprint(request)})
            rubric = {"schema_version": "1.0", "layer": layer, "dimension": dimension,
                      "implementation": "novel_factory.domain.creative_review.v1", "scope": scope}
            if "adaptive_instruction_ref" in agent_input:
                rubric["adaptive_instruction_ref"] = agent_input["adaptive_instruction_ref"]
            rubric_ref = _artifact(self.kernel, repo, work, principal, "CREATIVE_REVIEW_RUBRIC", rubric)
            report = {"schema_version": "1.0", "kind": "REVIEW_REPORT", "scope": scope, "target_ref": target_ref,
                      "review_binding": {"producer": _identity(producer, producer_grant), "reviewer": _identity(principal, grant),
                                         "independence_receipt": independence_ref},
                      "mode": "ENGINEERING", "rubric_ref": rubric_ref, "findings": findings,
                      "recommendation": "REVISE" if changes else "INSUFFICIENT_EVIDENCE" if findings else "ACCEPT_CANDIDATE",
                      "rework_limit": rework_limit, "evaluation": {"kind": "ENGINEERING", "check_receipts": [native_report],
                      "literary_conclusion": "NOT_EVALUATED"}, "status": "ADVISORY",
                      "independence_policy": "DISTINCT_PRINCIPALS_AND_NO_SUBSTANTIVE_PROXY"}
            validate_contract("ReviewReport", report, "harness")
            report_ref = _artifact(self.kernel, repo, work, principal, "CREATIVE_REVIEW_REPORT", report)
            evidence = {"scope": scope, "report_ref": report_ref, "native_review_ref": native_report,
                        "layer": layer, "round": round_number, "previous_report_ref": prior_report_ref, "findings": notes,
                        "text_ref": draft["text_ref"], "before_draft_ref": draft["prior_draft_ref"], "after_draft_ref": target_ref,
                        "coverage_refs": coverage_refs, "quota": quota, "usage": usage,
                        "reader_observations": observations, "related_bindings": related_bindings, "prior_findings": prior_findings,
                        "scope_draft_refs": scope_draft_refs, "verified_evidence": verified,
                        "upstream_instruction_versions": _upstream_versions(repo, work, draft, binding, notes),
                        "source_versions": {"writer": {"task_id": draft["task_id"], "generation": draft["generation"],
                         "runtime_sha256": producer_spec["runtime_contract_version"]}, "reviewer": {"runtime_ref": agent_input["runtime_ref"],
                         "runtime_sha256": agent_input["runtime_sha256"], "skill": runtime["skill_name"],
                         "skill_version": runtime["skill_version"]}, "scene_ref": draft["scene_ref"], "plan_ref": binding["plan_ref"],
                         "style_refs": binding["style_refs"], "literary_refs": binding["literary_refs"]}}
            if "adaptive_instruction_ref" in agent_input:
                evidence["source_versions"]["reviewer"]["adaptive_instruction_ref"] = agent_input["adaptive_instruction_ref"]
            details_ref = _artifact(self.kernel, repo, work, principal, "CREATIVE_REVIEW_DETAILS", evidence,
                                    revision=_derived(report_ref, "details"))
            result = {"report_ref": report_ref, "native_review_ref": native_report, "details_ref": details_ref,
                      "report": report, "findings": notes}
            _quota(repo, task, spec, quota, units, previous_usage)
            self.kernel._receipt(repo, principal, spec["world_ref"], "CREATIVE_REVIEW", idempotency_key, request, result)
            return result

    def _refresh_many(self, refs):
        return ReviewIndependenceService(self.kernel).refresh_many(refs, include_reviewer_runtime=True)

    def _refresh_report(self, principal, task_id, generation, report_ref, *, writing=False):
        with self.kernel.db.uow(write=False) as repo:
            task = repo.one("tasks", {"task_id": task_id})
            require(task and task["generation"] == generation, "VIEW_DENIED", "Review task unavailable")
            self.kernel.authorize(repo, principal, task["work_id"], "READ", task["branch_id"])
            report, details = _review_history(self.kernel, repo, task["work_id"], report_ref)
            require(report["scope"]["world_ref"]["branch_id"] == task["branch_id"], "VIEW_DENIED", "Review scope mismatch")
            if writing:
                owner, _ = self.kernel._task_owner(repo, principal, task_id)
                spec = repo.one("task_inputs", {"task_id": task_id, "generation": generation})["spec"]
                require(owner["role_id"] == "writer" and report_ref in spec["input_artifacts"],
                        "VIEW_DENIED", "Writer is not assigned this review")
            else:
                require(report["scope"]["task_id"] == task_id and report["scope"]["generation"] == generation,
                        "VIEW_DENIED", "Report belongs to another assignment")
                if principal.principal_id != report["review_binding"]["reviewer"]["principal_id"]:
                    ReviewService(self.kernel)._controller(repo, principal, report["scope"]["world_ref"])
            proof_ref = report["review_binding"]["independence_receipt"]
        return self._refresh_many([proof_ref, *(b["independence"]["independence_receipt"] for b in details["related_bindings"])])

    def _independence(self, repo, target_ref, task, producer_task, proof_ref, live_proof):
        return ReviewIndependenceService(self.kernel).verify(repo, proof_ref=proof_ref, manuscript_ref=target_ref,
            creator_task_id=producer_task["task_id"], creator_generation=producer_task["generation"],
            reviewer_task_id=task["task_id"], reviewer_generation=task["generation"], live_proof=live_proof)

    def _current_review(self, repo, report, details, live_proof):
        scope = report["scope"]
        owner_identity = report["review_binding"]["reviewer"]
        owner = Principal(owner_identity["grant_id"], owner_identity["principal_id"])
        task, grant, spec, _, _ = _task(self.kernel, repo, owner, scope["task_id"], scope["generation"],
                                         "review_submit", live=False)
        primary = report["review_binding"]["independence_receipt"]
        verify_task(self.kernel, repo, owner, task, live_proof[primary["artifact_revision_id"]].reviewer_runtime,
                    required_tools=("review_submit",), required_capabilities=("ARTIFACT_WRITE",))
        require(_scope(task, spec) == scope and grant["epoch"] == owner_identity["permission_epoch"],
                "REVIEW_REVOKED", "The literary review is outside its original grant")
        service = ReviewService(self.kernel)
        native = service._artifact(repo, details["native_review_ref"], task["work_id"], "REVIEW_REPORT")["payload"]
        service._reports(repo, [details["native_review_ref"]], native["submission"]["binding"])
        target = report["target_ref"]
        draft = _bound_draft(self.kernel, repo, owner, task, spec, target, allow_accepted=True)
        producer, _, _, _ = service.narrative._origin(repo, draft)
        if details["verified_evidence"]:
            submission = native["submission"]
            require(_verified_checks(service, repo, spec, submission["binding"], target, draft, submission["dimension"])
                    == details["verified_evidence"], "REVIEW_REVOKED", "Native evidence changed during the review cycle")
        primary_proof = report["review_binding"]["independence_receipt"]
        proof = self._independence(repo, target, task, producer, primary_proof, live_proof[primary_proof["artifact_revision_id"]])
        for item in details["related_bindings"]:
            source = _bound_draft(self.kernel, repo, owner, task, spec, item["draft_ref"], allow_accepted=True)
            source_task, _, _, _ = service.narrative._origin(repo, source)
            ref = item["independence"]["independence_receipt"]
            require(self._independence(repo, item["draft_ref"], task, source_task, ref,
                                      live_proof[ref["artifact_revision_id"]]) == item["independence"],
                    "REVIEW_REVOKED", "Related manuscript independence changed")
        require(proof == report["review_binding"] and draft["text_ref"] == details["text_ref"],
                "REVIEW_REVOKED", "The review or independence proof no longer matches")
        return native

    def decide(self, principal, task_id, generation, *, report_ref, action, reason, idempotency_key,
               native_decision=None, worker_id=None):
        ReviewService._key(idempotency_key)
        validate_contract("ArtifactRef", report_ref)
        require(action in {"RETURN", "RETAIN", "ACCEPT_REVISION", "DEFER", "WITHDRAW"}
                and isinstance(reason, str) and 0 < len(reason.strip()) <= 4000,
                "INVALID_ARGUMENT", "An editorial action and reason are required")
        request = {"task_id": task_id, "generation": generation, "report_ref": report_ref,
                   "action": action, "reason": reason, "native_decision": native_decision}
        live_proof = self._refresh_report(principal, task_id, generation, report_ref)
        with self.kernel.objects.lock(), self.kernel.db.uow() as repo:
            task = _lock_work(repo, task_id)
            work = task["work_id"]
            report, details = _review_history(self.kernel, repo, work, report_ref)
            wr = report["scope"]["world_ref"]
            require(report["scope"]["task_id"] == task_id and report["scope"]["generation"] == generation
                    and task["generation"] == generation and wr["branch_id"] == task["branch_id"],
                    "VIEW_DENIED", "Report belongs to another review assignment")
            reviewer = report["review_binding"]["reviewer"]["principal_id"]
            final = action in {"RETAIN", "ACCEPT_REVISION"}
            participants = [report["review_binding"], *(b["independence"] for b in details["related_bindings"])]
            readings = []
            for participant in participants:
                ref = participant["independence_receipt"]
                binding = self.kernel.artifact(repo, ref, work, kind=PROOF_KIND)["payload"]["binding"]
                readings.append((ref, binding))
            producer_ids = {p["principal_id"] for _, binding in readings for p in binding["producers"]}
            native = ReviewService(self.kernel.in_transaction(repo))
            if action == "WITHDRAW" and principal.principal_id == reviewer:
                _task(self.kernel, repo, principal, task_id, generation, "review_submit", worker_id)
            else:
                grant = native._controller(repo, principal, wr)
                require(principal.principal_id not in producer_ids | {reviewer},
                        "VIEW_DENIED", "The producer and reviewer cannot approve their own work")
            submission = self._current_review(repo, report, details, live_proof)["submission"]
            if final:
                editor_cases = verify_editor_cases(readings, principal, grant, live_proof, repo.now())
            old = self.kernel._prior(repo, principal, work, "CREATIVE_REVIEW_DECIDE", idempotency_key, request)
            if old:
                return old
            for draft_ref in details["scope_draft_refs"]:
                _current_manuscript(repo, work, wr["branch_id"], draft_ref)
            previous = _latest_decision(self.kernel, repo, work, report_ref)
            require(previous is None or previous["action"] == "DEFER" or
                    (previous["action"] == "RETURN" and action == "WITHDRAW"),
                    "EDIT_CONFLICT", "This review already has a final editorial action")
            if action == "RETURN":
                task_spec = repo.one("task_inputs", {"task_id": task_id, "generation": generation})["spec"]
                _quota(repo, task, task_spec, details["quota"], 0, details["usage"])
                require(details["round"] < report["rework_limit"] and report["recommendation"] == "REVISE",
                        "REWORK_LIMIT", "No further revision is authorized for this review")
            require((native_decision is not None) == final, "EDITORIAL_DECISION_REQUIRED",
                    "A final decision needs a native editorial session and exact decision input")
            native_ref = None
            if final:
                validate_contract("EditDecisionRequest", native_decision, "editorial")
                require(details["native_review_ref"] in native_decision["review_refs"]
                        and native_decision["after_binding"] == submission["binding"],
                        "EDITORIAL_BINDING_MISMATCH", "Native decision does not cover this exact report and manuscript")
                if action == "ACCEPT_REVISION":
                    require(details["round"] > 0 and report["recommendation"] == "ACCEPT_CANDIDATE"
                            and all(f["edited"] or f["judgment"] not in {"PREFERENCE", "CONTRADICTION"}
                                    for f in details["prior_findings"]),
                            "REVIEW_REQUIRED", "The changed manuscript requires a clean independent re-review")
                if action == "RETAIN":
                    decisions = {d["issue_id"]: d["action"] for d in native_decision["issue_decisions"]}
                    require(all(decisions.get(f["finding_id"]) == "RETAIN_WITH_REASON" for f in details["findings"]),
                            "EDITORIAL_DECISION_REQUIRED", "Each retained finding needs a reason in the native editor's decision")
                native_ref = native.record_decision(principal, native_decision)
            decision = {"scope": report["scope"], "report_ref": report_ref, "target_ref": report["target_ref"],
                        "action": action, "reason": reason, "editor_id": principal.principal_id,
                        "native_decision_ref": native_ref, "previous_ref": previous["decision_ref"] if previous else None,
                        "recorded_at": repo.now().isoformat()}
            if final:
                decision["editor_independence"] = editor_cases
            ref = _artifact(self.kernel, repo, work, principal, "CREATIVE_REVIEW_DECISION", decision)
            key = {"work_id": work, "selection_kind": "CREATIVE_REVIEW_ACTION:" + report_ref["artifact_revision_id"]}
            current = repo.one("selections", key)
            if current:
                repo.change("selections", key, {"epoch": current["epoch"] + 1, "artifact_ref": ref})
            else:
                repo.add("selections", {**key, "epoch": 1, "artifact_ref": ref})
            result = {"action": action, "decision_ref": ref, "native_decision_ref": native_ref}
            self.kernel._receipt(repo, principal, wr, "CREATIVE_REVIEW_DECIDE", idempotency_key, request, result)
            return result

    def revise(self, principal, task_id, generation, *, report_ref, scene_ref, replacements, idempotency_key, worker_id=None):
        ReviewService._key(idempotency_key)
        validate_contract("ArtifactRef", report_ref)
        validate_contract("ArtifactRef", scene_ref)
        require(isinstance(replacements, list) and 1 <= len(replacements) <= 64
                and all(isinstance(r, dict) and set(r) == {"finding_id", "replacement"}
                        and isinstance(r["finding_id"], str) and isinstance(r["replacement"], str) for r in replacements)
                and len(canonical_bytes(replacements)) <= 1_000_000,
                "INVALID_ARGUMENT", "A bounded list of exact finding replacements is required")
        request = {"task_id": task_id, "generation": generation, "scene_ref": scene_ref, "report_ref": report_ref,
                   "replacements": replacements}
        live_proof = self._refresh_report(principal, task_id, generation, report_ref, writing=True)
        writer_proof = refresh_task(self.kernel, principal, task_id, generation)
        with self.kernel.objects.lock(), self.kernel.db.uow() as repo:
            _lock_work(repo, task_id)
            from .creative_revision_brief import authorize_returned_revision, pending_revision
            task, spec, report, details, decision = authorize_returned_revision(
                self, repo, principal, task_id, generation, report_ref, worker_id, live_proof, writer_proof)
            work, wr, old_ref = task["work_id"], spec["world_ref"], report["target_ref"]
            old = self.kernel._prior(repo, principal, work, "CREATIVE_REVISE", idempotency_key, request)
            if old:
                return old
            draft, old_text, eligible = pending_revision(self, repo, principal, task, spec, report_ref, report, details)
            require(draft["scene_ref"] == scene_ref, "VIEW_DENIED", "The directed revision must use the assigned scene")
            edits = []
            ids = set()
            for replacement in replacements:
                key = replacement["finding_id"]
                require(key in eligible and key not in ids, "INVALID_EVIDENCE", "Duplicate or unreviewed text location")
                ids.add(key)
                span = eligible[key][1]["text_span"]
                edits.append((span["start"], span["end"], replacement["replacement"]))
            text, edge = old_text, len(old_text)
            for start, end, new in sorted(edits, reverse=True):
                require(end <= edge, "INVALID_EVIDENCE", "Directed manuscript changes overlap")
                text = text[:start] + new + text[end:]
                edge = start
            require(text != old_text, "EDITORIAL_NO_PROGRESS", "Retain the original when no change is needed")
            usage = _quota(repo, task, spec, details["quota"],
                          len(old_text.encode("utf-8")) + len(canonical_bytes(replacements)), details["usage"])
            new_ref = NarrativeService(self.kernel.in_transaction(repo)).store_draft(
                principal, task_id, generation, scene_ref, text, "h3:" + fingerprint(request), title=draft["title"],
                prior_draft_ref=old_ref, declared_important_deltas=draft["declared_important_deltas"],
                handoff_refs=draft["handoff_refs"])
            revision = {"scope": _scope(task, spec), "report_ref": report_ref, "previous_draft_ref": old_ref,
                        "draft_ref": new_ref, "decision_ref": decision["decision_ref"], "finding_ids": sorted(ids),
                        "round": details["round"] + 1, "usage": usage}
            evidence = _artifact(self.kernel, repo, work, principal, "CREATIVE_REVIEW_REVISION", revision,
                                 revision=_derived(report_ref, "revision"))
            _artifact(self.kernel, repo, work, principal, "CREATIVE_REVISION_ORIGIN",
                      {"scope": _scope(task, spec), "previous_report_ref": report_ref, "draft_ref": new_ref,
                       "revision_ref": evidence}, revision=_derived(new_ref, "review-origin"))
            result = {"draft_ref": new_ref, "revision_ref": evidence, "round": details["round"] + 1}
            self.kernel._receipt(repo, principal, wr, "CREATIVE_REVISE", idempotency_key, request, result)
            return result


def _latest_decision(kernel, repo, work, report_ref):
    key = {"work_id": work, "selection_kind": "CREATIVE_REVIEW_ACTION:" + report_ref["artifact_revision_id"]}
    row = repo.one("selections", key)
    if row:
        ref = row["artifact_ref"]
        decision = kernel.artifact(repo, ref, work, kind="CREATIVE_REVIEW_DECISION")["payload"]
        require(decision["report_ref"] == report_ref, "INVALID_EVIDENCE", "Mismatched editorial action")
        return {**decision, "decision_ref": ref}
    return None


def _upstream_versions(repo, work, draft, binding, findings):
    roles = sorted({role for finding in findings for role in finding["upstream_roles"]})
    result = {role: [] for role in roles}
    if "writer" in result:
        spec = repo.one("task_inputs", {"task_id": draft["task_id"], "generation": draft["generation"]})["spec"]
        result["writer"].append({"task_id": draft["task_id"], "generation": draft["generation"],
                                 "runtime_sha256": spec["runtime_contract_version"]})
    for ref in [draft["scene_ref"], binding["plan_ref"], *binding["style_refs"], *binding["literary_refs"]]:
        if ref is None:
            continue
        row = repo.one("artifacts", {"work_id": work, "artifact_revision_id": ref["artifact_revision_id"]})
        payload = row["payload"]
        origin = payload.get("candidate", payload)
        task_id, generation = origin.get("task_id"), origin.get("generation")
        if task_id is None or generation is None:
            continue
        task_input = repo.one("task_inputs", {"task_id": task_id, "generation": generation})
        task = repo.one("tasks", {"task_id": task_id, "work_id": work})
        if task and task_input and task["role_id"] in result and row["created_by"] == task["principal_id"]:
            result[task["role_id"]].append({"source_ref": ref, "task_id": task_id, "generation": generation,
                                            "runtime_sha256": task_input["spec"]["runtime_contract_version"]})
    return {role: {"status": "TRACED" if traces else "UNKNOWN", "traces": traces} for role, traces in result.items()}


def _current_manuscript(repo, work, branch, draft_ref):
    from sqlalchemy import text
    newer = repo.conn.scalar(text("""
        SELECT 1 FROM nf_artifacts WHERE work_id = :work AND kind = 'NARRATIVE_DRAFT'
          AND payload #>> '{prior_draft_ref,artifact_revision_id}' = :draft
          AND payload #>> '{input_world_ref,branch_id}' = :branch LIMIT 1
    """), {"work": work, "branch": branch, "draft": draft_ref["artifact_revision_id"]})
    require(not newer, "TASK_REBIND_REQUIRED", "The manuscript has a newer revision")
