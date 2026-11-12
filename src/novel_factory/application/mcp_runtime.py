"""Application boundary for a server-provisioned stdio binding."""
from dataclasses import dataclass

from novel_factory.application.kernel import require
from novel_factory.application.planning import validate_task_plan_binding
from novel_factory.application.queries import WorldQueryService
from novel_factory.application.query_policy import check_profile
from novel_factory.domain.canonical import fingerprint
from novel_factory.domain.temporal import point_key
from novel_factory.infrastructure.database import tables
from novel_factory.infrastructure.repository import KernelRepository
from novel_factory.orchestration.workflow import WorkflowService


@dataclass(frozen=True)
class ProjectBinding:
    work_id: str
    branch_id: str


@dataclass(frozen=True)
class CreativeControllerBinding:
    work_id: str
    branch_id: str


@dataclass(frozen=True)
class TaskBinding:
    task_id: str
    generation: int
    worker_id: str


class McpRuntime:
    def __init__(self, kernel, principal, binding):
        self.kernel = kernel
        self.principal = principal
        self.binding = binding
        self.queries = WorldQueryService(kernel)

    def available_tools(self):
        with self.kernel.db.uow(write=False) as repo:
            if isinstance(self.binding, CreativeControllerBinding):
                from novel_factory.infrastructure.creative_config import CreativeHarnessConfig

                self._creative_authority(repo)
                configuration = CreativeHarnessConfig.from_env()
                if not configuration.enabled:
                    return []
                names = ["nf_harness_simulate", "nf_harness_plan_decide", "nf_harness_review_independence",
                         "nf_harness_review_decide"]
                if configuration.dynamic_agents:
                    names.extend(("nf_harness_agent_register", "nf_harness_agent_select",
                                  "nf_harness_agent_retire", "nf_harness_agent_history"))
                if configuration.adaptive_instructions:
                    names.extend(("nf_harness_instruction_propose", "nf_harness_instruction_evaluate",
                                  "nf_harness_instruction_case", "nf_harness_instruction_decide",
                                  "nf_harness_instruction_monitor", "nf_harness_instruction_rollback"))
                    if configuration.auto_promote:
                        names.append("nf_harness_instruction_policy")
                    names.append("nf_harness_instruction_revoke_policy")
                return names
            if isinstance(self.binding, ProjectBinding):
                self._project_auth(repo.conn)
                return ["nf_project_status", "nf_resume_project"]
            task, _, _ = self._task(repo)
            spec = repo.one("task_inputs", {"task_id": task["task_id"], "generation": task["generation"]})["spec"]
            from .dynamic_runtime import assignment_for, refresh

            ref = assignment_for(repo, task, spec)
            if ref is None:
                return self._task_tools(repo)
        live = refresh(self.kernel, self.principal, task["task_id"], task["generation"], ref)
        with self.kernel.db.uow(write=False) as repo:
            return self._task_tools(repo, dynamic_ref=ref, dynamic_live=live)

    def _task_tools(self, repo, *, dynamic_ref=None, dynamic_live=None):
        task, grant, context = self._task(repo)
        identity = repo.one("task_inputs", {"task_id": task["task_id"], "generation": task["generation"]})["spec"]
        # Only the role/skill ceilings selected at provisioning time apply. A later
        # catalog revision cannot silently add tools to an existing execution.
        inputs = repo.many("artifacts", {"work_id": task["work_id"], "kind": "AGENT_INPUT",
                          "artifact_revision_id": [r["artifact_revision_id"] for r in identity["input_artifacts"]]})
        adaptive_enabled = False
        creative_enabled = False
        requested = set()
        skills = set()
        for record in inputs:
            data = record["payload"]
            if (data["role_id"], data["task_kind"], data["runtime_sha256"]) != (
                    task["role_id"], task["task_kind"], identity["runtime_contract_version"]):
                continue
            snapshot = self.kernel.artifact(repo, data["runtime_ref"], task["work_id"], kind="AGENT_RUNTIME")["payload"]
            if fingerprint(snapshot) == identity["runtime_contract_version"]:
                # Pre-P5 text-only snapshots never declared MCP tool ceilings.
                requested.update(snapshot.get("mcp_requested_tools", []))
                skills.add(snapshot["skill_name"])
                if "creative_harness" in data:
                    from novel_factory.infrastructure.creative_config import CreativeHarnessConfig
                    config = CreativeHarnessConfig.parse(data["creative_harness"])
                    creative_enabled = config.enabled
                    adaptive_enabled = config.adaptive_instructions
        caps = set(grant["capabilities"])
        if dynamic_ref is not None:
            from .dynamic_runtime import verified

            record, caps = verified(self.kernel, repo, self.principal, task, grant, dynamic_ref, dynamic_live,
                                    plane=context["read_cut"]["knowledge_cut"]["plane"])
            requested.intersection_update(record["requested_tools"])
        ceiling = {"task_get": "READ", "context_get": "READ", "query_batch": "READ",
                   "artifact_put": "ARTIFACT_WRITE"}
        names = ["nf_" + name for name, cap in ceiling.items()
                 if name in requested and cap in caps]
        if creative_enabled and "ARTIFACT_WRITE" in caps:
            role = grant["role_id"]
            if role in {"reviewer", "literary_editor", "chief_editor", "writer"} and "evidence_read" in requested:
                names.append("nf_harness_review_read")
            if role in {"reviewer", "literary_editor", "chief_editor"} and "review_submit" in requested:
                names.extend(("nf_harness_review_submit", "nf_harness_review_withdraw"))
                if adaptive_enabled:
                    names.extend(("nf_harness_instruction_evidence", "nf_harness_instruction_review"))
            if (role == "writer" and task["task_kind"] == "targeted_revision"
                    and {"artifact_put", "draft_submit"} <= requested):
                names.extend(("nf_harness_revision_brief", "nf_harness_revise"))
        if {"PLAN_READ", "ARTIFACT_WRITE"} <= caps:
            if (creative_enabled and grant["role_id"] == "volume_director"
                    and "plan_propose" in requested and context["read_cut"]["knowledge_cut"]["plane"] == "PLAN"):
                names.append("nf_harness_plan_propose")
            if (grant["role_id"], task["task_kind"]) == ("volume_director", "volume_review"):
                if "volume-director-review" in skills and "plan_propose" in requested:
                    names.append("nf_change_point_request")
            if (grant["role_id"], task["task_kind"]) == ("volume_director", "replan"):
                if "roll-window" in skills and "plan_propose" in requested:
                    names.append("nf_rolling_window_submit")
            if (grant["role_id"], task["task_kind"]) == ("chief_editor", "editorial_decision"):
                if "review-change-point" in skills and "review_submit" in requested:
                    names.append("nf_change_point_review")
        return names

    def _role_proof(self):
        from .dynamic_runtime import assignment_for, refresh

        with self.kernel.db.uow(write=False) as repo:
            task, _, _ = self._task(repo)
            spec = repo.one("task_inputs", {"task_id": task["task_id"], "generation": task["generation"]})["spec"]
            ref = assignment_for(repo, task, spec)
        if ref is None:
            return None
        live = refresh(self.kernel, self.principal, task["task_id"], task["generation"], ref)
        return ref, live

    def _role_task(self, repo, tool, proof, capability="READ"):
        if proof is not None:
            require(tool in self._task_tools(repo, dynamic_ref=proof[0], dynamic_live=proof[1]),
                    "VIEW_DENIED", "Tool outside task authority")
        return self._task(repo, capability)

    def creative_review_call(self, tool, **arguments):
        from .creative_review import CreativeReviewService

        require(tool in self.available_tools(), "VIEW_DENIED", "Creative review operation unavailable")
        service = CreativeReviewService(self.kernel)
        if tool == "nf_harness_revision_brief":
            from .creative_revision_brief import CreativeRevisionBriefService

            method = CreativeRevisionBriefService(self.kernel).read
        elif tool == "nf_harness_review_withdraw":
            arguments["action"] = "WITHDRAW"
            method = service.decide
        else:
            method = {"nf_harness_review_read": service.read, "nf_harness_review_submit": service.review,
                      "nf_harness_revise": service.revise}[tool]
        return method(self.principal, self.binding.task_id, self.binding.generation,
                      worker_id=self.binding.worker_id, **arguments)

    def _creative_authority(self, repo, task_id=None):
        binding = self.binding
        require(isinstance(binding, CreativeControllerBinding), "VIEW_DENIED", "Creative controller binding required")
        grant = self.kernel.authorize(repo, self.principal, binding.work_id, "ADMIN", binding.branch_id)
        require(grant["role_id"] == "control_operator", "VIEW_DENIED", "Creative controller role required")
        if task_id is not None:
            task = repo.one("tasks", {"task_id": task_id, "work_id": binding.work_id, "branch_id": binding.branch_id})
            require(task is not None, "VIEW_DENIED", "Task unavailable in the bound branch")

    def propose_creative_plan(self, **arguments):
        from .creative_planning import CreativePlanningService

        require("nf_harness_plan_propose" in self.available_tools(), "VIEW_DENIED", "Creative planner unavailable")
        binding = self.binding
        return CreativePlanningService(self.kernel).propose(
            self.principal, binding.task_id, binding.generation, worker_id=binding.worker_id, **arguments)

    def _creative_controller_call(self, tool, operation, arguments):
        require(tool in self.available_tools(), "VIEW_DENIED", "Creative controller operation unavailable")
        with self.kernel.db.uow(write=False) as repo:
            self._creative_authority(repo, arguments["task_id"])
        return operation(self.principal, **arguments)

    def adaptive_instruction_call(self, tool, **arguments):
        from .adaptive_evaluation import AdaptiveEvaluationService
        from .adaptive_instructions import AdaptiveInstructionService
        from .adaptive_promotion import AdaptivePromotionService

        require(tool in self.available_tools(), "VIEW_DENIED", "Instruction operation unavailable")
        promotion = AdaptivePromotionService(self.kernel)
        if tool in {"nf_harness_instruction_evidence", "nf_harness_instruction_review"}:
            proof = self._role_proof()
            with self.kernel.db.uow(write=False) as repo:
                task, _, context = self._role_task(repo, tool, proof, "ARTIFACT_WRITE")
            method = promotion.evidence if tool == "nf_harness_instruction_evidence" else promotion.review
            return method(self.principal, context["read_cut"]["world_ref"], reviewer_task_id=task["task_id"],
                          reviewer_generation=task["generation"], **arguments)
        world = arguments["world_ref"]
        require((world["work_id"], world["branch_id"]) == (self.binding.work_id, self.binding.branch_id),
                "VIEW_DENIED", "Instruction operation exceeds the bound work or branch")
        with self.kernel.db.uow(write=False) as repo:
            self._creative_authority(repo, arguments.get("task_id", arguments.get("reviewer_task_id")))
        methods = {"nf_harness_instruction_propose": AdaptiveInstructionService(self.kernel).propose,
                   "nf_harness_instruction_evaluate": AdaptiveEvaluationService(self.kernel).run,
                   "nf_harness_instruction_case": promotion.describe_case,
                   "nf_harness_instruction_decide": promotion.decide, "nf_harness_instruction_policy": promotion.policy,
                   "nf_harness_instruction_revoke_policy": promotion.revoke_policy,
                   "nf_harness_instruction_monitor": promotion.monitor, "nf_harness_instruction_rollback": promotion.rollback}
        return methods[tool](self.principal, **arguments)

    def dynamic_agent_call(self, tool, **arguments):
        require(tool in self.available_tools(), "VIEW_DENIED", "Dynamic agent operation unavailable")
        binding = self.binding
        if tool == "nf_harness_agent_register":
            with self.kernel.db.uow(write=False) as repo:
                self._creative_authority(repo, arguments["task_id"])
        else:
            world = arguments["world_ref"]
            require((world["work_id"], world["branch_id"]) == (binding.work_id, binding.branch_id),
                    "VIEW_DENIED", "Dynamic operation exceeds the bound work or branch")

        from .dynamic_agents import DynamicAgentService

        service = DynamicAgentService(self.kernel)
        methods = {"nf_harness_agent_register": service.register_revision, "nf_harness_agent_select": service.select,
                   "nf_harness_agent_retire": service.retire, "nf_harness_agent_history": service.history}
        return methods[tool](self.principal, **arguments)

    def issue_review_independence(self, *, task_id, generation, manuscript_ref):
        from .review_independence import ReviewIndependenceService

        require("nf_harness_review_independence" in self.available_tools(), "VIEW_DENIED",
                "Review independence operation unavailable")
        with self.kernel.db.uow(write=False) as repo:
            self._creative_authority(repo, task_id)
        return ReviewIndependenceService(self.kernel).issue(self.principal, manuscript_ref=manuscript_ref,
                                                             reviewer_task_id=task_id, reviewer_generation=generation)

    def decide_creative_review(self, **arguments):
        from .creative_review import CreativeReviewService

        return self._creative_controller_call(
            "nf_harness_review_decide", CreativeReviewService(self.kernel).decide, arguments)

    def simulate_creative_plan(self, **arguments):
        from .creative_simulation import CreativeSimulationService

        return self._creative_controller_call(
            "nf_harness_simulate", CreativeSimulationService(self.kernel).run, arguments)

    def decide_creative_plan(self, **arguments):
        from .creative_planning import CreativePlanningService

        return self._creative_controller_call(
            "nf_harness_plan_decide", CreativePlanningService(self.kernel).decide, arguments)

    def _project_auth(self, conn):
        repo = KernelRepository(conn, tables)
        b = self.binding
        grant = self.kernel.authorize(repo, self.principal, b.work_id, "PROJECT_RESUME", b.branch_id)
        require(grant["role_id"] == "resume_operator" and grant["branch_ids"] == [b.branch_id]
                and grant["capabilities"] == ["PROJECT_RESUME"],
                "VIEW_DENIED", "A branch-bound resume grant is required")

    def project(self, *, resume=False):
        require(isinstance(self.binding, ProjectBinding), "VIEW_DENIED", "Project grant required")
        service = WorkflowService(self.kernel.tasks)
        operation = service.resume_project if resume else service.status
        expiration = {}

        def authorize(conn):
            self._project_auth(conn)
            fence = KernelRepository(conn, tables).one("resume_grants", {"grant_id": self.principal.grant_id})
            expiration["resume_grant_expires_at"] = fence["expires_at"].isoformat()

        return {**operation(self.binding.work_id, self.binding.branch_id, authorize_in_tx=authorize),
                **expiration}

    def _task(self, repo, capability="READ"):
        b = self.binding
        require(isinstance(b, TaskBinding), "VIEW_DENIED", "Task grant required")
        task, grant, spec = self.queries._task(repo, self.principal, b.task_id, b.generation, capability)
        require(task["state"] == "RUNNING" and task["lease_owner"] == b.worker_id
                and grant["role_id"] == spec["role_id"] and grant["role_id"] not in {"control_operator", "resume_operator"}
                and "ADMIN" not in grant["capabilities"], "VIEW_DENIED", "Runtime lease unavailable")
        validate_task_plan_binding(repo, self.kernel, task, spec)
        ctx = repo.one("contexts", {"context_bundle_id": task["context_bundle_id"], "task_id": b.task_id,
                    "generation": b.generation, "principal_id": self.principal.principal_id,
                    "grant_id": self.principal.grant_id})
        require(ctx is not None, "VIEW_DENIED", "Task context unavailable")
        bundle, cut = ctx["payload"], ctx["read_cut"]
        knowledge = cut["knowledge_cut"]
        require(spec["world_ref"] == bundle["world_ref"] == cut["world_ref"]
                and spec["story_time"] == bundle["story_time"] and grant["epoch"] == cut["permission_epoch"]
                and spec["policy_version"] == bundle["policy_version"] == cut["policy_version"]
                and task["dependency_fingerprint"] == ctx["dependency_fingerprint"] == bundle["dependency_fingerprint"]
                and bundle["role_id"] == spec["role_id"] and bundle["actor_id"] == knowledge.get("actor_id")
                and point_key(spec["story_time"]) <= point_key(grant["max_story_time"]),
                "VIEW_DENIED", "Task cut unavailable")
        check_profile(grant, knowledge["plane"])
        if grant["role_id"] == "character_actor" or knowledge["plane"] == "BELIEF":
            require(knowledge["plane"] == "BELIEF" and knowledge["actor_id"] == grant["actor_id"]
                    and point_key(knowledge["known_at"]) <= point_key(spec["story_time"]),
                    "VIEW_DENIED", "Task cut unavailable")
        return task, grant, ctx

    def task_get(self):
        proof = self._role_proof()
        with self.kernel.db.uow(write=False) as repo:
            task, _, ctx = self._role_task(repo, "nf_task_get", proof)
            return {**{k: task[k] for k in ("task_id", "generation", "state", "context_bundle_id")},
                    "world_ref": ctx["payload"]["world_ref"]}

    def context_get(self):
        proof = self._role_proof()
        with self.kernel.db.uow(write=False) as repo:
            _, _, ctx = self._role_task(repo, "nf_context_get", proof)
            return ctx["payload"]

    def query_batch(self, lookups):
        proof = self._role_proof()
        with self.kernel.db.uow(write=False) as repo:
            task, grant, ctx = self._role_task(repo, "nf_query_batch", proof)
            cut = ctx["read_cut"]
            check_profile(grant, cut["knowledge_cut"]["plane"], operation="STATE")
            compiled = self.kernel.artifact(repo, ctx["artifact_ref"], task["work_id"], kind="COMPILED_CONTEXT")
            manifest = self.kernel.artifact(repo, compiled["payload"]["input_manifest_ref"],
                                            task["work_id"], kind="INPUT_MANIFEST")["payload"]
            require(all(item["subject_id"] in manifest["subject_ids"] and item["predicate"] in manifest["predicates"]
                        for item in lookups), "VIEW_DENIED", "Requested material outside the task")
            request = {"schema_version": "2.1", "task_id": task["task_id"], "generation": task["generation"],
                       "knowledge_cut": cut["knowledge_cut"],
                       "read_contract": {"revision": {"mode": "FIXED", "world_ref": cut["world_ref"]},
                                         "process_policy": "ALLOW_PARTIAL", "index_policy": "AUTHORITATIVE", "max_wait_ms": 0},
                       "items": [{"item_id": str(index), "subject_time": {"kind": "POINT", "at": ctx["payload"]["story_time"]},
                                  "query": {"kind": "STATE", "subject_ids": [item["subject_id"]],
                                            "predicates": [item["predicate"]]}, "page_size": 1}
                                 for index, item in enumerate(lookups)]}
        response = self.queries.batch(self.principal, request)
        with self.kernel.db.uow(write=False) as repo:
            self._role_task(repo, "nf_query_batch", proof)
        return {"items": response["items"], "all_items_succeeded": response["all_items_succeeded"]}

    def artifact_put(self, content, idempotency_key):
        proof = self._role_proof()
        with self.kernel.db.uow(write=False) as repo:
            self._role_task(repo, "nf_artifact_put", proof, "ARTIFACT_WRITE")
        payload = {"task_id": self.binding.task_id, "generation": self.binding.generation, "content": content}
        ref = self.kernel._blob(payload)
        with self.kernel.objects.lock(), self.kernel.db.uow() as repo:
            repo.one("tasks", {"task_id": self.binding.task_id}, lock="update")
            task, _, ctx = self._role_task(repo, "nf_artifact_put", proof, "ARTIFACT_WRITE")
            key = f"{task['task_id']}:{task['generation']}:{idempotency_key}"
            old = self.kernel._prior(repo, self.principal, task["work_id"], "MCP_ARTIFACT", key, payload)
            if old:
                return old
            self.kernel._register_artifact(repo, ref, task["work_id"], payload, "MCP_TASK_CANDIDATE",
                                           self.principal.principal_id)
            result = {"artifact_ref": ref, "status": "CANDIDATE_STORED"}
            self.kernel._receipt(repo, self.principal, ctx["payload"]["world_ref"], "MCP_ARTIFACT", key, payload, result)
            return result

    def _editorial_call(self, tool_name, operation, arguments):
        # Direct application calls and MCP transport share the frozen tool ceiling.
        require(tool_name in self.available_tools(), "VIEW_DENIED", "Unavailable editorial tool")
        with self.kernel.db.uow(write=False) as repo:
            task, _, ctx = self._task(repo, "ARTIFACT_WRITE")
        return operation(self.principal, ctx["payload"]["world_ref"], task_id=task["task_id"],
                         generation=task["generation"], worker_id=self.binding.worker_id, **arguments)

    def request_change(self, **arguments):
        from novel_factory.application.rolling_plans import RollingPlanning
        return self._editorial_call("nf_change_point_request", RollingPlanning(self.kernel).request_change, arguments)

    def review_change(self, **arguments):
        from novel_factory.application.rolling_plans import RollingPlanning
        return self._editorial_call("nf_change_point_review", RollingPlanning(self.kernel).review, arguments)

    def submit_window(self, **arguments):
        from novel_factory.application.rolling_plans import RollingPlanning
        return self._editorial_call("nf_rolling_window_submit", RollingPlanning(self.kernel).propose_window, arguments)
