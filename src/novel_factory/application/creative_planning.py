"""Task-bound, conditional literary choices. Only PlanningService selects native plans."""
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4, uuid5

from novel_factory.application.authorization import Principal
from novel_factory.application.kernel import require
from novel_factory.application.planning import PlanningService, validate_task_plan_binding
from novel_factory.application.queries import WorldQueryService
from novel_factory.application.query_policy import check_profile
from novel_factory.application.semantic_dependencies import check_task_dependencies
from novel_factory.contracts.registry import validate_contract
from novel_factory.domain.canonical import fingerprint


def planning_binding(kernel, repo, principal, task_id, generation, *, require_current=True):
    task, grant, spec = WorldQueryService(kernel)._task(repo, principal, task_id, generation, "PLAN_READ")
    kernel.authorize(repo, principal, task["work_id"], "ARTIFACT_WRITE", task["branch_id"])
    check_profile(grant, "PLAN")
    require(task["state"] in {"RUNNING", "RESULT_STORED", "WAITING_REVIEW", "VALIDATING"}
            and grant["actor_id"] is None, "VIEW_DENIED", "A live planning assignment is required")
    ctx = repo.one("contexts", {"context_bundle_id": task["context_bundle_id"], "task_id": task_id,
                               "generation": generation, "principal_id": principal.principal_id,
                               "grant_id": principal.grant_id})
    require(ctx and ctx["payload"]["world_ref"] == spec["world_ref"] == ctx["read_cut"]["world_ref"]
            and ctx["read_cut"]["knowledge_cut"]["plane"] == "PLAN"
            and ctx["read_cut"]["permission_epoch"] == grant["epoch"],
            "VIEW_DENIED", "Planning requires the assigned PLAN cut")
    inputs = repo.many("artifacts", {"work_id": task["work_id"], "kind": "AGENT_INPUT",
                                     "artifact_revision_id": [r["artifact_revision_id"] for r in spec["input_artifacts"]]})
    profiles = [a["payload"] for a in inputs if a["payload"]["runtime_sha256"] == spec["runtime_contract_version"]
                and a["payload"]["role_id"] == spec["role_id"] and a["payload"]["task_kind"] == spec["task_kind"]]
    require(len(profiles) == 1 and profiles[0].get("creative_harness", {}).get("creative_harness", {}).get("enabled") is True,
            "VIEW_DENIED", "Creative Harness is disabled for this assignment")
    runtime = kernel.artifact(repo, profiles[0]["runtime_ref"], task["work_id"], kind="AGENT_RUNTIME")["payload"]
    require(grant["role_id"] == task["role_id"] == runtime["role_id"] == "volume_director"
            and runtime["task_kind"] == task["task_kind"] and fingerprint(runtime) == spec["runtime_contract_version"]
            and "plan_propose" in runtime.get("mcp_requested_tools", []),
            "VIEW_DENIED", "The frozen role and skill do not permit planning proposals")
    assigned = repo.one("selections", {"work_id": task["work_id"], "selection_kind": f"PLAN_TASK:{task_id}:{generation}"})
    require(assigned is not None, "VIEW_DENIED", "Planning nodes must be assigned to this task")
    binding = kernel.artifact(repo, assigned["artifact_ref"], task["work_id"], kind="PLAN_TASK_BINDING")["payload"]
    if require_current:
        check_task_dependencies(repo, task_id, generation)
        validate_task_plan_binding(repo, kernel, task, spec)
        _current_anchor(kernel, repo, task, spec["world_ref"], binding["plan_ref"], ctx)
    return task, grant, spec, ctx, binding


def _current_anchor(kernel, repo, task, wr, ref, context):
    work = wr["work_id"]
    require(kernel.head(repo, work, wr["branch_id"]) == wr, "HEAD_CONFLICT", "The frozen world has advanced")
    plan = kernel.artifact(repo, ref, work, kind="PLAN_REVISION")["payload"]
    selected = repo.one("selections", {"work_id": work, "selection_kind": PlanningService.selection_key(plan["volume_id"])})
    manifest = repo.one("selections", {"work_id": work, "selection_kind": "PLAN_MANIFEST:" + plan["volume_id"]})
    require(plan["input_world_ref"] == wr and selected and selected["artifact_ref"] == ref
            and manifest and manifest["artifact_ref"] == context["read_cut"]["knowledge_cut"]["plan_manifest_ref"],
            "STALE_PLAN", "Planning anchor or assigned PLAN manifest is stale")
    return plan


def _decision_id(candidate_ref):
    return str(uuid5(UUID(candidate_ref["artifact_revision_id"]), "creative-plan-decision"))


def load_plan_candidate(kernel, repo, task, plan_binding, candidate_ref, *, require_current=True):
    row = kernel.artifact(repo, candidate_ref, task["work_id"], kind="CREATIVE_PLAN_CANDIDATE")
    record = row["payload"]
    scope = record["scope"]
    require(scope["task_id"] == task["task_id"] and scope["generation"] == task["generation"]
            and scope["world_ref"]["branch_id"] == task["branch_id"] and record["plan_anchor"] == plan_binding["plan_ref"]
            and row["created_by"] == task["principal_id"] and record["candidate"]["created_by"]["grant_id"] == task["grant_id"]
            and fingerprint(record) == candidate_ref["sha256"], "VIEW_DENIED", "Candidate is outside this planning assignment")
    if require_current:
        ctx = repo.one("contexts", {"context_bundle_id": task["context_bundle_id"]})
        _current_anchor(kernel, repo, task, scope["world_ref"], record["plan_anchor"], ctx)
        terminal = repo.one("artifacts", {"work_id": task["work_id"], "artifact_revision_id": _decision_id(candidate_ref)})
        require(terminal is None, "PLAN_SELECTION_CONFLICT", "Candidate already has a terminal decision")
    return record


def _option_id(candidate_id, node_id, variant):
    return str(uuid5(UUID(candidate_id), node_id + ":" + variant))


def option_node(plan, candidate_id, option_id):
    for node in plan["nodes"]:
        for variant in ("pursue", "fallback"):
            if _option_id(candidate_id, node["node_id"], variant) == option_id:
                return node, variant
    require(False, "INVALID_ARGUMENT", "Option does not resolve to an anchored node")


def _quota(kernel, repo, task, spec, volume, quota):
    validate_contract("Quota", quota, "harness")
    budget = repo.one("budgets", {"budget_id": quota["budget_id"], "work_id": task["work_id"]})
    enrolled = repo.one("supervisor_jobs", {"task_id": task["task_id"]})
    require(budget and (not enrolled or quota["budget_id"] == enrolled["config"]["dispatch"]["budget_id"]),
            "VIEW_DENIED", "The named budget is unavailable to this task")
    deadline = datetime.fromisoformat(quota["deadline"].replace("Z", "+00:00"))
    require(repo.now() < deadline <= task["deadline"], "DEADLINE_EXCEEDED", "Planning must fit the live task deadline")
    require(quota["currency"] == spec["budget"]["currency"] == budget["currency"]
            and Decimal(quota["max_amount"]) <= min(budget["limit_amount"], Decimal(spec["budget"]["max_amount"]))
            and quota["max_tokens"] <= spec["budget"]["token_budget"]
            and quota["max_attempts"] <= volume["budget"]["max_semantic_calls"],
            "BUDGET_EXCEEDED", "Requested planning allowance exceeds the task, volume or NF budget")


def _put(kernel, repo, principal, work, kind, payload, *, revision_id=None):
    ref = kernel._blob(payload)
    if revision_id:
        ref["artifact_revision_id"] = revision_id
    kernel._register_artifact(repo, ref, work, payload, kind, principal.principal_id)
    return ref


def _options(plan, allowed, ordered, trigger, count, candidate_id, plan_ref):
    nodes = {n["node_id"]: n for n in plan["nodes"]}
    protected = set(plan["published_prefix_node_ids"])
    require(set(ordered) <= allowed and all(n in nodes and n not in protected and nodes[n]["status"] not in
            {"REALIZED", "ABANDONED", "BLOCKED", "REVIEW_REQUIRED"} for n in ordered),
            "VIEW_DENIED", "Select only assigned, open plan nodes")
    variants = [(n, "pursue") for n in ordered[:1] if trigger == "ORDINARY"]
    if trigger != "ORDINARY":
        require(len(ordered) <= count, "CAPACITY_LIMIT", "Requested paths exceed the volume branch limit")
        variants = [(n, "pursue") for n in ordered]
        if len(variants) == 1 and count > 1:
            variants.append((ordered[0], "fallback"))
    result = []
    for node_id, variant in variants:
        node = nodes[node_id]
        downstream = [nodes[e["target_node_id"]]["goal"] for e in plan["dependencies"]
                      if e["source_node_id"] == node_id and e["target_node_id"] in allowed]
        goal = node["goal"] if variant == "pursue" else node["failure_exit"]
        if variant == "pursue":
            consequences = ["On success: " + g for g in (downstream or [node["goal"]])]
            consequences.append("If the goal fails: " + node["failure_exit"])
        else:
            consequences = ["If the original goal fails: " + node["failure_exit"]]
        risks = list(node["risk_notes"])
        reason = ("Continue this assigned goal under its existing prerequisites." if variant == "pursue" else
                  "Use the assigned node's failure exit if its goal cannot be met.")
        if trigger == "MOTIVE_CONFLICT":
            risks.extend("Competing goal may be deferred: " + nodes[n]["goal"] for n in ordered if n != node_id)
        elif trigger == "IRREVERSIBLE":
            risks.append("The published prefix and all realized outcomes constrain later choices.")
        if node["hard_preconditions"]:
            reason += " Its recorded hard preconditions require native validation at adoption."
        result.append({"option_id": _option_id(candidate_id, node_id, variant), "goal": goal[:512],
                       "premises": [plan_ref, plan["volume_contract_ref"]],
                       "expected_consequences": [g[:512] for g in dict.fromkeys(consequences)][:64],
                       "risks": [r[:512] for r in risks][:64], "disconfirming_evidence": [],
                       "selection_reason": reason})
    return result


def _native_plan(parent, plan_ref, candidate_id, selected, alternatives):
    plan = deepcopy(parent)
    node, variant = option_node(plan, candidate_id, selected["option_id"])
    focus = node["node_id"]
    # The native PLAN manifest distinguishes READY from DRAFT. Deferring an
    # independent competing path changes that read without cancelling it.
    prerequisites = {focus}
    while True:
        parents = {e["source_node_id"] for e in plan["dependencies"]
                   if e["kind"] == "HARD_PREREQUISITE" and e["target_node_id"] in prerequisites}
        if parents <= prerequisites:
            break
        prerequisites.update(parents)
    competing = {_option_id(candidate_id, n["node_id"], "pursue"): n for n in plan["nodes"]}
    for option in alternatives:
        other = competing.get(option["option_id"])
        if other and other["node_id"] not in prerequisites and other["status"] == "VIABLE":
            other["status"] = "PROPOSED"
    # The rolling window uses adopted order for equal story times. Promote
    # the focus only past another assigned alternative, preserving immutable
    # slots and every already ordered hard prerequisite in the full graph.
    positions = {n["node_id"]: i for i, n in enumerate(plan["nodes"])}
    current = positions[focus]
    hard = [(e["source_node_id"], e["target_node_id"]) for e in plan["dependencies"]
            if e["kind"] == "HARD_PREREQUISITE"]
    for option in alternatives:
        other = competing.get(option["option_id"])
        if not other or other["node_id"] in prerequisites or other["target_time"] != node["target_time"]:
            continue
        earlier = positions[other["node_id"]]
        if earlier >= current:
            continue
        new_pos = {**positions, focus: earlier, other["node_id"]: current}
        if all(positions[a] >= positions[b] or new_pos[a] < new_pos[b] for a, b in hard):
            plan["nodes"][earlier], plan["nodes"][current] = node, other
            break
    if variant == "fallback":
        node.update(goal=node["failure_exit"], status="PROPOSED")
    plan.update(plan_revision_id=str(uuid4()), prior_plan_ref=plan_ref,
                decision={"reason": selected["selection_reason"], "considered_options": [
                    {"option_id": option["option_id"], "summary": option["goal"], "reason": option["selection_reason"],
                     "disposition": "SELECTED" if option == selected else "DEFERRED"} for option in alternatives]})
    return plan


class CreativePlanningService:
    def __init__(self, kernel):
        self.kernel = kernel

    def propose(self, principal, task_id, generation, *, plan_ref, node_ids, trigger, quota, idempotency_key,
                horizon_scenes=1, worker_id=None):
        PlanningService._key(idempotency_key)
        validate_contract("ArtifactRef", plan_ref)
        require(trigger in ("ORDINARY", "MOTIVE_CONFLICT", "IRREVERSIBLE")
                and type(horizon_scenes) is int and 1 <= horizon_scenes <= 32
                and isinstance(node_ids, list) and 1 <= len(node_ids) <= 8,
                "INVALID_ARGUMENT", "Invalid planning trigger, horizon or number of nodes")
        for node in node_ids:
            validate_contract("UUID", node)
        require(len(set(node_ids)) == len(node_ids), "INVALID_ARGUMENT", "Repeated planning node")
        request = {"task_id": task_id, "generation": generation, "plan_ref": plan_ref, "node_ids": node_ids,
                   "trigger": trigger, "horizon_scenes": horizon_scenes, "quota": quota}
        with self.kernel.objects.lock(), self.kernel.db.uow() as repo:
            task, grant, spec, ctx, binding = planning_binding(self.kernel, repo, principal, task_id, generation)
            require(task["state"] == "RUNNING" and (worker_id is None or worker_id == task["lease_owner"]),
                    "VIEW_DENIED", "A current planning lease is required")
            wr = spec["world_ref"]
            require(binding["plan_ref"] == plan_ref, "VIEW_DENIED", "Plan is not assigned to this task")
            repo.one("branches", {"branch_id": task["branch_id"]}, lock="update")
            self.kernel.tasks.guard_task(repo.conn, task_id, generation, worker_id)
            old = self.kernel._prior(repo, principal, task["work_id"], "CREATIVE_PLAN_PROPOSE", idempotency_key, request)
            if old:
                load_plan_candidate(self.kernel, repo, task, binding, old["candidate_ref"])
                return old
            plan = _current_anchor(self.kernel, repo, task, wr, plan_ref, ctx)
            volume = self.kernel.artifact(repo, plan["volume_contract_ref"], task["work_id"], kind="VOLUME_CONTRACT")["payload"]
            _quota(self.kernel, repo, task, spec, volume, quota)
            require(volume["budget"]["max_candidate_branches"] >= 1, "CAPACITY_LIMIT", "Volume has no candidate allowance")
            candidate_id = str(uuid4())
            options = _options(plan, set(binding["node_ids"]), node_ids, trigger,
                               min(8, volume["budget"]["max_candidate_branches"]), candidate_id, plan_ref)
            record = {"schema_version": "1.0", "kind": "PLAN_CANDIDATE", "scope": {"world_ref": wr,
                      "task_id": task_id, "generation": generation}, "plan_anchor": plan_ref, "alternatives": options,
                      "assumptions": ["These alternatives are conditional on the frozen planning cut.",
                                      "Assigned goals express planning intentions. Actors may choose differently."],
                      "protected_commitments": ([plan["volume_contract_ref"]] if volume["protected_commitments"] else [])
                                              + ([plan_ref] if plan["published_prefix_node_ids"] else []),
                      "horizon_scenes": horizon_scenes, "quota": quota, "status": "HYPOTHESIS_ONLY"}
            record["candidate"] = {"candidate_id": candidate_id, "revision": 1, "parent_ref": None,
                                   "content_sha256": fingerprint(record), "created_by": {"grant_id": principal.grant_id,
                                   "principal_id": principal.principal_id, "permission_epoch": grant["epoch"]}}
            validate_contract("PlanCandidate", record, "harness")
            candidate_ref = _put(self.kernel, repo, principal, task["work_id"], "CREATIVE_PLAN_CANDIDATE", record)
            plans = PlanningService(self.kernel.in_transaction(repo))
            native_refs = {o["option_id"]: plans.save_plan(principal, _native_plan(plan, plan_ref, candidate_id, o, options),
                           "creative-option:" + o["option_id"]) for o in options}
            result = {"candidate_ref": candidate_ref, "candidate": record, "option_plan_refs": native_refs}
            _put(self.kernel, repo, principal, task["work_id"], "CREATIVE_OPTION_PLANS",
                 {"scope": record["scope"], "candidate_ref": candidate_ref, "option_plan_refs": native_refs},
                 revision_id=str(uuid5(UUID(candidate_ref["artifact_revision_id"]), "nf-plan-options")))
            self.kernel._receipt(repo, principal, wr, "CREATIVE_PLAN_PROPOSE", idempotency_key, request, result)
            return result

    def decide(self, principal, task_id, generation, *, candidate_ref, option_id, idempotency_key,
               rolling_review_ref=None):
        PlanningService._key(idempotency_key)
        validate_contract("ArtifactRef", candidate_ref)
        if option_id is not None:
            validate_contract("UUID", option_id)
        if rolling_review_ref is not None:
            validate_contract("ArtifactRef", rolling_review_ref)
        request = {"task_id": task_id, "generation": generation, "candidate_ref": candidate_ref,
                   "option_id": option_id, "rolling_review_ref": rolling_review_ref}
        with self.kernel.objects.lock(), self.kernel.db.uow() as repo:
            task = repo.one("tasks", {"task_id": task_id})
            require(task is not None, "VIEW_DENIED", "Unknown planning assignment")
            work = task["work_id"]
            repo.one("works", {"work_id": work}, lock="update")
            owner = Principal(task["grant_id"], task["principal_id"])
            original_grant = self.kernel.authorize(repo, owner, work, "PLAN_READ", task["branch_id"])
            capability = "ARTIFACT_WRITE" if principal == owner and option_id is None else "APPROVE"
            approver = self.kernel.authorize(repo, principal, work, capability, task["branch_id"])
            require(option_id is None or principal.principal_id != owner.principal_id, "VIEW_DENIED",
                    "Independent plan approval is required")
            row = self.kernel.artifact(repo, candidate_ref, work, kind="CREATIVE_PLAN_CANDIDATE")
            require(task["generation"] == generation
                    and row["payload"]["candidate"]["created_by"]["permission_epoch"] == original_grant["epoch"]
                    and row["payload"]["scope"]["task_id"] == task_id and row["payload"]["scope"]["generation"] == generation
                    and row["payload"]["scope"]["world_ref"]["branch_id"] == task["branch_id"],
                    "VIEW_DENIED", "Unavailable candidate scope")
            check_profile(approver, "PLAN")
            old = self.kernel._prior(repo, principal, work, "CREATIVE_PLAN_DECIDE", idempotency_key, request)
            if old:
                return old
            terminal = repo.one("artifacts", {"work_id": work, "artifact_revision_id": _decision_id(candidate_ref)})
            require(terminal is None, "PLAN_SELECTION_CONFLICT", "Candidate already has a terminal decision")
            task, _, spec, _, binding = planning_binding(self.kernel, repo, owner, task_id, generation)
            record = load_plan_candidate(self.kernel, repo, task, binding, candidate_ref)
            wr = spec["world_ref"]
            plan_ref, adoption = None, None
            if option_id is not None:
                require(option_id in [o["option_id"] for o in record["alternatives"]],
                        "INVALID_ARGUMENT", "Option is not part of this candidate")
                options = repo.one("artifacts", {"work_id": work, "kind": "CREATIVE_OPTION_PLANS",
                       "artifact_revision_id": str(uuid5(UUID(candidate_ref["artifact_revision_id"]), "nf-plan-options"))})
                require(options and fingerprint(options["payload"]) == options["sha256"], "INVALID_EVIDENCE",
                        "Native plan revisions are unavailable")
                option_set = options["payload"]
                require(option_set["candidate_ref"] == candidate_ref and option_set["scope"] == record["scope"],
                        "INVALID_EVIDENCE", "Native alternatives do not match the bound planning scope")
                plan_ref = option_set["option_plan_refs"][option_id]
                plans = PlanningService(self.kernel.in_transaction(repo))
                adoption = plans.adopt_plan(principal, wr, plan_ref, record["plan_anchor"],
                                            "creative-adopt:" + option_id, rolling_review_ref=rolling_review_ref)
            result = {"status": "ADOPTED" if adoption else "DECLINED", "plan_ref": plan_ref,
                      "candidate_ref": candidate_ref, "option_id": option_id, "adoption": adoption}
            _put(self.kernel, repo, principal, work, "CREATIVE_PLAN_DECISION", {"scope": record["scope"], **result},
                 revision_id=_decision_id(candidate_ref))
            self.kernel._receipt(repo, principal, wr, "CREATIVE_PLAN_DECIDE", idempotency_key, request, result)
            return result
