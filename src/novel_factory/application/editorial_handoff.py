"""Trusted one-task delivery of validated editorial candidates to planning services."""

from novel_factory.application.authorization import Principal
from novel_factory.application.kernel import require
from novel_factory.application.planning import PlanningService
from novel_factory.application.queries import WorldQueryService
from novel_factory.application.rolling_plans import RollingPlanning
from novel_factory.contracts.registry import validate_contract
from novel_factory.application.candidate_delivery import CandidateDelivery
from novel_factory.application.editorial_effects import review_effects


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
        selected = repo.one("selections", {"work_id": task["work_id"],
            "selection_kind": PlanningService.selection_key(route["volume_id"])})
        require(selected and selected["artifact_ref"] == route["prior_plan_ref"]
                and prior["volume_id"] == route["volume_id"] and repo.ancestor(prior["input_world_ref"], spec["world_ref"]),
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
        current, grant, live_spec = WorldQueryService(kernel)._task(repo, principal, task["task_id"], task["generation"])
        require(grant["epoch"] == job["config"]["grant_epoch"] and current["state"] == "RUNNING" and live_spec == spec,
                "VIEW_DENIED", "Editorial execution no longer authorized")
        material = check_enrollment(kernel, repo, principal, task, spec, binding["context"]["read_cut"]["knowledge_cut"],
                                    binding["input"], route)
        packet = {"operation": route["operation"], "volume_id": route["volume_id"],
                  "materials": {k: {"ref": route[k], "content": v} for k,v in material.items()}}
        if route["operation"] == "request_change":
            packet.update(review_effects(kernel, repo, current, live_spec, job["config"]["manifest_ref"],
                                         binding["context"]["read_cut"]))
        return packet


class EditorialDelivery(CandidateDelivery):
    route = "editorial"

    def _deliver(self, repo, task, key):
        owner = Principal(task["grant_id"], task["principal_id"])
        task_id, gen = task["task_id"], task["generation"]
        _, grant, spec = WorldQueryService(self.kernel)._task(repo, owner, task_id, gen)
        cfg = repo.one("supervisor_jobs", {"task_id": task_id})["config"]
        enrolled_by = check_authorizer(self.kernel, repo, cfg, task)
        require(grant["epoch"] == cfg["grant_epoch"], "VIEW_DENIED", "Editorial grant was changed")
        evidence, candidate = self.audited_candidate(repo, task)
        kernel = self.kernel.in_transaction(repo)
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
                    "AUTHOR_DECISION_REQUIRED", "Upper planning did not reach a final decision")
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
        from novel_factory.application.scene_continuation import continue_after_review
        continue_after_review(kernel, repo, self.gateway, enrolled_by, task, spec, outcome)
        return outcome
