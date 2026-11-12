"""Project administrator's scoped adoption of a Workshop policy and immutable route freezes."""
from agent_runtime.routing import RoutingError, resolve, validate_policy

from .kernel import require
from novel_factory.domain.canonical import fingerprint
from novel_factory.domain.errors import DomainError


POLICY_ROUTE = "@policy"


def _key(wr):
    return {"work_id": wr["work_id"], "selection_kind": "RUNTIME_POLICY:" + wr["branch_id"]}


def select(kernel, repo, wr, actor, adapters, *, override=None):
    pointer = repo.one("selections", _key(wr))
    require(pointer, "MODEL_UNAVAILABLE", "Activate a routing policy for this branch first")
    ref = pointer["artifact_ref"]
    policy = kernel.artifact(repo, ref, wr["work_id"], kind="RUNTIME_ROUTING_POLICY")["payload"]
    require(fingerprint(policy) == ref["sha256"], "OBJECT_CORRUPT", "Policy hash differs")
    try:
        decision = resolve(policy, actor, override=override)
    except RoutingError as exc:
        raise DomainError("INVALID_ARGUMENT", str(exc)) from None
    adapter = adapters.get(decision["profile_id"])
    require(adapter is not None, "MODEL_UNAVAILABLE", "Selected profile is not installed")
    require(adapter.configuration.get("isolation") == "STATELESS_TEXT_ONLY", "HOST_ISOLATION_UNVERIFIED",
            "A policy route requires a text-only model")
    return {**decision, "policy_ref": ref, "configuration_sha256": fingerprint(adapter.configuration),
            "organization": policy["installation_id"] + "/" + policy["org_id"]}


def activate(kernel, owner, wr, policy, adapters, *, expected_revision):
    try:
        validate_policy(policy)
    except RoutingError as exc:
        raise DomainError("INVALID_ARGUMENT", str(exc)) from None
    require(type(expected_revision) is int and expected_revision >= 0, "INVALID_ARGUMENT",
            "Expected revision must be nonnegative")
    with kernel.objects.lock(), kernel.db.uow() as repo:
        repo.one("works", {"work_id": wr["work_id"]}, lock="update")
        kernel.authorize(repo, owner, wr["work_id"], "ADMIN", wr["branch_id"])
        kernel.revision(repo, wr)
        require(all(wr[k] == policy[k] for k in ("work_id", "branch_id")), "VIEW_DENIED",
                "Export belongs to another work or branch")
        ref = kernel._blob(policy)
        key = _key(wr)
        current = repo.one("selections", key, lock="update")
        if current and current["artifact_ref"]["sha256"] == ref["sha256"]:
            return {"revision": current["epoch"], "policy_ref": current["artifact_ref"]}
        require((current["epoch"] if current else 0) == expected_revision and
                policy["revision"] > expected_revision, "HEAD_CONFLICT", "Stale routing policy revision")
        if current:
            prior = kernel.artifact(repo, current["artifact_ref"], wr["work_id"],
                                    kind="RUNTIME_ROUTING_POLICY")["payload"]
            require(all(prior[k] == policy[k] for k in ("installation_id", "org_id")), "VIEW_DENIED",
                    "Policy belongs to another installation or organization")
        require(all(r["profile"] in adapters for r in policy["rules"]), "MODEL_UNAVAILABLE",
                "Install every selected profile before activating this revision")
        kernel._register_artifact(repo, ref, wr["work_id"], policy, "RUNTIME_ROUTING_POLICY", owner.principal_id)
        values = {"artifact_ref": ref, "epoch": policy["revision"]}
        if current:
            repo.change("selections", key, values)
        else:
            repo.add("selections", {**key, **values})
        return {"revision": policy["revision"], "policy_ref": ref}


def preview(kernel, owner, wr, actor, adapters, *, override=None):
    with kernel.db.uow(write=False) as repo:
        kernel.authorize(repo, owner, wr["work_id"], "ADMIN", wr["branch_id"])
        kernel.revision(repo, wr)
        return select(kernel, repo, wr, actor, adapters, override=override)


def rebind(kernel, owner, wr, task_id, expected_generation, adapters, idempotency_key):
    """Fence an enrolled, unsent task and compile a fresh authorized context at its next claim."""
    from decimal import Decimal
    from sqlalchemy import text
    from .planning import PlanningService
    from .semantic_dependencies import check_task_dependencies
    from novel_factory.contracts.registry import validate_contract
    from novel_factory.orchestration.runtime_admission import scope_for

    PlanningService._key(idempotency_key)
    request = {"task_id": task_id, "generation": expected_generation, "work_id": wr["work_id"],
               "branch_id": wr["branch_id"]}
    with kernel.objects.lock(), kernel.db.uow() as repo:
        kernel.authorize(repo, owner, wr["work_id"], "ADMIN", wr["branch_id"])
        # Graph edits take the branch lock before the task row. A blocked rebind
        # copies its immutable predecessor edges under the same lock.
        repo.conn.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:scope,0))"),
                          {"scope": "nf-workflow:" + wr["branch_id"]})
        task = repo.one("tasks", {"task_id": task_id, "work_id": wr["work_id"], "branch_id": wr["branch_id"]},
                        lock="update")
        require(task, "VIEW_DENIED", "Task is outside the authorized project")
        prior = kernel._prior(repo, owner, wr["work_id"], "MODEL_ROUTE_REBIND", idempotency_key, request)
        if prior:
            return prior
        require(task["generation"] == expected_generation, "STALE_GENERATION", "Task generation changed")
        calls = repo.many("call_attempts", {"task_id": task_id})
        prior_unsent = set(repo.conn.scalars(text("""SELECT u.attempt_id::text FROM nf_runtime_unsent u
            JOIN nf_call_attempts c USING(attempt_id) WHERE c.task_id=:task"""), {"task": task_id}))
        calls = [c for c in calls if str(c["attempt_id"]) not in prior_unsent]
        require(all(c["state"] == "PREPARED" and c["generation"] == expected_generation for c in calls),
                "CALL_RECONCILIATION_REQUIRED", "A sent or terminal attempt requires explicit reconciliation")
        job = repo.one("supervisor_jobs", {"task_id": task_id}, lock="update")
        require(job and task["state"] in {"READY", "RUNNING", "BLOCKED"} and task["current_result_id"] is None
                and task["deadline"] > repo.now(), "TASK_REBIND_REQUIRED", "Task needs fresh enrollment")
        check_task_dependencies(repo, task_id, expected_generation)
        require(not repo.conn.scalar(text("""
            SELECT EXISTS(SELECT FROM nf_delegation_receipts WHERE task_id=:task)
        """), {"task": task_id}) and not repo.conn.scalar(kernel.tasks.plan_binding_exists(task_id, wr["work_id"]).select()),
                "TASK_REBIND_REQUIRED", "Obtain fresh generation-bound delegation or plan authorization")
        cfg = job["config"]
        old_ref = cfg["dispatch"]["input_ref"]
        old = kernel.artifact(repo, old_ref, wr["work_id"], kind="AGENT_INPUT")["payload"]
        require(not old.get("dynamic_assignment_ref") and not old.get("adaptive_instruction_ref"),
                "TASK_REBIND_REQUIRED", "Obtain a fresh generation-bound creative assignment")
        grant = repo.one("grants", {"grant_id": task["grant_id"]})
        require(grant["active"] and grant["epoch"] == cfg["grant_epoch"], "VIEW_DENIED", "Grant changed")
        spec = repo.one("task_inputs", {"task_id": task_id, "generation": expected_generation})["spec"]
        decision = select(kernel, repo, spec["world_ref"], {"task_id": task_id, "role_id": task["role_id"],
                          "task_kind": task["task_kind"], "instance_id": grant.get("actor_id")}, adapters)
        new = {**old, "adapter_id": decision["profile_id"], "model_config_sha256": decision["configuration_sha256"],
               "routing_decision": decision}
        validate_contract("AgentInput", new, "agent")
        ref = kernel._blob(new)
        kernel._register_artifact(repo, ref, wr["work_id"], new, "AGENT_INPUT", owner.principal_id)
        gen = expected_generation + 1
        for call in calls:
            kernel.tasks._settle_locked_task_call(repo.conn, call, "FAILED", Decimal(0), 0, None)
            repo.conn.execute(text("INSERT INTO nf_runtime_unsent(attempt_id,generation) VALUES (:id,:gen)"),
                              {"id": call["attempt_id"], "gen": expected_generation})
        replaced = {**spec, "context_bundle_id": None, "dependency_fingerprint": None,
                    "input_artifacts": [ref if item == old_ref else item for item in spec["input_artifacts"]]}
        state = "BLOCKED" if task["state"] == "BLOCKED" else "READY"
        kernel.tasks._save_input(repo.conn, task_id, gen, replaced, state)
        if state == "BLOCKED":
            params = {"task": task_id, "old": expected_generation, "new": gen}
            repo.conn.execute(text("""
                INSERT INTO nf_workflow_waits(task_id,generation,mode,grant_epoch)
                SELECT task_id,:new,mode,grant_epoch FROM nf_workflow_waits
                WHERE task_id=:task AND generation=:old AND woken_at IS NULL
            """), params)
            repo.conn.execute(text("""
                INSERT INTO nf_workflow_edges(task_id,generation,predecessor_id)
                SELECT task_id,:new,predecessor_id FROM nf_workflow_edges
                WHERE task_id=:task AND generation=:old
            """), params)
        repo.change("tasks", {"task_id": task_id}, {"state": state, "generation": gen,
                    "context_bundle_id": None, "dependency_fingerprint": None, "lease_owner": None,
                    "attempt_id": None, "lease_expiry": None, "updated_at": repo.now()})
        repo.conn.execute(text("DELETE FROM nf_dispatch_slots WHERE task_id=:task"), {"task": task_id})
        scope = scope_for(adapters[decision["profile_id"]], new, wr["work_id"])
        repo.change("supervisor_jobs", {"task_id": task_id}, {"config": {
            **cfg, "runtime_scope": scope, "dispatch": {**cfg["dispatch"], "input_ref": ref}}})
        kernel.tasks._emit(repo.conn, task_id, gen, state)
        result = {"task_id": task_id, "generation": gen, "old_input_ref": old_ref, "input_ref": ref,
                  "policy_revision": decision["revision"], "released_unsent": len(calls)}
        kernel._receipt(repo, owner, wr, "MODEL_ROUTE_REBIND", idempotency_key, request, result)
        return result
