"""Deterministic, scoped supervisor for pre-authorized project tasks."""
from uuid import UUID, uuid4, uuid5

from sqlalchemy import text

from novel_factory.application.authorization import Principal
from novel_factory.application.kernel import require
from novel_factory.application.queries import WorldQueryService
from novel_factory.application.query_policy import check_profile, profiles
from novel_factory.contracts.registry import validate_contract
from novel_factory.domain.errors import DomainError
from novel_factory.infrastructure.codex_host import CodexAppServerAdapter
from novel_factory.infrastructure.database import tables
from novel_factory.infrastructure.model_gateway import OpenAICompatibleAdapter
from novel_factory.infrastructure.repository import KernelRepository
from novel_factory.orchestration.workflow import WorkflowService


class HostJournal:
    """Every host identity is subordinate to the existing paid-call attempt."""
    def __init__(self, kernel):
        self.kernel = kernel

    def authorize_send(self, attempt_id):
        with self.kernel.db.uow() as repo:
            call = repo.one("call_attempts", {"attempt_id": attempt_id})
            require(call and call["state"] == "SENT", "INVALID_CALL_TRANSITION", "No live host call")
            task = self.kernel.tasks.guard_task(repo.conn, call["task_id"], call["generation"])
            owner = Principal(task["grant_id"], task["principal_id"])
            _, grant, spec = WorldQueryService(self.kernel)._task(repo, owner, task["task_id"], task["generation"])
            job = repo.one("supervisor_jobs", {"task_id": task["task_id"]})
            if job and job["config"].get("editorial"):
                from novel_factory.application.editorial_handoff import check_authorizer
                check_authorizer(self.kernel, repo, job["config"], task)
            ctx = repo.one("contexts", {"context_bundle_id": task["context_bundle_id"]})
            require(task["state"] == "RUNNING" and ctx and ctx["grant_id"] == grant["grant_id"]
                    and ctx["read_cut"]["permission_epoch"] == grant["epoch"]
                    and ctx["read_cut"]["policy_version"] == spec["policy_version"],
                    "VIEW_DENIED", "Host grant or knowledge cut changed")

    def record(self, attempt_id, thread_id, turn_id, phase):
        with self.kernel.db.uow() as repo:
            call = repo.one("call_attempts", {"attempt_id": attempt_id})
            require(call is not None, "INVALID_CALL_TRANSITION", "Unreserved host turn")
            row = repo.one("host_turns", {"attempt_id": attempt_id}, lock="update")
            if phase == "THREAD_STARTED":
                require(not row and call["state"] == "SENT", "INVALID_CALL_TRANSITION", "Call already has a host")
                repo.add("host_turns", {"attempt_id": attempt_id, "thread_id": thread_id, "phase": phase})
                return
            previous = {"TURN_STARTED": "THREAD_STARTED", "COMPLETED": "TURN_STARTED"}
            require(row and row["thread_id"] == thread_id and row["phase"] == previous.get(phase)
                    and turn_id and row["turn_id"] in (None, turn_id),
                    "HOST_BINDING_MISMATCH", "Host identifiers do not match their attempt")
            repo.change("host_turns", {"attempt_id": attempt_id},
                        {"turn_id": turn_id, "phase": phase, "updated_at": repo.now()})


class RuntimeSupervisor:
    def __init__(self, kernel, gateway):
        self.kernel, self.gateway = kernel, gateway
        self.workflow = WorkflowService(kernel.tasks)

    def enroll(self, controller, task_id, *, manifest_ref, knowledge_cut, context_tokens,
               input_ref, budget_id, reserve_amount, token_reservation, accept_summary=False, seal_interaction_id=None, editorial=None, continue_editorial=False):
        """Freeze one dispatch allowance. Nothing in the model prompt selects a route or owner."""
        with self.kernel.db.uow() as repo:
            task = repo.one("tasks", {"task_id": task_id})
            require(task, "VIEW_DENIED", "Task unavailable")
            self.kernel.authorize(repo, controller, task["work_id"], "ADMIN", task["branch_id"])
            task = repo.one("tasks", {"task_id": task_id}, lock="update")
            spec = repo.one("task_inputs", {"task_id": task_id, "generation": task["generation"]})["spec"]
            owner = Principal(task["grant_id"], task["principal_id"])
            _, grant = self.kernel._task_owner(repo, owner, task_id, "COMPILE")
            require(manifest_ref in spec["input_artifacts"] and input_ref in spec["input_artifacts"],
                    "VIEW_DENIED", "Dispatch materials must be pinned in the task")
            self.kernel.artifact(repo, manifest_ref, task["work_id"], kind="INPUT_MANIFEST")
            payload = self.kernel.artifact(repo, input_ref, task["work_id"], kind="AGENT_INPUT")["payload"]
            route = self.gateway.adapters.get(payload["adapter_id"])
            require(isinstance(route, (OpenAICompatibleAdapter, CodexAppServerAdapter)),
                    "HOST_ISOLATION_UNVERIFIED", "Supervised runtime roles require a stateless text route")
            validate_contract("KnowledgeCut", knowledge_cut, "2.1")
            check_profile(grant, knowledge_cut["plane"])
            if task["role_id"] == "character_actor":
                require(knowledge_cut["plane"] == "BELIEF" and knowledge_cut["actor_id"] == grant["actor_id"],
                        "VIEW_DENIED", "Actor cut does not match its grant")
            require(type(context_tokens) is int and 256 <= context_tokens <= 200000,
                    "INVALID_ARGUMENT", "Context budget is out of range")
            budget = repo.one("budgets", {"budget_id": budget_id, "work_id": task["work_id"]})
            require(budget, "VIEW_DENIED", "Budget unavailable")
            dispatch = dict(input_ref=input_ref, budget_id=budget_id, reserve_amount=reserve_amount,
                            token_reservation=token_reservation)
            validate_contract("AgentDispatchRequest", {"schema_version": "2.1", "task_id": task_id,
                "generation": 1, "worker_id": "enrollment", "call_key": "candidate", **dispatch}, "agent")
            require(type(accept_summary) is bool and (not accept_summary or
                    (task["role_id"], task["task_kind"]) == ("evidence_maintainer", "summary")),
                    "VIEW_DENIED", "Automatic completion is limited to an explicitly enrolled evidence summary")
            config = {"accept_summary": accept_summary, "manifest_ref": manifest_ref, "knowledge_cut": knowledge_cut, "context_tokens": context_tokens,
                      "dispatch": dispatch, "grant_epoch": grant["epoch"], "policy_version": spec["policy_version"]}
            if seal_interaction_id is not None:
                validate_contract("UUID", seal_interaction_id)
                interaction = repo.one("simulation_interactions", {"interaction_id": seal_interaction_id,
                    "work_id": task["work_id"], "branch_id": task["branch_id"]})
                slot = {"actor_id": grant["actor_id"], "grant_id": grant["grant_id"]}
                require(task["role_id"] == "character_actor" and payload["output_contract"] ==
                        {"name": "ActionProposal", "version": "2.0"} and interaction
                        and interaction["payload"]["base_world_ref"] == spec["world_ref"]
                        and interaction["payload"]["story_time"] == spec["story_time"]
                        and slot in interaction["payload"]["participant_slots"], "VIEW_DENIED",
                        "Sealed handoff requires this actor's exact slot, time, world and grant")
                config["seal_interaction_id"] = seal_interaction_id
            require(type(continue_editorial) is bool and (not continue_editorial or isinstance(editorial, dict)
                    and editorial.get("operation") in {"request_change", "propose_window"}),
                    "INVALID_ARGUMENT", "Only review and window jobs may create a bounded successor")
            if editorial is not None:
                from novel_factory.application.editorial_handoff import check_enrollment
                check_enrollment(self.kernel, repo, owner, task, spec, knowledge_cut, payload, editorial)
                config["editorial"] = editorial
                authorizer = self.kernel.authorize(repo, controller, task["work_id"], "ADMIN", task["branch_id"])
                config["editorial_authorizer"] = {"grant_id": controller.grant_id, "principal_id": controller.principal_id,
                                                   "epoch": authorizer["epoch"]}
                if continue_editorial:
                    config["continue_editorial"] = True
            old = repo.one("supervisor_jobs", {"task_id": task_id})
            if old:
                require(old["config"] == config, "IDEMPOTENCY_CONFLICT", "Task dispatch allowance already exists")
            else:
                require(task["state"] in {"BLOCKED", "READY", "RUNNING"}, "INVALID_TASK_TRANSITION",
                        "Cannot enroll a finished task")
                repo.add("supervisor_jobs", {"task_id": task_id, "config": config})
            return {"task_id": task_id, "enrolled": True}

    def run_once(self, controller, work_id, branch_id):
        def authorize(conn):
            self.kernel.authorize(KernelRepository(conn, tables), controller, work_id, "ADMIN", branch_id)
        from novel_factory.application.editorial_handoff import EditorialDelivery
        decided = EditorialDelivery(self.kernel, self.gateway).deliver_one(controller, work_id, branch_id)
        if decided:
            return decided
        self._deliver_candidates(controller, work_id, branch_id)
        summary = self.workflow.resume_project(work_id, branch_id, authorize_in_tx=authorize)
        with self.kernel.db.uow() as repo:
            authorize(repo.conn)
            stale = repo.conn.execute(text("""
                SELECT t.task_id::text,t.generation FROM nf_tasks t JOIN nf_supervisor_jobs j USING(task_id)
                WHERE t.work_id=:work AND t.branch_id=:branch AND t.state='READY'
                  AND EXISTS(SELECT FROM nf_call_attempts c WHERE c.task_id=t.task_id)
                ORDER BY t.updated_at LIMIT 32 FOR UPDATE OF t SKIP LOCKED
            """), {"work": work_id, "branch": branch_id}).all()
            for task_id, generation in stale:
                repo.change("tasks", {"task_id": task_id}, {"state": "PAUSED", "lease_owner": None,
                            "lease_expiry": None, "updated_at": repo.now()})
                self.kernel.tasks._emit(repo.conn, task_id, generation, "PAUSED")
        if stale:
            summary.update(self.workflow.status(work_id, branch_id, authorize_in_tx=authorize))
        with self.kernel.db.uow(write=False) as repo:
            authorize(repo.conn)
            ids = list(repo.conn.scalars(text("""
                SELECT t.task_id::text FROM nf_tasks t JOIN nf_supervisor_jobs j USING(task_id)
                JOIN nf_grants g ON g.grant_id=t.grant_id JOIN nf_works w ON w.work_id=t.work_id
                WHERE t.work_id=:work AND t.branch_id=:branch AND t.state='READY' AND t.deadline>clock_timestamp()
                  AND g.active AND g.principal_id::text=t.principal_id AND g.work_id=t.work_id AND g.role_id=t.role_id
                  AND g.epoch=(j.config->>'grant_epoch')::bigint AND w.policy_version=j.config->>'policy_version'
                  AND (g.branch_ids='["*"]'::jsonb OR g.branch_ids @> jsonb_build_array(t.branch_id))
                  AND g.capabilities @> '["TASK_CLAIM","READ","COMPILE"]'::jsonb
                  AND NOT EXISTS(SELECT FROM nf_call_attempts c WHERE c.task_id=t.task_id)
                ORDER BY t.updated_at,t.task_id LIMIT 32
            """), {"work": work_id, "branch": branch_id}))
        for task_id in ids:
            worker = "supervisor:" + str(uuid4())
            with self.kernel.db.uow(write=False) as repo:
                task = repo.one("tasks", {"task_id": task_id})
                config = repo.one("supervisor_jobs", {"task_id": task_id})["config"]
                owner = Principal(task["grant_id"], task["principal_id"])
            def authorized_claim(conn):
                authorize(conn)
                repo = KernelRepository(conn, tables)
                _, grant = self.kernel._task_owner(repo, owner, task_id, "TASK_CLAIM")
                require(grant["epoch"] == config["grant_epoch"]
                        and repo.one("works", {"work_id": work_id})["policy_version"] == config["policy_version"],
                        "TASK_REBIND_REQUIRED", "Dispatch allowance is no longer current")
            try:
                claimed = self.kernel.tasks.claim(worker, work_id=work_id, principal_id=owner.principal_id,
                    grant_id=owner.grant_id, task_id=task_id, lease_seconds=900, authorize_in_tx=authorized_claim)
            except DomainError as exc:
                if exc.code not in {"TASK_REBIND_REQUIRED", "VIEW_DENIED"}:
                    raise
                continue
            if claimed:
                return self._execute(owner, claimed, worker, config)
        return {"status": "IDLE", **summary}

    def _deliver_candidates(self, controller, work_id, branch_id):
        """Recover the completion/outbox boundary even if the prior worker died.

        Accept validated summaries or seal one specifically enrolled actor
        slot. World effects and joint adjudication use independent decisions.
        """
        with self.kernel.objects.lock(), self.kernel.db.uow() as repo:
            self.kernel.authorize(repo, controller, work_id, "ADMIN", branch_id)
            rows = repo.conn.execute(text("""
                SELECT t.task_id::text AS task_id, j.config FROM nf_tasks t JOIN nf_supervisor_jobs j USING(task_id)
                JOIN nf_grants g ON t.grant_id=g.grant_id
                JOIN nf_works w ON w.work_id=t.work_id
                JOIN nf_call_attempts c ON c.task_id=t.task_id AND c.generation=t.generation
                  AND c.call_key='supervised-candidate'
                JOIN nf_task_results r ON r.result_id=t.current_result_id AND r.disposition='CURRENT'
                JOIN nf_artifacts a ON a.artifact_revision_id::text=r.payload->'artifacts'->0->>'artifact_revision_id'
                  AND a.kind='AGENT_CALL_EVIDENCE' AND a.payload->>'attempt_id'=c.attempt_id::text
                  AND a.payload->>'outcome'='VALIDATED_CANDIDATE' AND a.payload->>'result_status'='SUCCEEDED'
                WHERE t.work_id=:work AND t.branch_id=:branch AND t.state='RESULT_STORED'
                  AND ((t.role_id='evidence_maintainer' AND t.task_kind='summary' AND j.config->>'accept_summary'='true')
                      OR (t.role_id='character_actor' AND j.config->>'seal_interaction_id' IS NOT NULL))
                  AND t.deadline>clock_timestamp() AND g.active AND g.epoch=(j.config->>'grant_epoch')::bigint
                  AND g.principal_id::text=t.principal_id AND g.work_id=t.work_id AND g.role_id=t.role_id
                  AND (g.branch_ids='["*"]'::jsonb OR g.branch_ids @> jsonb_build_array(t.branch_id))
                  AND g.capabilities @> '["READ"]'::jsonb
                  AND w.policy_version=j.config->>'policy_version'
                ORDER BY t.updated_at LIMIT 32 FOR UPDATE OF t SKIP LOCKED
            """), {"work": work_id, "branch": branch_id}).mappings().all()
            for row in rows:
                task = repo.one("tasks", {"task_id": row["task_id"]})
                owner = Principal(task["grant_id"], task["principal_id"])
                _, grant, _ = WorldQueryService(self.kernel)._task(repo, owner, task["task_id"], task["generation"])
                if grant["epoch"] != row["config"]["grant_epoch"]:
                    continue
                call = repo.one("call_attempts", {"task_id": task["task_id"], "generation": task["generation"],
                                                  "call_key": "supervised-candidate"})
                if not call:
                    continue
                audit = self.gateway._read_audit(repo, call["attempt_id"])
                result_id = str(uuid5(UUID(call["attempt_id"]), "task-result"))
                result = repo.one("task_results", {"result_id": result_id, "task_id": task["task_id"],
                    "generation": task["generation"], "disposition": "CURRENT"})
                audit_ref = {k: audit[k] for k in ("artifact_id", "artifact_revision_id", "sha256")} if audit else None
                if (audit and result and result["payload"]["artifacts"] == [audit_ref, audit["payload"]["output_ref"]]
                        and task["current_result_id"] == result_id and audit["payload"]["outcome"] == "VALIDATED_CANDIDATE"
                        and audit["payload"]["result_status"] == "SUCCEEDED"
                        and audit["payload"]["context_bundle_id"] == task["context_bundle_id"]):
                    interaction_id = row["config"].get("seal_interaction_id")
                    if interaction_id:
                        from novel_factory.application.simulation import WorldSchedulerService
                        try:
                            with repo.conn.begin_nested():
                                candidate = self.kernel.artifact(repo, audit["payload"]["output_ref"], work_id,
                                                                 kind="AGENT_CANDIDATE")
                                WorldSchedulerService(self.kernel).seal_in_transaction(repo, owner, interaction_id,
                                                                                      candidate["payload"])
                        except DomainError as exc:
                            # The stored result remains auditable. Repeated scans
                            # cannot spend again or spin on this rejected handoff.
                            repo.change("tasks", {"task_id": task["task_id"]}, {"state": "WAITING_REVIEW",
                                        "lease_owner": None, "lease_expiry": None, "updated_at": repo.now()})
                            self.kernel.tasks._emit(repo.conn, task["task_id"], task["generation"], "WAITING_REVIEW")
                            repo.outbox("supervisor.handoff.blocked", "task", task["task_id"],
                                {"generation": task["generation"], "reason": exc.code},
                                f"handoff:{task['task_id']}:{task['generation']}:blocked")
                            continue
                    self.kernel.tasks.accept_in_transaction(repo.conn, task["task_id"], task["generation"])

    def _execute(self, owner, task, worker, config):
        task_id, generation = task["task_id"], task["generation"]
        with self.kernel.db.uow(write=False) as repo:
            spec = repo.one("task_inputs", {"task_id": task_id, "generation": generation})["spec"]
        try:
            context = WorldQueryService(self.kernel).context_compile(owner, {
                "schema_version": "2.1", "task_id": task_id, "generation": generation,
                "recipe_id": profiles()[task["role_id"]]["allowed_recipes"][0],
                "input_manifest_ref": config["manifest_ref"], "knowledge_cut": config["knowledge_cut"],
                "read_contract": {"revision": {"mode": "FIXED", "world_ref": spec["world_ref"]},
                                  "process_policy": "ALLOW_PARTIAL", "index_policy": "AUTHORITATIVE", "max_wait_ms": 0},
                "subject_time": {"kind": "POINT", "at": spec["story_time"]}, "token_budget": config["context_tokens"],
                "idempotency_key": f"supervisor-compile:{task_id}:{generation}"})
            require(context["status"] == "READY", "CONTEXT_REQUIRED", "Task requires an available fixed context")
            result = self.gateway.execute(owner, {"schema_version": "2.1", "task_id": task_id, "generation": generation,
                "worker_id": worker, "call_key": "supervised-candidate", **config["dispatch"]})
        except DomainError as exc:
            result = {"status": "BLOCKED", "reason": exc.code}
        if result["status"] == "BLOCKED":
            # Preserve unknown calls and never repeatedly claim a known blocker.
            with self.kernel.db.uow() as repo:
                current = repo.one("tasks", {"task_id": task_id}, lock="update")
                if current["generation"] == generation and current["state"] == "RUNNING" and current["lease_owner"] == worker:
                    repo.change("tasks", {"task_id": task_id}, {"state": "PAUSED", "lease_owner": None,
                                "lease_expiry": None, "updated_at": repo.now()})
                    self.kernel.tasks._emit(repo.conn, task_id, generation, "PAUSED")
        return {**result, "task_id": task_id, "generation": generation}
