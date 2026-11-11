"""Bounded, replayable coordination of immutable cross-workflow conflict cases."""
from datetime import timedelta
import json
from time import monotonic
from uuid import UUID, uuid5

from sqlalchemy import text

from novel_factory.application.authorization import Principal
from novel_factory.application.kernel import require
from novel_factory.domain.canonical import fingerprint
from novel_factory.domain.errors import DomainError

MAX_REFRESHES = 2
MAX_WINDOW = 8
MAX_IMPACT = 32


def _id(case_id, name):
    return str(uuid5(UUID(str(case_id)), name))


def scan_event(repo, event_id):
    """Consume the durable hint in the same transaction as detection and its audit."""
    row = repo.conn.execute(text("""
        DELETE FROM nf_semantic_scans WHERE event_id=:event
        RETURNING work_id::text,branch_id::text,world_revision_id::text
    """), {"event": event_id}).mappings().one_or_none()
    if not row:
        return []
    from novel_factory.application.semantic_dependencies import detect_revision
    return detect_revision(repo, dict(row))


def _set_state(kernel, repo, task, state):
    if task["state"] == state:
        return
    repo.change("tasks", {"task_id": task["task_id"]},
                {"state": state, "lease_owner": None, "lease_expiry": None, "updated_at": repo.now()})
    kernel.tasks._emit(repo.conn, task["task_id"], task["generation"], state)


class ConflictResolutionService:
    def __init__(self, kernel):
        self.kernel = kernel

    def scan_one(self, controller, work_id, branch_id):
        with self.kernel.db.uow() as repo:
            self.kernel.authorize(repo, controller, work_id, "ADMIN", branch_id)
            event_id = repo.conn.scalar(text("""
                SELECT event_id FROM nf_semantic_scans WHERE work_id=:work AND branch_id=:branch
                ORDER BY event_id LIMIT 1 FOR UPDATE SKIP LOCKED
            """), {"work": work_id, "branch": branch_id})
            if event_id:
                return scan_event(repo, event_id)
            return None

    def resolve_one(self, controller, work_id, branch_id):
        return self._run(controller, work_id, branch_id, None)

    def resolve(self, controller, work_id, branch_id, case_id):
        return self._run(controller, work_id, branch_id, str(UUID(str(case_id))))

    def _run(self, controller, work_id, branch_id, case_id):
        # Discover one key first. Idle supervisors do not take world write locks.
        params = {"work": work_id, "branch": branch_id}
        if case_id:
            params["case"] = case_id
            condition = "AND c.case_id=:case"
        else:
            condition = "AND NOT EXISTS (SELECT FROM nf_conflict_resolutions r WHERE r.case_id=c.case_id)"
        with self.kernel.db.uow(write=False) as repo:
            self.kernel.authorize(repo, controller, work_id, "ADMIN", branch_id)
            found = repo.conn.scalar(text("""
                SELECT c.case_id::text FROM nf_conflict_cases c
                WHERE c.work_id=:work AND c.branch_id=:branch
            """ + condition + " ORDER BY c.created_at,c.case_id LIMIT 1"), params)
        if not found:
            require(case_id is None, "VIEW_DENIED", "Conflict case is unavailable")
            return None
        started = monotonic()
        with self.kernel.objects.lock(), self.kernel.db.uow() as repo:
            # Match Kernel's work -> branch -> task lock order, including services
            # nested below. All decisions here are finite SQL or local computations.
            repo.one("works", {"work_id": work_id}, lock="update")
            self.kernel.authorize(repo, controller, work_id, "ADMIN", branch_id)
            repo.one("branches", {"work_id": work_id, "branch_id": branch_id}, lock="update")
            case = repo.conn.execute(text("""
                SELECT case_id::text,task_id::text,generation,conflict_type,strategy,details
                FROM nf_conflict_cases WHERE case_id=:case AND work_id=:work AND branch_id=:branch
            """), {**params, "case": found}).mappings().one()
            prior = repo.conn.scalar(text("SELECT details FROM nf_conflict_resolutions WHERE case_id=:case"),
                                     {"case": found})
            if prior:
                return prior if case_id else None
            task = repo.one("tasks", {"task_id": case["task_id"], "work_id": work_id,
                                      "branch_id": branch_id}, lock="update")
            require(task, "INVALID_EVIDENCE", "Affected task is unavailable")
            kernel = self.kernel.in_transaction(repo)
            head = kernel.head(repo, work_id, branch_id)
            try:
                with repo.conn.begin_nested():
                    result = self._decide(kernel, repo, controller, head, case, task)
            except DomainError as exc:
                # Insufficient proofs, revoked grants or stale schedules end this
                # attempt. Database/connection faults roll the entire transaction back.
                result = self._review(kernel, repo, controller, head, case, task, exc.code)
            receipt_id = _id(found, "resolution:v1")
            result.update(case_id=found, receipt_id=receipt_id, task_id=case["task_id"],
                          generation=case["generation"], conflict_type=case["conflict_type"],
                          resolution_strategy=result.get("resolution_strategy", case["strategy"]),
                          additional_model_attempts=0, actual_tokens=0, cost_status="NO_MODEL_CALL")
            repo.conn.execute(text("""
                INSERT INTO nf_conflict_resolutions(case_id,receipt_id,task_id,generation,strategy,outcome,details)
                VALUES(:case_id,:receipt_id,:task_id,:generation,:resolution_strategy,:outcome,CAST(:json AS jsonb))
            """), {**result, "json": json.dumps(result)})
            from novel_factory.application.audit_log import LogService
            LogService.conflict(repo.conn, "resolved", case_id=found, receipt_id=receipt_id,
                                task_id=task["task_id"], generation=case["generation"],
                                outcome=result["outcome"], details=result,
                                duration_ms=round((monotonic() - started) * 1000, 3))
            repo.outbox("semantic.conflict.resolved", "task", task["task_id"],
                        {"work_id": work_id, "branch_id": branch_id, "case_id": found,
                         "generation": case["generation"], "receipt_id": receipt_id}, "semantic:resolved:" + found)
            return result

    def _decide(self, kernel, repo, controller, head, case, task):
        if task["generation"] != case["generation"] or task["state"] in {"CANCELLED", "FAILED"}:
            return {"outcome": "SUPERSEDED"}
        if task["state"] == "ACCEPTED":
            return {"outcome": "HISTORY_PRESERVED", "resolution_strategy": "REPAIR_HISTORY"}
        calls = repo.many("call_attempts", {"task_id": task["task_id"]})
        if any(call["state"] in {"PREPARED", "SENT", "UNKNOWN"} for call in calls):
            for call in calls:
                if call["state"] == "SENT":
                    kernel.tasks.settle_call(call["attempt_id"], state="UNKNOWN")
            _set_state(kernel, repo, task, "PAUSED")
            return {"outcome": "CALL_RECONCILIATION_REQUIRED", "call_attempt_ids":
                    [str(c["attempt_id"]) for c in calls if c["state"] in {"PREPARED", "SENT", "UNKNOWN"}]}
        spec = repo.one("task_inputs", {"task_id": task["task_id"], "generation": task["generation"]})["spec"]
        strategy = case["strategy"]
        if strategy in {"REPAIR_HISTORY", "CANDIDATE_BRANCH"}:
            # Preserve candidates; a fork alone never accepts, rewrites or promotes.
            kind = "REPAIR" if strategy == "REPAIR_HISTORY" else "CANDIDATE"
            ref = kernel.fork(controller, head, kind, "semantic:" + case["case_id"])
            result = self._review(kernel, repo, controller, head, case, task, "INDEPENDENT_DECISION_REQUIRED")
            result.update(outcome="CANDIDATE_PRESERVED", candidate_world_ref=ref)
            return result
        if strategy == "REPLAN_WINDOW":
            return self._plan(kernel, repo, controller, head, case, task, spec)
        if strategy == "JOINT_ADJUDICATION":
            result = self._review(kernel, repo, controller, head, case, task, "JOINT_DECISION_REQUIRED")
            result["outcome"] = "JOINT_DECISION_REQUIRED"
            # Bind an enrolled interaction if one exists, never invent participants
            # or a winner from Redis delivery order.
            job = repo.one("supervisor_jobs", {"task_id": task["task_id"]})
            if job:
                cfg = job["config"]
                interaction_id = cfg.get("joint", {}).get("interaction_id") or cfg.get("seal_interaction_id")
                if interaction_id and repo.one("simulation_interactions", {"interaction_id": interaction_id,
                                              "work_id": head["work_id"], "branch_id": head["branch_id"]}):
                    result["interaction_id"] = interaction_id
            return result
        if strategy == "ESCALATE_REVIEW":
            return self._review(kernel, repo, controller, head, case, task, "UNCERTAIN_MEANING")
        from novel_factory.application.semantic_dependencies import check_task_dependencies
        try:
            check_task_dependencies(repo, task["task_id"], task["generation"])
        except DomainError as exc:
            if exc.code != "DEPENDENCY_CONFLICT":
                raise
        else:
            if strategy == "REBASE_CANDIDATE":
                return self._certificate(kernel, repo, head, case, task, spec)
            return {"outcome": "REVALIDATED", "validated_world_ref": head}
        if strategy in {"RECOMPUTE_DEPENDENTS", "REVALIDATE", "REBASE_CANDIDATE"} and not calls:
            return self._refresh(kernel, repo, head, case, task, spec)
        return self._review(kernel, repo, controller, head, case, task, "NEW_COMPUTATION_REQUIRES_REVIEW")

    @staticmethod
    def _certificate(kernel, repo, head, case, task, spec):
        proposals = repo.many("proposals", {"task_id": task["task_id"], "generation": task["generation"]}, limit=2)
        require(len(proposals) == 1, "REVIEW_REQUIRED", "A single certified candidate is required")
        proposal = proposals[0]
        context = repo.one("contexts", {"context_bundle_id": task["context_bundle_id"]})
        approvals = repo.many("approvals", {"proposal_id": proposal["proposal_id"], "active": True})
        owner = Principal(task["grant_id"], task["principal_id"])
        ref = kernel.prepare(owner, {"schema_version": "2.1", "task_id": task["task_id"],
            "generation": task["generation"], "context_bundle_id": task["context_bundle_id"],
            "concurrency_mode": "DEPENDENCY_GUARDED", "base_world_ref": spec["world_ref"],
            "proposal_ref": proposal["artifact_ref"], "dependency_handle": context["payload"]["dependency_handle"],
            "approval_refs": [a["approval_id"] for a in approvals], "idempotency_key": "semantic:" + case["case_id"]})
        return {"outcome": "CERTIFICATE_REISSUED", "certificate_ref": ref, "validated_world_ref": head}

    @staticmethod
    def _refresh(kernel, repo, head, case, task, spec):
        # Keep the task identity (and waiting successors) but never rewrite a frozen
        # input. Only refetchable context is eligible for unattended recomputation.
        tid, gen = task["task_id"], task["generation"]
        require(task["state"] in {"READY", "RUNNING", "RESULT_STORED", "VALIDATING"}
                and task["deadline"] > repo.now(), "REVIEW_REQUIRED", "Task cannot refresh automatically")
        owner = Principal(task["grant_id"], task["principal_id"])
        grant = kernel.authorize(repo, owner, head["work_id"], "COMPILE", head["branch_id"])
        ctx = repo.one("contexts", {"task_id": tid, "generation": gen})
        require(ctx and grant["epoch"] == ctx["read_cut"]["permission_epoch"] and
                spec["policy_version"] == repo.one("works", {"work_id": head["work_id"]})["policy_version"]
                and spec["world_ref"] != head, "REVIEW_REQUIRED", "No authorized progress to a newer input")
        require(ctx["read_cut"]["knowledge_cut"]["plane"] == "WORLD", "REVIEW_REQUIRED",
                "Specialized knowledge cuts require a new authorized binding")
        origins = set(repo.conn.scalars(text("""SELECT DISTINCT details->>'origin' FROM nf_semantic_reads
            WHERE task_id=:task AND generation=:gen"""), {"task": tid, "gen": gen}))
        require(origins <= {"CONTEXT", "ARTIFACT"} and "CONTEXT" in origins,
                "REVIEW_REQUIRED", "Task has additional semantic or tool dependencies")
        require(not repo.one("proposals", {"task_id": tid, "generation": gen})
                and not repo.conn.scalar(text("SELECT EXISTS(SELECT FROM nf_workflow_waits WHERE task_id=:task)"),
                                         {"task": tid}), "REVIEW_REQUIRED", "Rebind sealed proposals or workflow inputs")
        input_kinds = [kernel.artifact(repo, ref, head["work_id"])["kind"] for ref in spec["input_artifacts"]]
        require(all(k in {"INPUT_MANIFEST", "AGENT_INPUT"} for k in input_kinds),
                "REVIEW_REQUIRED", "Specialized input artifacts require independent review")
        job = repo.one("supervisor_jobs", {"task_id": tid})
        if job:
            cfg = job["config"]
            require((task["role_id"], task["task_kind"]) == ("evidence_maintainer", "summary")
                    and cfg.get("accept_summary") and not cfg.get("editorial") and not cfg.get("joint")
                    and grant["epoch"] == cfg["grant_epoch"], "REVIEW_REQUIRED", "Re-enroll specialized dispatch")
            agent = kernel.artifact(repo, cfg["dispatch"]["input_ref"], head["work_id"], kind="AGENT_INPUT")
            require(not agent["payload"]["material_refs"], "REVIEW_REQUIRED", "Pinned material needs revalidation")
        require(not repo.conn.scalar(text("""SELECT count(*) >= :limit FROM nf_conflict_resolutions
            WHERE task_id=:task AND outcome='RECOMPUTE_QUEUED'"""), {"task": tid, "limit": MAX_REFRESHES}),
            "REPAIR_BUDGET_EXHAUSTED", "Task exhausted its automatic refresh budget")
        fresh = {**spec, "world_ref": head, "context_bundle_id": None, "dependency_fingerprint": None}
        kernel.tasks._save_input(repo.conn, tid, gen + 1, fresh, "READY")
        repo.change("tasks", {"task_id": tid, "generation": gen}, {"state": "READY", "generation": gen + 1,
                    "context_bundle_id": None, "dependency_fingerprint": None, "lease_owner": None,
                    "lease_expiry": None, "attempt_id": None, "current_result_id": None, "updated_at": repo.now()})
        kernel.tasks._emit(repo.conn, tid, gen + 1, "READY")
        return {"outcome": "RECOMPUTE_QUEUED", "resolution_strategy": "RECOMPUTE_DEPENDENTS",
                "new_generation": gen + 1, "input_world_ref": head, "prior_world_ref": spec["world_ref"]}

    def _plan(self, kernel, repo, controller, head, case, task, spec):
        from novel_factory.application.planning import PlanningService
        from novel_factory.application.rolling_plans import _anchor_name, _selection
        from novel_factory.domain.planning import affected_nodes
        from novel_factory.domain.temporal import point_key
        binding = _selection(repo, head["work_id"], "PLAN_TASK:" + task["task_id"] + ":" + str(task["generation"]))
        require(binding, "REVIEW_REQUIRED", "No frozen plan binding")
        pinned = kernel.artifact(repo, binding, head["work_id"], kind="PLAN_TASK_BINDING")["payload"]
        prior = kernel.artifact(repo, pinned["plan_ref"], head["work_id"], kind="PLAN_REVISION")["payload"]
        plans = PlanningService(kernel)
        require(_selection(repo, head["work_id"], plans.selection_key(prior["volume_id"])) == pinned["plan_ref"],
                "PLAN_SELECTION_CONFLICT", "Re-evaluate current selected plan")
        revision = kernel.revision(repo, head)
        impact = affected_nodes(prior, revision["snapshot"], revision["selection"])
        affected = set(impact["affected"])
        require(affected.intersection(pinned["node_ids"]) and len(affected) <= MAX_WINDOW,
                "WINDOW_CONFLICT", "Impact exceeds or misses this finite window")
        cuts = [spec["story_time"]]
        cuts.extend(e["at"] for e in case["details"].get("story_time_constraints", [])
                    if isinstance(e, dict) and isinstance(e.get("at"), dict))
        boundary = max(map(point_key, cuts))
        require(all(node["target_time"]["kind"] != "UNSPECIFIED" and
                    point_key(node["target_time"].get("at") or node["target_time"]["earliest"]) > boundary
                    for node in prior["nodes"] if node["node_id"] in affected), "WINDOW_CONFLICT",
                "Replan can only affect dated future slots")
        anchor = _selection(repo, head["work_id"], _anchor_name(head, prior["volume_id"]))
        if anchor:
            from novel_factory.application.rolling_plans import _future_window
            policy = kernel.artifact(repo, anchor, head["work_id"], kind="REFERENCE_TIMELINE")["payload"]
            window = _future_window(prior, {"tick": boundary[0], "phase": boundary[1]}, policy["scene_limit"])
            require(affected <= set(window), "WINDOW_CONFLICT", "Existing rolling policy bounds this change")
        if len(case["details"].get("affected_task_ids", [task["task_id"]])) > MAX_IMPACT:
            return self._review(kernel, repo, controller, head, case, task, "IMPACT_LIMIT")
        candidate = plans.replan(controller, {"schema_version": "2.1", "prior_plan_ref": pinned["plan_ref"],
            "input_world_ref": head, "replacements": [], "additional_nodes": [], "additional_dependencies": [],
            "decision": {"reason": "已登记的未来场景前提发生变化，保留其他场景供独立审核。",
                "considered_options": [{"option_id": _id(case["case_id"], "replan"),
                    "summary": "标记已失效的未来前提", "disposition": "SELECTED", "reason": "当前版本已重新验证"},
                    {"option_id": _id(case["case_id"], "keep"), "summary": "沿用旧前提",
                     "disposition": "REJECTED", "reason": "硬前提已经失效或无法确定"}]}},
            "semantic:" + case["case_id"])
        result = self._review(kernel, repo, controller, head, case, task, "PLAN_SELECTION_REQUIRES_REVIEW")
        result.update(outcome="REPLAN_PROPOSED", plan_ref=candidate["plan_ref"], report_ref=candidate["report_ref"],
                      affected_node_ids=sorted(affected), preserved_node_ids=candidate["report"]["preserved_node_ids"])
        return result

    @staticmethod
    def _review(kernel, repo, controller, head, case, task, reason):
        # A controller-owned inbox job with a frozen scope, finite deadline and no
        # provider allowance. Interactive review uses an independently issued grant.
        case_id = case["case_id"]
        review_id = _id(case_id, "review")
        work = repo.one("works", {"work_id": head["work_id"]})
        grant = kernel.authorize(repo, controller, head["work_id"], "ADMIN", head["branch_id"])
        scope = {"case_id": case_id, "affected_task_id": task["task_id"], "generation": case["generation"],
             "input_world_ref": head, "strategy": case["strategy"], "reason": reason,
             "maximum_model_calls": 0, "maximum_future_nodes": MAX_WINDOW}
        ref = kernel._blob(scope)
        kernel._register_artifact(repo, ref, head["work_id"], scope, "SEMANTIC_REVIEW_SCOPE",
                                  controller.principal_id)
        spec = {"schema_version": "2.0", "task_id": review_id, "generation": 0,
                "role_id": grant["role_id"], "task_kind": "semantic_conflict_review", "state": "BLOCKED",
                "world_ref": head, "story_time": {"tick": 0, "phase": 0}, "context_bundle_id": None,
                "dependency_fingerprint": None, "policy_version": work["policy_version"],
                "runtime_contract_version": fingerprint("nf-semantic-review-1"), "capability_grant_id": grant["grant_id"],
                "input_artifacts": [ref], "budget": {"currency": "USD", "max_amount": "0", "token_budget": 0},
                "wall_deadline": (repo.now() + timedelta(days=1)).isoformat()}
        kernel.create_task(controller, spec, "semantic:review:" + case_id)
        if task["state"] not in {"ACCEPTED", "CANCELLED", "FAILED", "STALE"}:
            _set_state(kernel, repo, task, "STALE")
        return {"outcome": "REVIEW_REQUIRED", "reason": reason, "review_task_id": review_id}
