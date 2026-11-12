"""Bounded hypothetical rule replay using individually authorized actor cuts.

Actor perspectives are interpreted separately. Only the controller compares
the resulting positions with protected plan conditions. No actor sees the
plan, another actor's evidence, or a tentative world event.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
import json
from time import monotonic
from uuid import UUID, uuid5

from sqlalchemy import select, text

from novel_factory.application.authorization import Principal
from novel_factory.application.creative_planning import load_plan_candidate, option_node, planning_binding
from novel_factory.application.kernel import require
from novel_factory.application.queries import WorldQueryService
from novel_factory.application.semantic_dependencies import check_task_dependencies
from novel_factory.contracts.registry import validate_contract
from novel_factory.domain.canonical import canonical_bytes, fingerprint
from novel_factory.domain.temporal import point_key
from novel_factory.domain.world import ALIASES
from novel_factory.infrastructure.creative_config import CreativeHarnessConfig
from novel_factory.infrastructure.database import tables


def _actor_scope(kernel, repo, wr, story_time, selector):
    task = repo.one("tasks", {"task_id": selector["task_id"]}, lock="share")
    require(task and task["work_id"] == wr["work_id"] and task["branch_id"] == wr["branch_id"],
            "VIEW_DENIED", "Actor cut is outside this simulation")
    owner = Principal(task["grant_id"], task["principal_id"])
    _, grant, spec = WorldQueryService(kernel)._task(repo, owner, task["task_id"], selector["generation"])
    require(spec["world_ref"] == wr and spec["story_time"] == story_time and grant["actor_id"]
            and spec["role_id"] == grant["role_id"] == "character_actor" and "BELIEF" in grant["planes"],
            "VIEW_DENIED", "Actor cut is outside this simulation")
    context = repo.one("contexts", {"context_bundle_id": task["context_bundle_id"], **selector})
    require(context and context["principal_id"] == owner.principal_id and context["grant_id"] == owner.grant_id,
            "VIEW_DENIED", "Actor task has no authorized frozen context")
    cut, bundle = context["read_cut"], context["payload"]
    require(cut["permission_epoch"] == grant["epoch"] and cut["knowledge_cut"]["plane"] == "BELIEF"
            and cut["world_ref"] == bundle["world_ref"] == wr
            and bundle["task_id"] == task["task_id"] and bundle["generation"] == task["generation"]
            and bundle["story_time"] == story_time and bundle["role_id"] == grant["role_id"]
            and bundle["policy_version"] == cut["policy_version"] == spec["policy_version"]
            and cut["knowledge_cut"]["actor_id"] == bundle["actor_id"] == grant["actor_id"]
            and point_key(cut["knowledge_cut"]["known_at"]) <= point_key(story_time) <= point_key(grant["max_story_time"])
            and bundle["dependency_fingerprint"] == task["dependency_fingerprint"],
            "VIEW_DENIED", "Actor knowledge is outside its fixed authorization")
    check_task_dependencies(repo, task["task_id"], task["generation"])
    compiled = kernel.artifact(repo, context["artifact_ref"], wr["work_id"], kind="COMPILED_CONTEXT")["payload"]
    require(fingerprint(compiled) == context["artifact_ref"]["sha256"] and compiled["bundle"] == bundle
            and compiled["read_cut"] == cut, "TASK_REBIND_REQUIRED", "Actor context integrity failed")
    artifacts = tables["artifacts"]
    inputs = repo.conn.execute(select(artifacts.c.payload).where(
        artifacts.c.work_id == wr["work_id"], artifacts.c.kind == "AGENT_INPUT",
        artifacts.c.artifact_revision_id.in_([r["artifact_revision_id"] for r in spec["input_artifacts"]]))).scalars().all()
    require(len(inputs) == 1 and "creative_harness" in inputs[0]
            and CreativeHarnessConfig.parse(inputs[0]["creative_harness"]).enabled
            and inputs[0]["runtime_sha256"] == spec["runtime_contract_version"],
            "VIEW_DENIED", "Actor was not enrolled in Creative Harness")
    return grant["actor_id"], context["artifact_ref"], bundle["facts"]


def _perspective(facts):
    """Read only NF's structured statements in one actor's BELIEF bundle."""
    positions = {}
    for fact in facts:
        require(fact["status"] == "BELIEF", "VIEW_DENIED", "Unexpected fact in actor context")
        try:
            statement = json.loads(fact["statement"])
        except (ValueError, RecursionError):
            continue
        if isinstance(statement, dict) and {"subject_id", "predicate", "value_status", "value"} <= statement.keys():
            predicate = statement["predicate"]
            positions[(statement["subject_id"], ALIASES.get(predicate, predicate))] = (
                statement["value_status"], statement["value"])
    return positions


def _condition(position, condition):
    kind = condition["kind"]
    if kind == "STATE_EQUALS":
        predicate = condition["predicate"]
        value = position.get((condition["subject_id"], ALIASES.get(predicate, predicate)))
        if value and value[0] in {"NONE", "NOT_APPLICABLE"}:
            return False
        return value[1] == condition["expected_value"] if value and value[0] == "VALUE" else None
    if kind == "ENTITY_ACTIVE":
        value = position.get((condition["subject_id"], "lifecycle"))
        return value[1] == {"kind": "STRING", "value": "ACTIVE"} if value and value[0] == "VALUE" else None
    if kind == "RESOURCE_AT_LEAST":
        predicate = "resource:" + condition["resource_id"] + ":" + condition["unit"]
        value = position.get((condition["account_id"], predicate))
        if value and value[0] == "VALUE":
            amount = value[1].get("quantity", value[1].get("value")) if isinstance(value[1], dict) else value[1]
            try:
                return Decimal(str(amount)) >= Decimal(condition["quantity"])
            except (InvalidOperation, ValueError):
                pass
    # An actor's inability to prove a world event never proves it did not occur.
    return None


def _comparison(node, perspectives):
    checks = node["hard_preconditions"]
    if checks:
        positions = [[_condition(view, check) for check in checks] for view in perspectives]
        if any(True in column and False in column for column in zip(*positions)):
            return "DISAGREEMENT"
        if any(False in row for row in positions):
            return "CONFLICT"
        if all(all(v is True for v in row) for row in positions):
            return "CONSISTENT"
        return "UNRESOLVED"
    subjects = set(node["subject_ids"])
    evidence = {}
    common = None
    for view in perspectives:
        observed = set()
        for (subject, predicate), value in view.items():
            if subject in subjects and value[0] == "VALUE":
                key = (subject, predicate)
                if key in evidence and evidence[key] != value:
                    return "DISAGREEMENT"
                evidence[key] = value
                observed.add(key)
        common = observed if common is None else common & observed
    return "CONSISTENT" if common else "UNRESOLVED"


_RESPONSES = {
    "pursue": {
        "DISAGREEMENT": "Incompatible accounts would lead participants to contest the premise before acting.",
        "CONFLICT": "An actor's known circumstances would obstruct pursuit of this goal.",
        "CONSISTENT": "Participants could pursue the goal under the conditions they believe hold.",
        "UNRESOLVED": "The available perspectives do not establish that the goal is feasible.",
    },
    "fallback": {
        "DISAGREEMENT": "The fallback permits verification before participants act on conflicting accounts.",
        "CONFLICT": "The fallback avoids relying on a premise an actor disputes.",
        "CONSISTENT": "The fallback remains possible, though the shared account supplies no reason to take it.",
        "UNRESOLVED": "The fallback preserves an open choice while necessary information is missing.",
    },
}


def _transition(plan, node, variant, reaction, traversed, fulfilled, permitted, seed):
    """Choose the next authored dependency without touching canonical nodes."""
    fallback = variant == "fallback" or reaction != "CONSISTENT"
    statuses = {n["node_id"]: n["status"] for n in plan["nodes"]}
    protected = set(plan["published_prefix_node_ids"]) | {k for k, v in statuses.items() if v == "REALIZED"}
    eligible = {"ALTERNATIVE"} if fallback else {"HARD_PREREQUISITE", "CAUSAL_RISK", "LITERARY_PROMISE"}
    edges = [e for e in plan["dependencies"] if e["source_node_id"] == node["node_id"] and e["kind"] in eligible]
    edges.sort(key=lambda e: uuid5(UUID(node["node_id"]), str(seed) + ":" + e["target_node_id"]).int)
    for edge in edges:
        target = edge["target_node_id"]
        if target in protected or target in traversed or target not in permitted or statuses[target] != "VIABLE":
            continue
        prerequisites = {e["source_node_id"] for e in plan["dependencies"]
                         if e["target_node_id"] == target and e["kind"] == "HARD_PREREQUISITE"}
        if not prerequisites <= fulfilled | protected:
            continue
        return target, edge["kind"]
    if fallback:
        return None, "FAILURE_EXIT"
    return None, "RULE_STOP"


def _rollout(candidate, plan, actors, request, cutoff, permitted):
    quota = candidate["quota"]
    consumed = attempts = 0
    stops, outcomes = [], []
    nodes = {n["node_id"]: n for n in plan["nodes"]}
    horizon = min(request["max_steps_per_branch"], candidate["horizon_scenes"])
    for index, option in enumerate(candidate["alternatives"]):
        identifier = option["option_id"]
        node, variant = option_node(plan, candidate["candidate"]["candidate_id"], identifier)
        initial_variant = variant
        cause = "COMPLETED"
        if index >= request["max_branches"]:
            cause = "BRANCH_LIMIT"
        elif monotonic() >= cutoff:
            cause = "DEADLINE"
        elif attempts >= quota["max_attempts"]:
            cause = "ATTEMPT_LIMIT"
        path, traversed, fulfilled = [], set(), set()
        reaction = "UNRESOLVED"
        action = "PURSUE" if variant == "pursue" else "FALLBACK"
        if cause == "COMPLETED":
            attempts += 1
        while cause == "COMPLETED":
            if monotonic() >= cutoff:
                cause = "DEADLINE"
                break
            if len(path) >= horizon:
                cause = "STEP_LIMIT"
                break
            if action == "FAILURE_EXIT":
                size = len(node["failure_exit"].encode("utf-8"))
                if consumed + size > quota["max_tokens"]:
                    cause = "TOKEN_BUDGET"
                    break
                consumed += size
                path.append({"node_id": node["node_id"], "action": action, "consequence": node["failure_exit"]})
                break
            views = []
            for _, facts in actors:
                if monotonic() >= cutoff:
                    cause = "DEADLINE"
                    break
                # Count local material as a conservative UTF-8 upper bound.
                # No model attempt or invoice is generated by this rule run.
                units = len(canonical_bytes(facts)) + len(canonical_bytes(node["hard_preconditions"]))
                if consumed + units > quota["max_tokens"]:
                    cause = "TOKEN_BUDGET"
                    break
                consumed += units
                views.append(_perspective(facts))
            if cause != "COMPLETED":
                break
            reaction = _comparison(node, views)
            path.append({"node_id": node["node_id"], "action": action, "reaction": reaction})
            traversed.add(node["node_id"])
            if reaction == "CONSISTENT" and variant == "pursue":
                fulfilled.add(node["node_id"])
            target, action = _transition(plan, node, variant, reaction, traversed, fulfilled, permitted, request["seed"])
            if target is not None:
                node, variant = nodes[target], "pursue"
            elif action != "FAILURE_EXIT":
                break
        if cause == "COMPLETED" and reaction == "UNRESOLVED":
            cause = "INSUFFICIENT_EVIDENCE"
        stops.append({"option_id": identifier, "reason": cause, "steps": len(path), "reaction": reaction, "path": path})
        formal = {"COMPLETED": "RULE_STOP", "INSUFFICIENT_EVIDENCE": "INSUFFICIENT_EVIDENCE",
                  "STEP_LIMIT": "HORIZON"}.get(cause, "QUOTA")
        description = _RESPONSES[initial_variant][reaction]
        if path:
            ending = path[-1]
            result = ending.get("consequence", nodes[ending["node_id"]]["goal"])
            description += " Conditional endpoint: " + result
        outcomes.append({"option_id": identifier, "conditional_result": description[:512],
                         "supporting_refs": [{"kind": "ARTIFACT", "artifact_ref": candidate["plan_anchor"]}],
                         "uncertainties": ["The rule compares beliefs only; it does not predict an actor's final choice."],
                         "stop_reason": formal})
    usage = {"local_input_units": consumed, "rollout_attempts": attempts, "model_tokens": 0, "model_attempts": 0,
             "amount": "0", "currency": quota["currency"]}
    return outcomes, stops, usage


class CreativeSimulationService:
    def __init__(self, kernel):
        self.kernel = kernel

    def _read(self, repo, controller, request, *, writing=False):
        planner = repo.one("tasks", {"task_id": request["task_id"]})
        require(planner, "VIEW_DENIED", "Simulation task is unavailable")
        grant = self.kernel.authorize(repo, controller, planner["work_id"], "ADMIN", planner["branch_id"])
        require(grant["role_id"] == "control_operator", "VIEW_DENIED", "Simulation requires a control operator")
        if writing:
            repo.one("branches", {"branch_id": planner["branch_id"]}, lock="share")
            repo.one("tasks", {"task_id": planner["task_id"]}, lock="update")
        owner = Principal(planner["grant_id"], planner["principal_id"])
        task, _, spec, context, bound = planning_binding(
            self.kernel, repo, owner, planner["task_id"], request["generation"])
        candidate = load_plan_candidate(self.kernel, repo, task, bound, request["candidate_ref"])
        plan = self.kernel.artifact(repo, candidate["plan_anchor"], task["work_id"], kind="PLAN_REVISION")["payload"]
        actors, refs = [], []
        for selector in sorted(request["actor_tasks"], key=lambda x: x["task_id"]):
            actor, ref, facts = _actor_scope(self.kernel, repo, spec["world_ref"], spec["story_time"], selector)
            actors.append((actor, facts))
            refs.append(ref)
        require(len(set(a[0] for a in actors)) == len(actors), "INVALID_ARGUMENT",
                "Each participant must identify a distinct character")
        return candidate, plan, actors, context["artifact_ref"], refs, set(bound["node_ids"])

    def run(self, controller, *, task_id, generation, candidate_ref, actor_tasks, max_branches,
            max_steps_per_branch, seed, idempotency_key):
        validate_contract("UUID", task_id)
        validate_contract("ArtifactRef", candidate_ref)
        require(type(generation) is int and generation >= 0 and isinstance(actor_tasks, list)
                and 1 <= len(actor_tasks) <= 16 and type(max_branches) is int and 1 <= max_branches <= 32
                and type(max_steps_per_branch) is int and 1 <= max_steps_per_branch <= 128
                and type(seed) is int and 0 <= seed <= 2147483647
                and isinstance(idempotency_key, str) and 1 <= len(idempotency_key) <= 200,
                "INVALID_ARGUMENT", "Invalid bounded simulation request")
        for row in actor_tasks:
            require(isinstance(row, dict) and set(row) == {"task_id", "generation"}
                    and type(row["generation"]) is int and row["generation"] >= 0,
                    "INVALID_ARGUMENT", "Invalid actor task selector")
            validate_contract("UUID", row["task_id"])
        request = {"task_id": task_id, "generation": generation, "candidate_ref": candidate_ref,
                   "actor_tasks": sorted(actor_tasks, key=lambda a: a["task_id"]),
                   "max_branches": max_branches, "max_steps_per_branch": max_steps_per_branch, "seed": seed}
        command = "CREATIVE_SIMULATION_RUN"
        with self.kernel.db.uow(write=False) as repo:
            candidate, plan, actors, cut_ref, actor_refs, permitted = self._read(repo, controller, request)
            wr = candidate["scope"]["world_ref"]
            previous = self.kernel._prior(repo, controller, wr["work_id"], command, idempotency_key, request)
            if previous is not None:
                return previous
            seconds = (datetime.fromisoformat(candidate["quota"]["deadline"]) - repo.now()).total_seconds()
            cutoff = monotonic() + seconds

        # Serialize only this frozen candidate while computing the bounded
        # local rules. No world, grant, branch or task row is locked during
        # the calculation. A crashed process automatically loses this lock.
        frozen_id = str(uuid5(UUID(candidate_ref["artifact_revision_id"]), "creative-simulation-run"))
        with self.kernel.objects.lock(), self.kernel.db.uow() as repo:
            repo.conn.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"), {"key": frozen_id})
            frozen = repo.one("artifacts", {"work_id": wr["work_id"], "artifact_revision_id": frozen_id,
                                            "kind": "CREATIVE_SIMULATION_RUN"})
            if frozen:
                require(frozen["payload"]["request_sha256"] == fingerprint(request), "IDEMPOTENCY_MISMATCH",
                        "This candidate has already used its frozen simulation allowance")
            else:
                outcomes, stops, usage = _rollout(candidate, plan, actors, request, cutoff, permitted)
                report = {"schema_version": "1.0", "kind": "SIMULATION_REPORT", "scope": candidate["scope"],
                          "plan_candidate_ref": candidate_ref, "input_cut": cut_ref, "assumptions": candidate["assumptions"],
                          "method": "RULE_REPLAY", "max_branches": max_branches, "max_steps_per_branch": max_steps_per_branch,
                          "seed": seed, "quota": candidate["quota"], "outcomes": outcomes, "accepted_world_events": 0,
                          "status": "HYPOTHESIS_ONLY"}
                validate_contract("SimulationReport", report, "harness")
            current, _, _, live_cut, live_actors, live_nodes = self._read(repo, controller, request, writing=True)
            require(current == candidate and live_cut == cut_ref and live_actors == actor_refs and live_nodes == permitted,
                    "TASK_REBIND_REQUIRED", "Simulation sources have changed")
            previous = self.kernel._prior(repo, controller, wr["work_id"], command, idempotency_key, request)
            if previous is not None:
                return previous
            if frozen:
                run = frozen["payload"]
                require(fingerprint(run) == frozen["sha256"] and run["scope"] == candidate["scope"]
                        and cut_ref == run["input_cut"] and actor_refs == [a["cut_ref"] for a in run["participants"]],
                        "TASK_REBIND_REQUIRED", "Frozen simulation no longer matches the authorized actors")
                ref, usage, stops = run["report_ref"], run["usage"], run["stops"]
                report = self.kernel.artifact(repo, ref, wr["work_id"], kind="CREATIVE_SIMULATION_REPORT")["payload"]
            else:
                ref = self.kernel._blob(report)
                self.kernel._register_artifact(repo, ref, wr["work_id"], report, "CREATIVE_SIMULATION_REPORT",
                                               controller.principal_id)
                run = {"scope": candidate["scope"], "input_cut": cut_ref, "request_sha256": fingerprint(request),
                       "participants": [{**task, "cut_ref": cut} for task, cut in zip(request["actor_tasks"], actor_refs, strict=True)],
                       "report_ref": ref, "usage": usage, "stops": stops}
                run_ref = self.kernel._blob(run)
                run_ref["artifact_revision_id"] = frozen_id
                self.kernel._register_artifact(repo, run_ref, wr["work_id"], run, "CREATIVE_SIMULATION_RUN", controller.principal_id)
            result = {"report_ref": ref, "report": report, "usage": usage, "stops": stops}
            self.kernel._receipt(repo, controller, wr, command, idempotency_key, request, result)
        return result
