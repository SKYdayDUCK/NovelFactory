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
            if isinstance(self.binding, ProjectBinding):
                self._project_auth(repo.conn)
                return ["nf_project_status", "nf_resume_project"]
            task, grant, _ = self._task(repo)
            identity = repo.one("task_inputs", {"task_id": task["task_id"], "generation": task["generation"]})["spec"]
            # Only the role/skill ceilings selected at provisioning time apply. A later
            # catalog revision cannot silently add tools to an existing execution.
            inputs = repo.many("artifacts", {"work_id": task["work_id"], "kind": "AGENT_INPUT",
                              "artifact_revision_id": [r["artifact_revision_id"] for r in identity["input_artifacts"]]})
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
            ceiling = {"task_get": "READ", "context_get": "READ", "query_batch": "READ",
                       "artifact_put": "ARTIFACT_WRITE"}
            names = ["nf_" + name for name, cap in ceiling.items()
                     if name in requested and cap in grant["capabilities"]]
            if {"PLAN_READ", "ARTIFACT_WRITE"} <= set(grant["capabilities"]):
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
        with self.kernel.db.uow(write=False) as repo:
            task, _, ctx = self._task(repo)
            return {**{k: task[k] for k in ("task_id", "generation", "state", "context_bundle_id")},
                    "world_ref": ctx["payload"]["world_ref"]}

    def context_get(self):
        with self.kernel.db.uow(write=False) as repo:
            _, _, ctx = self._task(repo)
            return ctx["payload"]

    def query_batch(self, lookups):
        with self.kernel.db.uow(write=False) as repo:
            task, grant, ctx = self._task(repo)
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
            self._task(repo)
        return {"items": response["items"], "all_items_succeeded": response["all_items_succeeded"]}

    def artifact_put(self, content, idempotency_key):
        with self.kernel.db.uow(write=False) as repo:
            self._task(repo, "ARTIFACT_WRITE")
        payload = {"task_id": self.binding.task_id, "generation": self.binding.generation, "content": content}
        ref = self.kernel._blob(payload)
        with self.kernel.objects.lock(), self.kernel.db.uow() as repo:
            repo.one("tasks", {"task_id": self.binding.task_id}, lock="update")
            task, _, ctx = self._task(repo, "ARTIFACT_WRITE")
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
