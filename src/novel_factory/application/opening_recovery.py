"""Repair a returned, invalid opening intent without changing its actor or accepted world."""
import json
from copy import deepcopy
from uuid import uuid4

from sqlalchemy import text

from .authorization import Principal
from .volume_successor import _selected
from .idea_repair import UnparsedDesign, returned_design
from .kernel import require
from .planning import PlanningService
from .production_pipeline import _artifact
from .quality_memory import PAUSED, plan_repair, reflect_repair
from .semantic_dependencies import check_task_dependencies
from .supervisor import RuntimeSupervisor


def verify_director_binding(kernel, repo, task, input_ref):
    """Bind a rejected initial director to the current opening and its accepted joint event."""
    from .idea_opening import _delivery
    work, branch = task["work_id"], task["branch_id"]
    ref = _selected(repo, work, "P6_OPENING:" + branch)
    require(ref and not _selected(repo, work, "P6_OPENING_DONE:" + branch), "VIEW_DENIED", "Opening already changed")
    opening = kernel.artifact(repo, ref, work, kind="P6_OPENING")["payload"]
    sim = opening["simulation"]
    config = repo.one("supervisor_jobs", {"task_id":task["task_id"]})["config"]
    joint = _delivery(repo, "joint", sim["adjudicator_task_id"])
    require(task["task_id"] == sim["director_task_id"] and (task["role_id"], task["task_kind"]) ==
            ("volume_director", "volume_review") and config["dispatch"]["input_ref"] == input_ref
            and config.get("editorial", {}).get("operation") == "request_change" and config["continue_editorial"]
            and config["editorial"]["reference_timeline_ref"] == sim["reference_timeline_ref"]
            and config["knowledge_cut"]["plane"] == "PLAN" and joint and joint["status"] == "JOINT_ACCEPTED"
            and joint["world_ref"] == kernel.head(repo, work, branch), "VIEW_DENIED", "Director task no longer owns this cut")
    from .editorial_handoff import check_authorizer
    require(check_authorizer(kernel, repo, config, task) == Principal(opening["grant_id"], opening["principal_id"]),
            "VIEW_DENIED", "Opening director authorizer changed")
    return joint["world_ref"]


def recover_opening_director(kernel, repo, gateway, owner, opening, task_id):
    from .production_profiles import load_profile
    wr = opening["world_ref"]
    if task_id != opening["simulation"]["director_task_id"]:
        return None
    task = repo.one("tasks", {"work_id":wr["work_id"], "branch_id":wr["branch_id"], "task_id":task_id}, lock="update")
    if task["state"] != "FAILED":
        return None
    profile = load_profile(kernel, repo, owner, wr["work_id"], wr["branch_id"])
    if not profile or profile["profile"].get("production_mode") != "quality":
        return None
    config = repo.one("supervisor_jobs", {"task_id":task_id})["config"]
    input_ref = config["dispatch"]["input_ref"]
    cut = verify_director_binding(kernel, repo, task, input_ref)
    result, audit, ref = returned_design(kernel, repo, gateway, task, cut, {"input_ref":input_ref},
                                        allow_contract=True, allow_malformed=True, opening_director=True)
    require(isinstance(result, UnparsedDesign), "INVALID_EVIDENCE", "No verified director format rejection")
    history = opening.setdefault("director_repairs", [])
    plan = reflect_repair(kernel, repo, owner, cut, task_id, audit["attempt_id"], ref,
                          plan_repair("schema", result.diagnostic, history), "OPENING_DIRECTOR")
    if plan["decision"] == PAUSED:
        return {"status":"OPENING_REVIEW_REQUIRED", "reason":PAUSED, "task_id":task_id}
    history.append({**plan, "task_id":task_id, "attempt_id":audit["attempt_id"]})
    bound = kernel.in_transaction(repo)
    supervisor = RuntimeSupervisor(bound, gateway.with_kernel(bound))
    successor = _successor(bound, repo, supervisor, owner, task, director_cut=cut,
                           diagnosis=result.diagnostic, method=plan["method"])
    supervisor.workflow.wait(successor, 0, [opening["simulation"]["control_task_id"]],
        work_id=wr["work_id"], branch_id=wr["branch_id"], mode="AND",
        authorize_in_tx=lambda _: bound.authorize(repo, owner, wr["work_id"], "ADMIN", wr["branch_id"]))
    opening["simulation"]["director_task_id"] = successor
    new_ref = _artifact(kernel, repo, wr["work_id"], owner, "P6_OPENING", opening)
    PlanningService(bound)._select(repo, cut, "P6_OPENING:" + wr["branch_id"], new_ref)
    return {"status":"OPENING_DIRECTOR_REPAIR_ENROLLED", "task_id":successor}


def _successor(kernel, repo, supervisor, controller, task, *, actor=False, director_cut=None, diagnosis=None, method=None):
    config = repo.one("supervisor_jobs", {"task_id": task["task_id"]})["config"]
    spec = deepcopy(repo.one("task_inputs", {"task_id": task["task_id"], "generation": 0})["spec"])
    grant = repo.one("grants", {"grant_id": task["grant_id"]}, lock="share")
    require(grant["active"] and grant["epoch"] == config["grant_epoch"] and task["deadline"] > repo.now()
            and spec["policy_version"] == config["policy_version"], "TASK_REBIND_REQUIRED", "Opening allowance expired")
    check_task_dependencies(repo, task["task_id"], task["generation"])
    dispatch = dict(config["dispatch"])
    new_id = str(uuid4())
    spec.update(task_id=new_id, state="READY" if actor else "BLOCKED")
    if director_cut:
        current = repo.one("task_inputs", {"task_id":task["task_id"], "generation":task["generation"]})["spec"]
        require(current["world_ref"] == director_cut, "TASK_REBIND_REQUIRED", "Director read cut changed")
        # The director claims its accepted world through the original control-task wait.
        # Its unclaimed TaskSpec must retain the earlier world, exactly like the original.
    if actor or director_cut:
        source_ref = dispatch["input_ref"]
        original = kernel.artifact(repo, source_ref, task["work_id"], kind="AGENT_INPUT")["payload"]
        require(not original.get("dynamic_assignment_ref") and (director_cut or original["output_contract"] ==
                {"name": "ActionProposal", "version": "2.0"}), "TASK_REBIND_REQUIRED", "Cannot transfer task-specific instructions")
        assignment = original["assignment"] + "\n" + json.dumps({
            "format_correction": "使用本次任务外层身份，将本角色的业务字段放入 payload，遵守冻结的字段名、类型和嵌套层级。",
            "diagnostic": diagnosis, "method": method,
        }, ensure_ascii=False)
        runtime = kernel.artifact(repo, original["runtime_ref"], task["work_id"], kind="AGENT_RUNTIME")["payload"]
        prepared = supervisor.gateway.prepare_input(controller, spec["world_ref"], role_id=task["role_id"],
            task_kind=task["task_kind"], skill_name=runtime["skill_name"], assignment=assignment, task_id=new_id,
            routing_instance_id=config["knowledge_cut"].get("actor_id"), material_refs=original["material_refs"],
            adapter_id="@policy" if original.get("routing_decision") else original["adapter_id"],
            knowledge_reread_reason="known_error")
        revised = prepared["input_ref"]
        check = kernel.artifact(repo, revised, task["work_id"], kind="AGENT_INPUT")["payload"]
        require(all(original.get(key) == check.get(key) for key in
                    ("model_config_sha256", "adapter_id", "output_contract", "runtime_sha256", "material_refs",
                     "creative_harness", "adaptive_instruction_ref")),
                "TASK_REBIND_REQUIRED", "Opening repair requires the same runtime and material authority")
        spec["input_artifacts"] = [revised if ref == source_ref else ref for ref in spec["input_artifacts"]]
        dispatch["input_ref"] = revised
    role_owner = Principal(task["grant_id"], task["principal_id"])
    kernel.create_task(role_owner, spec, "opening-recovery:" + new_id)
    if director_cut:
        route = {"editorial":config["editorial"], "continue_editorial":config["continue_editorial"]}
    elif actor:
        route = {"seal_interaction_id":config["seal_interaction_id"]}
    else:
        route = {"joint":config["joint"]}
    supervisor.enroll(controller, new_id, manifest_ref=config["manifest_ref"], knowledge_cut=config["knowledge_cut"],
                      context_tokens=config["context_tokens"], **dispatch,
                      **route)
    repo.conn.execute(text("""
        INSERT INTO nf_trace_tasks(task_id, trace_id, volume_id)
        SELECT :new_id, trace_id, volume_id FROM nf_trace_tasks WHERE task_id=:source
    """), {"new_id": new_id, "source": task["task_id"]})
    return new_id


def recover_opening(kernel, repo, gateway, owner, opening):
    sim, wr = opening["simulation"], opening["world_ref"]
    work, branch = wr["work_id"], wr["branch_id"]
    from .production_profiles import load_profile
    binding = load_profile(kernel, repo, owner, work, branch)
    if not binding or binding["profile"].get("production_mode") != "quality":
        return None
    if kernel.head(repo, work, branch) != wr:
        return None
    actor_tasks = {actor: repo.one("tasks", {"work_id": work, "branch_id": branch, "task_id": tid}, lock="update")
                   for actor, tid in sim["actor_tasks"].items()}
    rejected = {actor: task for actor, task in actor_tasks.items() if task and task["state"] == "FAILED"}
    joint = repo.one("tasks", {"work_id": work, "branch_id": branch, "task_id": sim["adjudicator_task_id"]}, lock="update")
    if not rejected or not joint or joint["state"] != "FAILED":
        return None
    # Only an unsent downstream task explicitly failed by a prerequisite may be replaced.
    closed = repo.one("outbox", {"dedupe_key": f"workflow:{joint['task_id']}:{joint['generation']}:closed"})
    require(closed and not repo.many("call_attempts", {"task_id": joint["task_id"]}),
            "CALL_RECONCILIATION_REQUIRED", "Opening adjudication is not an unsent dependency failure")
    bound = kernel.in_transaction(repo)
    supervisor = RuntimeSupervisor(bound, gateway.with_kernel(bound))
    history = opening.setdefault("actor_repairs", {})
    replacements = {}
    for actor, task in rejected.items():
        require(not repo.one("simulation_intents", {"interaction_id": sim["interaction_id"], "actor_id": actor}),
                "INVALID_EVIDENCE", "A sealed actor slot cannot be replaced")
        job = repo.one("supervisor_jobs", {"task_id": task["task_id"]})["config"]
        result, audit, ref = returned_design(kernel, repo, gateway, task, wr, {"input_ref": job["dispatch"]["input_ref"]},
                            allow_malformed=True, allow_contract=True, opening_slot=(sim["interaction_id"],actor))
        require(isinstance(result, UnparsedDesign), "INVALID_EVIDENCE", "Opening failure has no reproducible format error")
        plans = history.setdefault(actor, [])
        plan = reflect_repair(kernel, repo, owner, wr, task["task_id"], audit["attempt_id"], ref,
                              plan_repair("schema", result.diagnostic, plans), "OPENING_ACTOR")
        if plan["decision"] == PAUSED:
            return {"status": "OPENING_REVIEW_REQUIRED", "reason": PAUSED, "task_id": task["task_id"]}
        plans.append({**plan, "task_id": task["task_id"], "attempt_id": audit["attempt_id"]})
        replacements[actor] = (task, result.diagnostic, plan["method"])
    for actor, (task, diagnostic, method) in replacements.items():
        sim["actor_tasks"][actor] = _successor(bound, repo, supervisor, owner, task, actor=True,
                                              diagnosis=diagnostic, method=method)
    successor = _successor(bound, repo, supervisor, owner, joint)
    supervisor.workflow.wait(successor, 0, list(sim["actor_tasks"].values()), work_id=work, branch_id=branch,
                             mode="AND", authorize_in_tx=lambda _: bound.authorize(repo, owner, work, "ADMIN", branch))
    sim["adjudicator_task_id"] = successor
    selected = _artifact(kernel, repo, work, owner, "P6_OPENING", opening)
    PlanningService(kernel)._select(repo, wr, "P6_OPENING:" + branch, selected)
    return {"status": "OPENING_FORMAT_REPAIR_ENROLLED", "task_id": successor}
