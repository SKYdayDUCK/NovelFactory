"""Explicitly replace a returned, stopped fact reading under a new routing policy.

The source manuscript and every old attempt/ledger stay immutable. This operation
does no model I/O and does not claim the new model has approved the manuscript.
"""
from copy import deepcopy
from dataclasses import dataclass
from uuid import UUID, uuid5

from sqlalchemy import text

from novel_factory.contracts.registry import validate_contract

from . import model_routing
from . import production_pipeline as boards
from . import rolling_production as rolling
from .budget_limits import SharedBudgetService
from .candidate_delivery import CandidateDelivery
from .kernel import require
from .volume_successor import _selected


@dataclass(frozen=True)
class ReadingPolicyRecovery:
    work_id: str
    branch_id: str
    task_id: str
    expected_generation: int
    expected_board_sha256: str
    creative_profile: str
    review_profile: str
    token_reservation: int = 500000


def replace_stopped_reading(kernel, gateway, principal, request, *, apply=False):
    require(type(apply) is bool, "INVALID_ARGUMENT", "Explicit apply flag required")
    for value in (request.work_id, request.branch_id, request.task_id):
        validate_contract("UUID", value)
    require(type(request.expected_generation) is int and request.expected_generation >= 0
            and type(request.token_reservation) is int and 1 <= request.token_reservation <= 500000,
            "INVALID_ARGUMENT", "Invalid generation or reservation")
    command, intent = "P6_READING_POLICY_RECOVERY", vars(request)
    with kernel.objects.lock(), kernel.db.uow() as repo:
        repo.one("works", {"work_id": request.work_id}, lock="update")
        kernel.authorize(repo, principal, request.work_id, "ADMIN", request.branch_id)
        prior = kernel._prior(repo, principal, request.work_id, command, request.task_id, intent)
        if prior:
            return prior
        selected = _selected(repo, request.work_id, boards._key(request.branch_id))
        require(selected and selected["sha256"] == request.expected_board_sha256,
                "STALE_GENERATION", "Production board changed")
        board = boards._load(kernel, repo, request.work_id, request.branch_id)
        owner = boards._current_owner(kernel, repo, board)
        wr = board["world_ref"]
        repo.one("branches", {"work_id": request.work_id, "branch_id": request.branch_id}, lock="share")
        task = repo.one("tasks", {"task_id": request.task_id, "work_id": request.work_id,
                                 "branch_id": request.branch_id}, lock="update")
        require(task and task["generation"] == request.expected_generation
                and task["state"] in {"RESULT_STORED", "BLOCKED"} and task["role_id"] == "evidence_maintainer"
                and task["task_kind"] == "assertion_extract", "TASK_REBIND_REQUIRED",
                "Only a returned fact reading can use this recovery")
        matches = [(key, node) for key, node in board["nodes"].items() if node.get("task_id") == request.task_id]
        require(len(matches) == 1, "STALE_GENERATION", "Selected reading changed")
        identity, node = matches[0]
        require(node["stage"] == "facts" and node["state"] == "STOPPED" and not node.get("output_ref")
                and node.get("reason") in {"STRATEGY_REASSESSMENT_REQUIRED", "MODEL_INPUT_REQUIRED"}
                and not any(n["state"] == "ENROLLED" for n in board["nodes"].values()),
                "TASK_REBIND_REQUIRED", "Drain active handoffs before changing the phase policy")
        active = repo.conn.scalar(text("""SELECT count(*) FROM nf_call_attempts c JOIN nf_tasks t USING(task_id)
            WHERE t.work_id=:w AND t.branch_id=:b AND c.state IN ('PREPARED','SENT','RUNNING')"""),
            {"w": request.work_id, "b": request.branch_id})
        require(not active, "CALL_RECONCILIATION_REQUIRED", "Drain active upstream calls first")
        if task["state"] == "BLOCKED":
            from .native_delivery import require_native_delivery

            require(node.get("reason") == "MODEL_INPUT_REQUIRED", "TASK_REBIND_REQUIRED", "Missing-input handoff required")
            call = repo.one("call_attempts", {"task_id": task["task_id"], "generation": task["generation"],
                                             "call_key": "supervised-candidate"})
            audit_row = call and gateway._read_audit(repo, call["attempt_id"])
            evidence = audit_row and audit_row["payload"]
            result = repo.one("task_results", {"result_id": task["current_result_id"], "disposition": "CURRENT"})
            require(evidence and result and result["payload"]["status"] == "NEEDS_INPUT"
                    and task["current_result_id"] == str(uuid5(UUID(call["attempt_id"]), "task-result"))
                    and result["payload"]["artifacts"] == [
                        {k: audit_row[k] for k in ("artifact_id", "artifact_revision_id", "sha256")}, evidence["output_ref"]],
                    "INVALID_EVIDENCE", "Missing-input result identity differs")
            require_native_delivery(kernel, repo, task, call, audit_row,
                kernel.artifact(repo, evidence["response_ref"], request.work_id, kind="AGENT_RESPONSE"),
                kernel.artifact(repo, evidence["output_ref"], request.work_id, kind="AGENT_CANDIDATE"), needs_input_read=True)
        else:
            evidence, _ = CandidateDelivery(kernel, gateway).audited_candidate(repo, task, production_accounting=True)
        job = repo.one("supervisor_jobs", {"task_id": request.task_id})
        old_input = kernel.artifact(repo, job["config"]["dispatch"]["input_ref"], request.work_id,
                                    kind="AGENT_INPUT")["payload"]
        profiles = {"creative": request.creative_profile, "review": request.review_profile}
        require(all(profile in gateway.adapters for profile in profiles.values()),
                "MODEL_UNAVAILABLE", "Install both selected profiles")
        require(all(gateway.adapters[p].configuration.get("isolation") == "STATELESS_TEXT_ONLY"
                    for p in profiles.values()), "HOST_ISOLATION_UNVERIFIED", "Text-only profiles required")
        require(gateway.adapters[request.review_profile].configuration != gateway.adapters[request.creative_profile].configuration,
                "INVALID_MODEL_CONFIG", "Creation and review need distinct model configurations")
        policy_pointer = repo.one("selections", model_routing._key(wr))
        revision = policy_pointer["epoch"] if policy_pointer else 0
        policy = model_routing.idea_policy(wr, profiles)
        if policy_pointer:
            previous = kernel.artifact(repo, policy_pointer["artifact_ref"], request.work_id,
                                        kind="RUNTIME_ROUTING_POLICY")["payload"]
            policy.update({k: previous[k] for k in ("installation_id", "org_id")})
        policy["revision"] = revision + 1
        result = {"status": "READING_POLICY_PREVIEW", "old_task_id": request.task_id,
                  "old_budget_id": board["budget_id"], "node_id": identity,
                  "accounting_state": "OLD_LEDGERS_PRESERVED", "provider_calls_sent": 0,
                  "profiles": profiles, "token_reservation": request.token_reservation}
        if not apply:
            return result
        bound = kernel.in_transaction(repo)
        adopted = model_routing.activate(bound, owner, wr, policy, gateway.adapters, expected_revision=revision)
        old_budget = repo.one("budgets", {"budget_id": board["budget_id"]})
        bound.tasks.cancel(request.task_id, request.expected_generation, principal_id=principal.principal_id,
                           idempotency_key=command + ":" + request.task_id,
                           reason="New independent reading under explicitly adopted model policy; old accounting preserved")
        budget = str(uuid5(UUID(request.task_id), command + ":budget"))
        bound.tasks.create_budget(request.work_id, board["reserve_amount"], currency=old_budget["currency"], budget_id=budget)
        SharedBudgetService(bound).set_limit(owner, request.work_id, request.branch_id, budget, request.token_reservation)
        history = {**result, "prior_node": deepcopy(node), "input_ref": job["config"]["dispatch"]["input_ref"],
                   "old_attempt_id": evidence["attempt_id"], "new_budget_id": budget,
                   "policy_ref": adopted["policy_ref"], "recovered_at": repo.now().isoformat()}
        for field in ("task_id", "reason", "enrolled_at", "schema_repairs", "fact_format_repairs", "active_format_repair", "input_requirement"):
            node.pop(field, None)
        node.setdefault("policy_recoveries", []).append(history)
        board.update(budget_id=budget, token_reservation=request.token_reservation,
                     routes=dict.fromkeys(board["routes"], model_routing.POLICY_ROUTE))
        boards._enroll(bound, repo, gateway.with_kernel(bound), owner, board, identity)
        new_job = repo.one("supervisor_jobs", {"task_id": node["task_id"]})
        new_input = bound.artifact(repo, new_job["config"]["dispatch"]["input_ref"], request.work_id,
                                   kind="AGENT_INPUT")["payload"]
        require(all(old_input[k] == new_input[k] for k in
                    ("role_id", "task_kind", "material_refs", "output_contract", "runtime_sha256")),
                "TASK_REBIND_REQUIRED", "Replacement changed its authorized reading source or contract")
        cursor = rolling._load(bound, repo, request.work_id, request.branch_id)
        if cursor:
            require(cursor["allowance"]["budget_id"] == old_budget["budget_id"], "STALE_GENERATION", "Rolling ledger changed")
            cursor["allowance"].update(budget_id=budget, token_reservation=request.token_reservation, routes=board["routes"])
            cursor.setdefault("policy_recoveries", []).append({k: v for k, v in history.items() if k != "prior_node"})
            rolling._save(bound, repo, owner, cursor)
        result.update(status="READING_POLICY_ENROLLED", new_task_id=node["task_id"], new_budget_id=budget,
                      policy_ref=adopted["policy_ref"], board_ref=boards._save(bound, repo, owner, board))
        kernel._receipt(repo, principal, wr, command, request.task_id, intent, result)
        repo.conn.execute(text("SELECT pg_notify('nf_supervisor_wake', '')"))
        return result
