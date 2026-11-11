"""Trusted one-task delivery of validated editorial candidates to planning services."""
from contextlib import nullcontext
import json
from types import SimpleNamespace
from uuid import UUID, uuid5

from sqlalchemy import text

from novel_factory.application.authorization import Principal
from novel_factory.application.kernel import Kernel, require
from novel_factory.application.planning import PlanningService
from novel_factory.application.queries import WorldQueryService
from novel_factory.application.rolling_plans import RollingPlanning
from novel_factory.contracts.registry import validate_contract
from novel_factory.domain.errors import DomainError
from novel_factory.orchestration.workflow import review_acceptance


# All refs in a row are pinned both by the server enrollment and the TaskSpec.
_ROUTES = {
    "request_change": ("volume_director", "volume_review", "volume-director-review",
                       {"prior_plan_ref": "PLAN_REVISION", "reference_timeline_ref": "REFERENCE_TIMELINE"}),
    "propose_window": ("volume_director", "replan", "roll-window",
                      {"change_point_ref": "CHANGE_POINT", "prior_plan_ref": "PLAN_REVISION",
                       "reference_timeline_ref": "REFERENCE_TIMELINE", "volume_contract_ref": "VOLUME_CONTRACT"}),
    "review_and_adopt": ("chief_editor", "editorial_decision", "review-change-point",
                        {"change_point_ref": "CHANGE_POINT", "plan_ref": "PLAN_REVISION",
                         "window_ref": "WINDOW_PLAN", "reference_timeline_ref": "REFERENCE_TIMELINE"}),
}


def check_enrollment(kernel, repo, owner, task, spec, cut, agent_input, route):
    name = route.get("operation") if isinstance(route, dict) else None
    require(isinstance(name, str) and name in _ROUTES, "INVALID_ARGUMENT", "Unknown editorial delivery")
    role, mode, skill, refs = _ROUTES[name]
    require(set(route) == {"operation", "volume_id", *refs}
            and (task["role_id"], task["task_kind"]) == (role, mode) and cut["plane"] == "PLAN",
            "VIEW_DENIED", "Editorial delivery requires its registered role and fixed inputs")
    validate_contract("UUID", route["volume_id"])
    PlanningService(kernel)._authorize(repo, owner, spec["world_ref"])
    manifest = repo.one("selections", {"work_id": task["work_id"], "selection_kind": "PLAN_MANIFEST:" + route["volume_id"]})
    require(manifest and manifest["artifact_ref"] in spec["input_artifacts"]
            and cut == {"plane": "PLAN", "plan_manifest_ref": manifest["artifact_ref"]},
            "VIEW_DENIED", "The task does not own this volume's selected plan cut")
    runtime = kernel.artifact(repo, agent_input["runtime_ref"], task["work_id"], kind="AGENT_RUNTIME")["payload"]
    require(runtime["skill_name"] == skill, "VIEW_DENIED", "Wrong editorial skill")
    materials = {}
    for key, kind in refs.items():
        ref = route[key]
        require(ref in spec["input_artifacts"], "VIEW_DENIED", "Editorial input is outside the task")
        materials[key] = kernel.artifact(repo, ref, task["work_id"], kind=kind)["payload"]
    anchor = materials["reference_timeline_ref"]
    require(anchor["volume_id"] == route["volume_id"] and anchor["world_ref"]["branch_id"] == task["branch_id"],
            "VIEW_DENIED", "Reference does not belong to the enrolled volume and branch")
    frozen = repo.one("selections", {"work_id": task["work_id"],
        "selection_kind": f"REFERENCE_TIMELINE:{task['branch_id']}:{route['volume_id']}"})
    require(frozen and frozen["artifact_ref"] == route["reference_timeline_ref"], "VIEW_DENIED",
            "The reference differs from the frozen timeline")
    if "change_point_ref" in materials:
        cp = materials["change_point_ref"]
        require(cp["volume_id"] == route["volume_id"] and cp["reference_timeline_ref"] == route["reference_timeline_ref"]
                and cp["input_world_ref"] == spec["world_ref"], "VIEW_DENIED", "ChangePoint belongs to another cut")
    if "prior_plan_ref" in materials:
        prior = materials["prior_plan_ref"]
        require(prior["volume_id"] == route["volume_id"] and repo.ancestor(prior["input_world_ref"], spec["world_ref"]),
                "VIEW_DENIED", "Prior plan does not apply to this volume")
    if name == "propose_window":
        require(cp["prior_plan_ref"] == route["prior_plan_ref"] and prior["volume_contract_ref"] == route["volume_contract_ref"],
                "VIEW_DENIED", "Window inputs do not match the ChangePoint and volume contract")
    elif name == "review_and_adopt":
        require(materials["plan_ref"]["volume_id"] == route["volume_id"]
                and materials["plan_ref"]["prior_plan_ref"] == cp["prior_plan_ref"]
                and materials["plan_ref"]["input_world_ref"] == materials["window_ref"]["input_world_ref"] == spec["world_ref"],
                "VIEW_DENIED", "Review inputs do not match the proposed plan lineage")
    return materials


def check_authorizer(kernel, repo, cfg, task):
    authorization = cfg["editorial_authorizer"]
    enrolled_by = Principal(authorization["grant_id"], authorization["principal_id"])
    grant = kernel.authorize(repo, enrolled_by, task["work_id"], "ADMIN", task["branch_id"])
    require(grant["epoch"] == authorization["epoch"], "VIEW_DENIED", "Editorial preauthorization changed")
    return enrolled_by


def prompt_material(kernel, principal, binding):
    task, spec = binding["task"], binding["spec"]
    if (spec["role_id"], spec["task_kind"]) not in {(r, m) for r, m, _, _ in _ROUTES.values()}:
        return None
    with kernel.db.uow(write=False) as repo:
        job = repo.one("supervisor_jobs", {"task_id": task["task_id"]})
        route = job["config"].get("editorial") if job else None
        if not route:
            return None
        check_authorizer(kernel, repo, job["config"], task)
        current, grant, _ = WorldQueryService(kernel)._task(repo, principal, task["task_id"], task["generation"])
        require(grant["epoch"] == job["config"]["grant_epoch"] and current["state"] == "RUNNING",
                "VIEW_DENIED", "Editorial execution no longer authorized")
        material = check_enrollment(kernel, repo, principal, task, spec, binding["context"]["read_cut"]["knowledge_cut"],
                                    binding["input"], route)
        packet = {"operation": route["operation"], "volume_id": route["volume_id"],
                  "materials": {k: {"ref": route[k], "content": v} for k,v in material.items()}}
        if route["operation"] == "request_change":
            receipt = review_acceptance(repo.conn, task, spec)
            require(receipt["world_ref"] == spec["world_ref"], "TASK_REBIND_REQUIRED", "Wake receipt changed")
            packet["accepted_event_refs"] = receipt["accepted_event_refs"]
        return packet


class EditorialDelivery:
    def __init__(self, kernel, gateway):
        self.kernel, self.gateway = kernel, gateway

    def deliver_one(self, controller, work, branch):
        with self.kernel.db.uow(write=False) as repo:
            self.kernel.authorize(repo, controller, work, "ADMIN", branch)
            task_id = repo.conn.scalar(text("""
                SELECT t.task_id::text FROM nf_tasks t JOIN nf_supervisor_jobs j USING(task_id)
                WHERE t.work_id=:work AND t.branch_id=:branch AND t.state='RESULT_STORED'
                AND j.config->'editorial' IS NOT NULL ORDER BY t.updated_at,t.task_id LIMIT 1
            """), {"work": work, "branch": branch})
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
            key = f"editorial:{task_id}:{generation}"
            try:
                with repo.conn.begin_nested():
                    result = self._deliver(repo, task, key)
            except DomainError as exc:
                repo.change("tasks", {"task_id": task_id}, {"state": "WAITING_REVIEW",
                            "lease_owner": None, "lease_expiry": None, "updated_at": repo.now()})
                self.kernel.tasks._emit(repo.conn, task_id, generation, "WAITING_REVIEW")
                result = {"status": "WAITING_REVIEW", "reason": exc.code}
            result.update(task_id=task_id, generation=generation)
            repo.outbox("supervisor.editorial.delivery", "task", task_id, result, key)
            return result

    def _deliver(self, repo, task, key):
        owner = Principal(task["grant_id"], task["principal_id"])
        task_id, gen = task["task_id"], task["generation"]
        _, grant, spec = WorldQueryService(self.kernel)._task(repo, owner, task_id, gen)
        cfg = repo.one("supervisor_jobs", {"task_id": task_id})["config"]
        enrolled_by = check_authorizer(self.kernel, repo, cfg, task)
        require(grant["epoch"] == cfg["grant_epoch"], "VIEW_DENIED", "Editorial grant was changed")
        call = repo.one("call_attempts", {"task_id": task_id, "generation": gen, "call_key": "supervised-candidate"})
        require(call and task["current_result_id"] == str(uuid5(UUID(call["attempt_id"]), "task-result")),
                "INVALID_EVIDENCE", "Missing supervised candidate result")
        audit = self.gateway._read_audit(repo, call["attempt_id"])
        result = repo.one("task_results", {"task_id": task_id, "generation": gen, "result_id": task["current_result_id"],
                                          "disposition": "CURRENT"})
        require(audit and result, "INVALID_EVIDENCE", "Candidate evidence unavailable")
        evidence = audit["payload"]
        audit_ref = {k: audit[k] for k in ("artifact_id", "artifact_revision_id", "sha256")}
        require(evidence["outcome"] == "VALIDATED_CANDIDATE" and evidence["result_status"] == "SUCCEEDED"
                and task["context_bundle_id"] == evidence["context_bundle_id"]
                and result["payload"]["artifacts"] == [audit_ref, evidence["output_ref"]],
                "INVALID_EVIDENCE", "Editorial result does not match audited candidate")
        response = self.kernel.artifact(repo, evidence["response_ref"], task["work_id"], kind="AGENT_RESPONSE")
        require(not json.loads(response["payload"]["text"])["unresolved_questions"], "REVIEW_REQUIRED",
                "A model-reported unresolved question needs a decision")
        candidate = self.kernel.artifact(repo, evidence["output_ref"], task["work_id"], kind="AGENT_CANDIDATE")["payload"]
        # Native services share this single short transaction. No model or
        # network I/O is permitted during candidate delivery.
        bound = SimpleNamespace(engine=SimpleNamespace(begin=lambda: nullcontext(repo.conn)),
                                uow=lambda **kwargs: nullcontext(repo))
        kernel = Kernel(bound, self.kernel.objects)
        planning, rolling = PlanningService(kernel), RollingPlanning(kernel)
        wr, route = spec["world_ref"], cfg["editorial"]
        materials = check_enrollment(kernel, repo, owner, task, spec, cfg["knowledge_cut"],
                    kernel.artifact(repo, cfg["dispatch"]["input_ref"], task["work_id"], kind="AGENT_INPUT")["payload"], route)
        binding = {"task_id": task_id, "generation": gen, "worker_id": task["lease_owner"], "idempotency_key": key}
        outcome = {"status": "EDITORIAL_DELIVERED", "candidate_ref": evidence["output_ref"], "operation": route["operation"]}
        if route["operation"] == "request_change":
            if candidate["important_deltas"]:
                outcome["change_point_ref"] = rolling.request_change(owner, wr, **binding, volume=route["volume_id"],
                    prior_plan_ref=route["prior_plan_ref"], event_refs=candidate["event_refs"], reason=candidate["text"])
            else:
                outcome["decision"] = "NO_MAJOR_CHANGE"
        elif route["operation"] == "propose_window":
            outcome.update(rolling.propose_window(owner, wr, **binding, change_point_ref=route["change_point_ref"],
                                                   window=candidate))
        else:
            decision = candidate["decision"]
            require(decision in {"APPROVED", "RETURNED"} and not candidate["deferred_questions"],
                    "REVIEW_REQUIRED", "Upper planning did not reach a final decision")
            reviewed = rolling.review(owner, wr, **binding, change_point_ref=route["change_point_ref"],
                        plan_ref=route["plan_ref"], window_ref=route["window_ref"], decision=decision, reason=candidate["reason"])
            outcome.update(decision=decision, review_ref=reviewed)
            if decision == "APPROVED":
                outcome["adopted"] = planning.adopt_plan(enrolled_by, wr, route["plan_ref"],
                    materials["change_point_ref"]["prior_plan_ref"], key, rolling_review_ref=reviewed)
        if repo.one("tasks", {"task_id": task_id})["state"] != "ACCEPTED":
            kernel.tasks.accept_in_transaction(repo.conn, task_id, gen)
        if cfg.get("continue_editorial"):
            from novel_factory.application.editorial_successors import provision_successor
            child = provision_successor(kernel, self.gateway, enrolled_by, task, spec, cfg, outcome)
            if child:
                outcome["successor_task_id"] = child
        return outcome
