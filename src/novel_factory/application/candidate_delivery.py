"""Shared transactional delivery and verification of supervisor-enrolled model results."""

import json
from uuid import UUID, uuid5

from sqlalchemy import text

from novel_factory.application.authorization import Principal
from novel_factory.application.dynamic_runtime import refresh_task, verify_task
from novel_factory.application.kernel import require
from novel_factory.domain.errors import DomainError


class CandidateDelivery:
    """Crash-safe delivery of one audited candidate inside its native service transaction."""

    def __init__(self, kernel, gateway):
        self.kernel, self.gateway = kernel, gateway

    def deliver_one(self, controller, work, branch):
        with self.kernel.db.uow(write=False) as repo:
            self.kernel.authorize(repo, controller, work, "ADMIN", branch)
            task_id = repo.conn.scalar(
                text("""
                SELECT t.task_id::text FROM nf_tasks t JOIN nf_supervisor_jobs j USING(task_id)
                WHERE t.work_id=:work AND t.branch_id=:branch AND t.state='RESULT_STORED'
                AND jsonb_exists(j.config, :route) ORDER BY t.updated_at,t.task_id LIMIT 1
            """),
                {"work": work, "branch": branch, "route": self.route},
            )
            if not task_id:
                return None
            selected = repo.one("tasks", {"task_id": task_id})
            owner = Principal(selected["grant_id"], selected["principal_id"])
        try:
            proof = refresh_task(self.kernel, owner, task_id, selected["generation"])
            proof_error = None
        except DomainError as error:
            proof, proof_error = None, error
        with self.kernel.objects.lock(), self.kernel.db.uow() as repo:
            repo.one("works", {"work_id": work}, lock="update")
            self.kernel.authorize(repo, controller, work, "ADMIN", branch)
            repo.one("branches", {"work_id": work, "branch_id": branch}, lock="update")
            task = repo.one("tasks", {"task_id": task_id}, lock="update")
            if task["state"] != "RESULT_STORED" or task["generation"] != selected["generation"]:
                return None
            generation = task["generation"]
            key = f"{self.route}:{task_id}:{generation}"
            try:
                with repo.conn.begin_nested():
                    from novel_factory.application.semantic_dependencies import (
                        check_task_dependencies,
                    )
                    check_task_dependencies(repo, task_id, generation)
                    if proof_error:
                        raise proof_error
                    verify_task(self.kernel, repo, owner, task, proof)
                    result = self._deliver(repo, task, key)
            except DomainError as exc:
                repo.change(
                    "tasks",
                    {"task_id": task_id},
                    {
                        "state": "WAITING_REVIEW",
                        "lease_owner": None,
                        "lease_expiry": None,
                        "updated_at": repo.now(),
                    },
                )
                self.kernel.tasks._emit(repo.conn, task_id, generation, "WAITING_REVIEW")
                result = {"status": "WAITING_REVIEW", "reason": exc.code}
            result.update(task_id=task_id, generation=generation)
            repo.outbox(f"supervisor.{self.route}.delivery", "task", task_id, result, key)
            return result

    def audited_candidate(self, repo, task, *, production_accounting=False, design_revision_read=False, ending_plan_read=False):
        task_id, gen = task["task_id"], task["generation"]
        call = repo.one(
            "call_attempts", {"task_id": task_id, "generation": gen, "call_key": "supervised-candidate"}
        )
        require(
            call and task["current_result_id"] == str(uuid5(UUID(call["attempt_id"]), "task-result")),
            "INVALID_EVIDENCE",
            "Missing supervised candidate result",
        )
        audit = self.gateway._read_audit(repo, call["attempt_id"])
        result = repo.one(
            "task_results",
            {
                "task_id": task_id,
                "generation": gen,
                "result_id": task["current_result_id"],
                "disposition": "CURRENT",
            },
        )
        require(audit and result, "INVALID_EVIDENCE", "Candidate evidence unavailable")
        evidence = audit["payload"]
        audit_ref = {k: audit[k] for k in ("artifact_id", "artifact_revision_id", "sha256")}
        require(
            evidence["outcome"] == "VALIDATED_CANDIDATE"
            and evidence["result_status"] == "SUCCEEDED"
            and task["context_bundle_id"] == evidence["context_bundle_id"]
            and result["payload"]["artifacts"] == [audit_ref, evidence["output_ref"]],
            "INVALID_EVIDENCE",
            "Task result does not match audited candidate",
        )
        if "creative_context_ref" in evidence:
            from .creative_context_store import check_candidate_snapshot
            check_candidate_snapshot(self.kernel, repo, task, evidence)
        response = self.kernel.artifact(repo, evidence["response_ref"], task["work_id"], kind="AGENT_RESPONSE")
        questions = json.loads(response["payload"]["text"])["unresolved_questions"]
        candidate = self.kernel.artifact(repo, evidence["output_ref"], task["work_id"], kind="AGENT_CANDIDATE")
        if design_revision_read and questions:
            source = self.kernel.artifact(repo, evidence["input_ref"], task["work_id"], kind="AGENT_INPUT")["payload"]
            design_revision_read = (
                (task["role_id"], task["task_kind"]) == ("reviewer", "fact")
                and source["output_contract"] == {"name": "ShortDesignReview", "version": "production_profile"}
                and candidate["payload"].get("short_arc", {}).get("decision") == "ENDING_PLAN_REQUIRED"
            )
        if ending_plan_read and questions:
            from .postpaid_delivery import bound_ending_input
            source = self.kernel.artifact(repo, evidence["input_ref"], task["work_id"], kind="AGENT_INPUT")["payload"]
            ending_plan_read = bound_ending_input(repo, task, evidence["input_ref"], source["output_contract"])
        require(
            not questions or design_revision_read or ending_plan_read,
            "REVIEW_REQUIRED",
            "A model-reported unresolved question needs a decision",
        )
        if production_accounting and call["state"] != "SUCCEEDED":
            if evidence["model_configuration"].get("auth_mode") == "host-login":
                from .native_delivery import require_native_delivery as verify_delivery
            else:
                from .postpaid_delivery import require_postpaid_delivery as verify_delivery

            if ending_plan_read and questions:
                verify_delivery(self.kernel, repo, task, call, audit, response, candidate,
                                          ending_plan_read=True)
            elif design_revision_read and questions:
                verify_delivery(self.kernel, repo, task, call, audit, response, candidate,
                                          design_revision_read=True)
            else:
                verify_delivery(self.kernel, repo, task, call, audit, response, candidate)
        if questions:
            evidence = {**evidence, "revision_questions": questions}
        job = repo.one("supervisor_jobs", {"task_id": task_id})
        if job and job["config"].get("required_review_mcp"):
            from .review_evidence import require_review_evidence
            require_review_evidence(self.kernel, repo, task, evidence, candidate["payload"])
        return evidence, candidate["payload"]
