"""Explicitly replace a returned, stopped reading under a new routing policy.

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


def _guard_other_enrollments(repo, principal, request, board, selected):
    """A stopped operator may register a batch, keeping earlier unsent replacements frozen."""
    recoveries = repo.many("receipts", {"work_id": request.work_id, "branch_id": request.branch_id,
                                        "principal_id": principal.principal_id,
                                        "command_type": "P6_READING_POLICY_RECOVERY"})
    registered = {row["payload"].get("new_task_id"): row["payload"] for row in recoveries}
    for identity, node in board["nodes"].items():
        if identity == selected or node["state"] != "ENROLLED":
            continue
        task = repo.one("tasks", {"task_id": node.get("task_id"), "work_id": request.work_id,
                                   "branch_id": request.branch_id}, lock="update")
        receipt = task and registered.get(task["task_id"])
        job = task and repo.one("supervisor_jobs", {"task_id": task["task_id"]})
        require(receipt and task["state"] == "READY" and task["generation"] == 0
                and not task["current_result_id"] and not repo.many("call_attempts", {"task_id": task["task_id"]})
                and job and job["config"].get("production_node") == identity
                and job["config"]["dispatch"]["budget_id"] == receipt["new_budget_id"],
                "TASK_REBIND_REQUIRED", "Drain active handoffs before changing the phase policy")


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
        fact_reader = task and (task["role_id"], task["task_kind"]) == ("evidence_maintainer", "assertion_extract")
        review_reader = task and task["role_id"] == "reviewer" and task["task_kind"] in {"literary", "epistemic"}
        require(task and task["generation"] == request.expected_generation
                and (fact_reader and task["state"] in {"RESULT_STORED", "BLOCKED"}
                     or review_reader and task["state"] == "RESULT_STORED"), "TASK_REBIND_REQUIRED",
                "Only a returned fact reading or independent review can use this recovery")
        matches = [(key, node) for key, node in board["nodes"].items() if node.get("task_id") == request.task_id]
        require(len(matches) == 1, "STALE_GENERATION", "Selected reading changed")
        identity, node = matches[0]
        require(node["state"] == "STOPPED" and not node.get("output_ref")
                and (fact_reader and node["stage"] == "facts"
                     and node.get("reason") in {"STRATEGY_REASSESSMENT_REQUIRED", "MODEL_INPUT_REQUIRED"}
                     or review_reader and node["stage"] == ("review" if task["task_kind"] == "literary" else "epistemic")
                     and node.get("reason") == "REVIEW_REQUIRED"),
                "TASK_REBIND_REQUIRED", "Drain active handoffs before changing the phase policy")
        _guard_other_enrollments(repo, principal, request, board, identity)
        active = repo.conn.scalar(text("""SELECT count(*) FROM nf_call_attempts c JOIN nf_tasks t USING(task_id)
            WHERE t.work_id=:w AND t.branch_id=:b AND c.state IN ('PREPARED','SENT','RUNNING')"""),
            {"w": request.work_id, "b": request.branch_id})
        require(not active, "CALL_RECONCILIATION_REQUIRED", "Drain active upstream calls first")
        if task["state"] == "BLOCKED" or review_reader:
            from .native_delivery import require_native_delivery

            require(review_reader or node.get("reason") == "MODEL_INPUT_REQUIRED",
                    "TASK_REBIND_REQUIRED", "Missing-input handoff required")
            call = repo.one("call_attempts", {"task_id": task["task_id"], "generation": task["generation"],
                                             "call_key": "supervised-candidate"})
            audit_row = call and gateway._read_audit(repo, call["attempt_id"])
            evidence = audit_row and audit_row["payload"]
            result = repo.one("task_results", {"result_id": task["current_result_id"], "task_id": task["task_id"],
                                               "generation": task["generation"], "disposition": "CURRENT"})
            require(evidence and result and result["payload"]["status"] == ("SUCCEEDED" if review_reader else "NEEDS_INPUT")
                    and task["current_result_id"] == str(uuid5(UUID(call["attempt_id"]), "task-result"))
                    and result["payload"]["artifacts"] == [
                        {k: audit_row[k] for k in ("artifact_id", "artifact_revision_id", "sha256")}, evidence["output_ref"]],
                    "INVALID_EVIDENCE", "Returned reading result identity differs")
            require_native_delivery(kernel, repo, task, call, audit_row,
                kernel.artifact(repo, evidence["response_ref"], request.work_id, kind="AGENT_RESPONSE"),
                kernel.artifact(repo, evidence["output_ref"], request.work_id, kind="AGENT_CANDIDATE"),
                needs_input_read=not review_reader, allow_unresolved_review=bool(review_reader))
        else:
            evidence, _ = CandidateDelivery(kernel, gateway).audited_candidate(repo, task, production_accounting=True)
        call = repo.one("call_attempts", {"attempt_id": evidence["attempt_id"]})
        source_budget = repo.one("budgets", {"budget_id": call["budget_id"], "work_id": request.work_id})
        phase_budget = repo.one("budgets", {"budget_id": board["budget_id"], "work_id": request.work_id})
        require(source_budget and phase_budget and source_budget["currency"] == phase_budget["currency"],
                "INVALID_EVIDENCE", "Reading and current phase budgets differ in scope or currency")
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
                  "old_budget_id": source_budget["budget_id"], "prior_phase_budget_id": phase_budget["budget_id"],
                  "node_id": identity,
                  "accounting_state": "OLD_LEDGERS_PRESERVED", "provider_calls_sent": 0,
                  "profiles": profiles, "token_reservation": request.token_reservation}
        if not apply:
            return result
        bound = kernel.in_transaction(repo)
        adopted = model_routing.activate(bound, owner, wr, policy, gateway.adapters, expected_revision=revision)
        bound.tasks.cancel(request.task_id, request.expected_generation, principal_id=principal.principal_id,
                           idempotency_key=command + ":" + request.task_id,
                           reason="New independent reading under explicitly adopted model policy; old accounting preserved")
        budget = str(uuid5(UUID(request.task_id), command + ":budget"))
        bound.tasks.create_budget(request.work_id, board["reserve_amount"], currency=source_budget["currency"], budget_id=budget)
        SharedBudgetService(bound).set_limit(owner, request.work_id, request.branch_id, budget, request.token_reservation)
        history = {**result, "prior_node": deepcopy(node), "input_ref": job["config"]["dispatch"]["input_ref"],
                   "old_attempt_id": evidence["attempt_id"], "new_budget_id": budget,
                   "policy_ref": adopted["policy_ref"], "recovered_at": repo.now().isoformat()}
        for field in ("task_id", "reason", "enrolled_at", "schema_repairs", "fact_format_repairs", "active_format_repair", "input_requirement"):
            node.pop(field, None)
        node.setdefault("policy_recoveries", []).append(history)
        board.update(budget_id=budget, token_reservation=request.token_reservation,
                     routes=dict.fromkeys(board["routes"], model_routing.POLICY_ROUTE))
        assignment = None
        if review_reader:
            assignment = (old_input["assignment"] + "\n协议层提醒：独立重读相同稿件，保留全部实质问题。"
                          "外层 unresolved_questions 只用于缺少授权前提而无法完成任务的情况，并返回 NEEDS_INPUT。"
                          "已完成审读时，小说解释疑问、可选建议和修改要求放在审核 payload 的相应字段，"
                          "按实际证据标记 issue_kind、severity 和 disposition；不要编造答案或降低问题严重程度。")
        boards._enroll(bound, repo, gateway.with_kernel(bound), owner, board, identity, frozen_assignment=assignment)
        new_job = repo.one("supervisor_jobs", {"task_id": node["task_id"]})
        new_input = bound.artifact(repo, new_job["config"]["dispatch"]["input_ref"], request.work_id,
                                   kind="AGENT_INPUT")["payload"]
        require(all(old_input[k] == new_input[k] for k in
                    ("role_id", "task_kind", "material_refs", "output_contract", "runtime_sha256")),
                "TASK_REBIND_REQUIRED", "Replacement changed its authorized reading source or contract")
        old_spec = repo.one("task_inputs", {"task_id": task["task_id"], "generation": task["generation"]})["spec"]
        new_task = repo.one("tasks", {"task_id": node["task_id"]})
        new_spec = repo.one("task_inputs", {"task_id": node["task_id"], "generation": new_task["generation"]})["spec"]
        require(old_spec["world_ref"] == new_spec["world_ref"] and old_spec["story_time"] == new_spec["story_time"],
                "TASK_REBIND_REQUIRED", "Replacement changed its fixed world or story time")
        cursor = rolling._load(bound, repo, request.work_id, request.branch_id)
        if cursor:
            require(cursor["allowance"]["budget_id"] == phase_budget["budget_id"], "STALE_GENERATION", "Rolling ledger changed")
            cursor["allowance"].update(budget_id=budget, token_reservation=request.token_reservation, routes=board["routes"])
            cursor.setdefault("policy_recoveries", []).append({k: v for k, v in history.items() if k != "prior_node"})
            rolling._save(bound, repo, owner, cursor)
        result.update(status="READING_POLICY_ENROLLED", new_task_id=node["task_id"], new_budget_id=budget,
                      policy_ref=adopted["policy_ref"], board_ref=boards._save(bound, repo, owner, board))
        kernel._receipt(repo, principal, wr, command, request.task_id, intent, result)
        repo.conn.execute(text("SELECT pg_notify('nf_supervisor_wake', '')"))
        return result
