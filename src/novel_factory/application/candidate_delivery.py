"""Shared transactional delivery and verification of supervisor-enrolled model results."""

import json
from uuid import UUID, uuid5

from sqlalchemy import text

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
        with self.kernel.objects.lock(), self.kernel.db.uow() as repo:
            repo.one("works", {"work_id": work}, lock="update")
            self.kernel.authorize(repo, controller, work, "ADMIN", branch)
            repo.one("branches", {"work_id": work, "branch_id": branch}, lock="update")
            task = repo.one("tasks", {"task_id": task_id}, lock="update")
            if task["state"] != "RESULT_STORED":
                return None
            generation = task["generation"]
            key = f"{self.route}:{task_id}:{generation}"
            try:
                with repo.conn.begin_nested():
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

    def audited_candidate(self, repo, task):
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
        response = self.kernel.artifact(repo, evidence["response_ref"], task["work_id"], kind="AGENT_RESPONSE")
        require(
            not json.loads(response["payload"]["text"])["unresolved_questions"],
            "REVIEW_REQUIRED",
            "A model-reported unresolved question needs a decision",
        )
        candidate = self.kernel.artifact(repo, evidence["output_ref"], task["work_id"], kind="AGENT_CANDIDATE")[
            "payload"
        ]
        return evidence, candidate
