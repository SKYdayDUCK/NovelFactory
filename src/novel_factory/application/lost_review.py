"""Explicitly replace an irrecoverable read-only review without settling its bill.

This is an administrator recovery operation, never the transport retry path.
The old task is fenced and its UNKNOWN reservation remains on its original
ledger. A fresh task and ledger are linked by a durable, idempotent receipt.
"""

from copy import deepcopy
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid5

from novel_factory.contracts.registry import validate_contract

from . import production_pipeline as boards
from . import rolling_production as rolling
from .budget_limits import SharedBudgetService
from .kernel import require
from .volume_successor import _selected


@dataclass(frozen=True)
class LostReviewRequest:
    work_id: str
    branch_id: str
    attempt_id: str
    expected_generation: int
    expected_board_sha256: str
    acknowledge_unknown_cost: bool = False


def replace_lost_review(kernel, gateway, principal, request: LostReviewRequest, *, apply=False):
    return _replace_lost_handoff(kernel, gateway, principal, request, apply=apply, draft=False)


def _replace_lost_handoff(kernel, gateway, principal, request, *, apply, draft):
    """Shared accounting/fencing mechanics; public entry points retain separate role policies."""
    require(type(apply) is bool and type(request.acknowledge_unknown_cost) is bool,
            "INVALID_ARGUMENT", "Explicit recovery flags required")
    for value in (request.work_id, request.branch_id, request.attempt_id):
        validate_contract("UUID", value)
    require(type(request.expected_generation) is int and request.expected_generation >= 0,
            "INVALID_ARGUMENT", "Invalid generation")
    command = "P6_LOST_DRAFT_REPLACEMENT" if draft else "P6_LOST_REVIEW_REPLACEMENT"
    marker = "lost_draft_replacements" if draft else "lost_review_replacements"
    intent = {**vars(request), "acknowledge_unknown_cost": True}
    with kernel.objects.lock(), kernel.db.uow() as repo:
        repo.one("works", {"work_id": request.work_id}, lock="update")
        kernel.authorize(repo, principal, request.work_id, "ADMIN", request.branch_id)
        prior = kernel._prior(repo, principal, request.work_id, command, request.attempt_id, intent)
        if prior:
            return prior
        board_ref = _selected(repo, request.work_id, boards._key(request.branch_id))
        require(board_ref and board_ref["sha256"] == request.expected_board_sha256,
                "STALE_GENERATION", "Production board changed")
        board = boards._load(kernel, repo, request.work_id, request.branch_id)
        owner = boards._current_owner(kernel, repo, board)
        wr = board["world_ref"]
        repo.one("branches", {"work_id": request.work_id, "branch_id": request.branch_id}, lock="share")
        call = repo.one("call_attempts", {"attempt_id": request.attempt_id})
        require(call, "VIEW_DENIED", "Production attempt unavailable")
        task = repo.one("tasks", {"task_id": call["task_id"], "work_id": request.work_id,
                                 "branch_id": request.branch_id}, lock="update")
        permitted = (("writer", "scene_draft"), ("writer", "targeted_revision")) if draft else (
            ("reviewer", "epistemic"), ("reviewer", "literary"))
        require(task and (task["role_id"], task["task_kind"]) in permitted,
                "VIEW_DENIED", "Only the selected production role can use this recovery entry point")
        old_budget = repo.one("budgets", {"budget_id": call["budget_id"]}, lock="update")
        call = repo.one("call_attempts", {"attempt_id": request.attempt_id}, lock="update")
        require(task["generation"] == call["generation"] == request.expected_generation
                and task["state"] == "PAUSED" and not task["current_result_id"]
                and (not task["lease_expiry"] or task["lease_expiry"] < repo.now())
                and call["state"] == "UNKNOWN" and call["actual_tokens"] is None
                and call["created_at"] < repo.now() - timedelta(seconds=600),
                "CALL_RECONCILIATION_REQUIRED", "Production task is active, resolved, too recent or has returned output")
        matches = [(key, node) for key, node in board["nodes"].items() if node.get("task_id") == task["task_id"]]
        require(len(matches) == 1, "STALE_GENERATION", "Task is no longer the selected handoff")
        identity, node = matches[0]
        require(node["stage"] in ({"draft"} if draft else {"epistemic", "review"}) and not node.get("output_ref")
                and board["budget_id"] == call["budget_id"],
                "STALE_GENERATION", "Production output or budget changed")
        require(not node.get(marker), "CAPACITY_LIMIT", "This handoff already used its replacement")
        require(not any(other.get("task_id") and other["state"] == "ENROLLED" and key != identity
                        for key, other in board["nodes"].items()),
                "CAPACITY_LIMIT", "Drain other enrolled handoffs before replacing the phase ledger")
        job = repo.one("supervisor_jobs", {"task_id": task["task_id"]})
        require(job and call["call_key"] == "supervised-candidate"
                and job["config"].get("production_node") == identity,
                "STALE_GENERATION", "The lost call must own the enrolled production handoff")
        old_input = kernel.artifact(repo, job["config"]["dispatch"]["input_ref"], request.work_id,
                                    kind="AGENT_INPUT")["payload"]
        if draft:
            _require_lost_flash_draft(kernel, repo, gateway, request, node, task, call, old_input,
                                     job["config"]["dispatch"]["input_ref"])
        result = {"status": "REPLACEMENT_PREVIEW", "attempt_id": request.attempt_id,
                  "old_task_id": task["task_id"], "old_budget_id": call["budget_id"],
                  "unknown_reserved_amount": str(call["reserved_amount"]),
                  "unknown_reserved_tokens": call["reserved_tokens"], "currency": old_budget["currency"],
                  "node_id": identity, "accounting_state": "UNKNOWN_PRESERVED",
                  "new_dispatch_amount": board["reserve_amount"], "new_dispatch_tokens": board["token_reservation"],
                  "provider_calls_sent": 0}
        if not apply:
            return result
        require(request.acknowledge_unknown_cost, "INVALID_ARGUMENT", "Acknowledge the preserved unknown charge")
        bound = kernel.in_transaction(repo)
        bound.tasks.cancel(task["task_id"], task["generation"], principal_id=principal.principal_id,
                           idempotency_key=command + ":" + request.attempt_id,
                           reason="Lost production output abandoned; original charge remains UNKNOWN")
        new_budget = str(uuid5(UUID(request.attempt_id), command + ":budget"))
        bound.tasks.create_budget(request.work_id, board["reserve_amount"], currency=old_budget["currency"], budget_id=new_budget)
        SharedBudgetService(bound).set_limit(principal, request.work_id, request.branch_id,
                                             new_budget, board["token_reservation"])
        history = {**result, "recovered_at": repo.now().isoformat(), "new_budget_id": new_budget}
        node.setdefault(marker, []).append(history)
        node.pop("task_id")
        node.pop("reason", None)
        node.pop("enrolled_at", None)
        board["budget_id"] = new_budget
        boards._enroll(bound, repo, gateway.with_kernel(bound), owner, board, identity,
                       frozen_assignment=old_input["assignment"])
        new_job = repo.one("supervisor_jobs", {"task_id": node["task_id"]})
        new_input = kernel.artifact(repo, new_job["config"]["dispatch"]["input_ref"], request.work_id,
                                    kind="AGENT_INPUT")["payload"]
        fields = ("role_id", "task_kind", "assignment", "material_refs", "output_contract",
                  "runtime_sha256", "model_config_sha256")
        changed = [key for key in fields if old_input[key] != new_input[key]]
        require(not changed, "TASK_REBIND_REQUIRED",
                "Replacement changed frozen fields: " + ", ".join(changed))
        cursor = rolling._load(bound, repo, request.work_id, request.branch_id)
        if cursor:
            require(cursor["allowance"]["budget_id"] == call["budget_id"],
                    "STALE_GENERATION", "Rolling budget changed")
            cursor["allowance"]["budget_id"] = new_budget
            cursor.setdefault(marker, []).append(deepcopy(history))
            rolling._save(bound, repo, owner, cursor)
        board_ref = boards._save(bound, repo, owner, board)
        result.update(status="REPLACEMENT_ENROLLED", new_task_id=node["task_id"],
                      new_budget_id=new_budget, board_ref=board_ref)
        kernel._receipt(repo, principal, wr, command, request.attempt_id, intent, result)
        return result


def _require_lost_flash_draft(kernel, repo, gateway, request, node, task, call, old_input, input_ref):
    """A received draft must be reconciled, never replaced through the lost-output path."""
    from ..domain.canonical import fingerprint
    from ..infrastructure.wire_receipts import read_receipt
    from .statistics import is_official_deepseek

    audit = gateway._read_audit(repo, request.attempt_id)
    require(audit and audit["payload"]["response_ref"] is None and audit["payload"]["output_ref"] is None
            and audit["payload"]["input_ref"] == input_ref
            and audit["payload"]["attempt_id"] == request.attempt_id
            and audit["payload"]["task_id"] == task["task_id"]
            and audit["payload"]["generation"] == task["generation"]
            and audit["payload"]["request_sha256"] == call["request_hash"]
            and not repo.many("task_results", {"task_id": task["task_id"]}),
            "CALL_RECONCILIATION_REQUIRED", "A returned draft or result must be reconciled")
    config = audit["payload"]["model_configuration"]
    require(is_official_deepseek(config) and config["model"] == "deepseek-flash"
            and fingerprint(config) == old_input["model_config_sha256"],
            "MODEL_UNAVAILABLE", "Lost draft replacement requires the original DeepSeek Flash binding")
    for label in ("response", "candidate"):
        require(not repo.one("artifacts", {"work_id": request.work_id,
                    "artifact_revision_id": str(uuid5(UUID(request.attempt_id), label + ":revision"))}),
                "CALL_RECONCILIATION_REQUIRED", "A persisted draft return must be reconciled")
    try:
        wire = read_receipt(kernel.objects, request.attempt_id, call["request_hash"])
    except (ValueError, OSError, KeyError, TypeError):
        require(False, "CALL_RECONCILIATION_REQUIRED", "Draft transport evidence needs reconciliation")
    require(not wire, "CALL_RECONCILIATION_REQUIRED", "A durable draft response must be reconciled")
    scene = kernel.artifact(repo, node["scene_ref"], request.work_id, kind="SCENE_PLAN")["payload"]
    require(not repo.one("selections", {"work_id": request.work_id,
            "selection_kind": "NARRATIVE_SCENE:" + scene["scene_contract"]["scene_id"]}),
            "NARRATIVE_SELECTION_CONFLICT", "A frozen scene cannot replace its writer")
