"""Resume one completed native reading held by a proved legacy memory collision.

The operator records the installed source commit. This does not authorize a new
model call or adopt an unverified result; the ordinary delivery path still runs.
"""
import re
from dataclasses import asdict, dataclass
from uuid import UUID, uuid5

from ..contracts.registry import validate_contract
from ..domain.canonical import fingerprint
from .authorization import Principal
from .candidate_delivery import CandidateDelivery
from .kernel import require
from .narrative import NarrativeService
from .native_delivery import require_native_delivery
from .production_pipeline import _current_owner, _deliver, _key, _load, _save
from .semantic_dependencies import check_task_dependencies
from .volume_successor import _selected

COMMAND = "RECOVER_COMPLETED_NATIVE_DELIVERY"


@dataclass(frozen=True)
class CompletedDeliveryRecovery:
    world_ref: dict
    expected_board_sha256: str
    task_id: str
    expected_generation: int
    expected_result_id: str
    source_revision: str


def _legacy_collision(kernel, repo, board, node, task, evidence):
    """Prove the exact writer/plan's old deterministic key aliases this read."""
    work, proofs = board["world_ref"]["work_id"], []
    advice_hash = fingerprint({"draft_refs": [board["nodes"][key]["output_ref"] for key in node["parents"]],
        "generation": task["generation"], "task_id": task["task_id"],
        "candidate_ref": evidence["output_ref"], "literary_quality": "NOT_EVALUATED"})
    for key in node["parents"]:
        writer = board["nodes"][key]
        plans = writer.get("repair_plans", [])
        if not plans or plans[-1].get("review_stage") != "review":
            continue
        call = repo.one("call_attempts", {"task_id": writer["task_id"], "generation": writer["generation"],
                                          "call_key": "supervised-candidate"})
        if not call:
            continue
        row = repo.one("artifacts", {"work_id": work, "artifact_revision_id": str(uuid5(
            UUID(call["attempt_id"]), "reviewed-local-repair:revision")), "kind": "P6_REFLECTION_RECORD"})
        if not row:
            continue
        ref = {field: row[field] for field in ("artifact_id", "artifact_revision_id", "sha256")}
        legacy = kernel.artifact(repo, ref, work, kind="P6_REFLECTION_RECORD")["payload"]
        require(fingerprint(legacy) == ref["sha256"], "INVALID_EVIDENCE", "Legacy reflection content binding failed")
        kernel.objects.verify(ref["sha256"])
        if not (legacy.get("world_ref") == board["world_ref"] and legacy.get("attempt_id") == call["attempt_id"]
                and legacy.get("phase") == "STAGE_COMPLETED" and legacy.get("actual_fix") == writer["output_ref"]
                and legacy.get("error_ref") == plans[-1]["error_ref"]
                and legacy.get("status") == "LOCAL_VALIDATION_ONLY" and legacy.get("reusable") is False):
            continue
        prior = kernel.artifact(repo, legacy["regression_result"], work, kind="CHAPTER_READING_ADVICE")["payload"]
        require(fingerprint(prior) == legacy["regression_result"]["sha256"],
                "INVALID_EVIDENCE", "Legacy chapter advice content binding failed")
        if (writer["output_ref"] in prior["draft_refs"] and prior["task_id"] != task["task_id"]
                and prior["candidate_ref"] != evidence["output_ref"]
                and legacy["regression_result"]["sha256"] != advice_hash):
            proofs.append(ref)
    require(proofs, "INVALID_EVIDENCE", "The stopped reading has no exact legacy quality-memory key collision")
    return proofs


def resume_completed_delivery(kernel, gateway, principal, request: CompletedDeliveryRecovery, *, idempotency_key, apply=False):
    require(isinstance(request, CompletedDeliveryRecovery) and type(apply) is bool,
            "INVALID_ARGUMENT", "An explicit completed-delivery recovery request is required")
    validate_contract("WorldRef", request.world_ref)
    for value in (request.task_id, request.expected_result_id):
        validate_contract("UUID", value)
    require(type(request.expected_generation) is int and request.expected_generation >= 0
            and isinstance(request.expected_board_sha256, str) and re.fullmatch(r"[0-9a-f]{64}", request.expected_board_sha256)
            and isinstance(request.source_revision, str) and re.fullmatch(r"[0-9a-f]{40}", request.source_revision)
            and isinstance(idempotency_key, str) and 0 < len(idempotency_key) <= 200,
            "INVALID_ARGUMENT", "Fixed board, generation, result, source commit and idempotency key are required")
    wr, intent = request.world_ref, asdict(request)
    with kernel.objects.lock(), kernel.db.uow() as repo:
        repo.one("works", {"work_id": wr["work_id"]}, lock="update")
        kernel.authorize(repo, principal, wr["work_id"], "ADMIN", wr["branch_id"])
        board = _load(kernel, repo, wr["work_id"], wr["branch_id"])
        require(board and _current_owner(kernel, repo, board) == principal,
                "VIEW_DENIED", "Only the enrolled production controller may resume this reading")
        prior = kernel._prior(repo, principal, wr["work_id"], COMMAND, idempotency_key, intent)
        if prior:
            return prior
        board_ref = _selected(repo, wr["work_id"], _key(wr["branch_id"]))
        require(board_ref["sha256"] == request.expected_board_sha256, "STALE_GENERATION", "The selected production board changed")
        repo.one("branches", {"work_id": wr["work_id"], "branch_id": wr["branch_id"]}, lock="share")
        require(kernel.head(repo, wr["work_id"], wr["branch_id"]) == board["world_ref"] == wr,
                "BRANCH_CHANGED", "Completed-delivery recovery requires its unchanged exact world cut")
        matches = [(key, node) for key, node in board["nodes"].items() if node.get("task_id") == request.task_id]
        require(len(matches) == 1, "STALE_GENERATION", "The selected reading task changed")
        identity, node = matches[0]
        require(node["stage"] == "review" and node["state"] == "STOPPED" and node.get("reason") == "VIEW_DENIED"
                and not node.get("output_ref"), "TASK_NOT_ACCEPTABLE", "Only the held legacy-collision chapter reading may resume")
        task = repo.one("tasks", {"task_id": request.task_id, "work_id": wr["work_id"], "branch_id": wr["branch_id"]}, lock="update")
        require(task and task["generation"] == request.expected_generation
                and task["current_result_id"] == request.expected_result_id,
                "STALE_GENERATION", "The completed reading generation or result changed")
        require(task["state"] == "RESULT_STORED" and (task["role_id"], task["task_kind"]) == ("reviewer", "literary"),
                "TASK_NOT_ACCEPTABLE", "Only the original completed literary return may resume")
        bound, actor = kernel.in_transaction(repo), Principal(task["grant_id"], task["principal_id"])
        NarrativeService(bound)._task(repo, actor, task["task_id"], task["generation"])
        check_task_dependencies(repo, task["task_id"], task["generation"])
        evidence, _ = CandidateDelivery(bound, gateway.with_kernel(bound)).audited_candidate(repo, task, production_accounting=True)
        call = repo.one("call_attempts", {"attempt_id": evidence["attempt_id"]})
        audit = gateway.with_kernel(bound)._read_audit(repo, evidence["attempt_id"])
        require_native_delivery(bound, repo, task, call, audit,
            bound.artifact(repo, evidence["response_ref"], wr["work_id"], kind="AGENT_RESPONSE"),
            bound.artifact(repo, evidence["output_ref"], wr["work_id"], kind="AGENT_CANDIDATE"))
        require(evidence["world_ref"] == wr, "BRANCH_CHANGED", "The returned reading used another world cut")
        collisions = _legacy_collision(bound, repo, board, node, task, evidence)
        result = {"status": "COMPLETED_DELIVERY_RECOVERY_PREVIEW", "node_id": identity, "task_id": task["task_id"],
                  "generation": task["generation"], "result_id": task["current_result_id"], "attempt_id": evidence["attempt_id"],
                  "old_board_ref": board_ref, "legacy_collision_refs": collisions, "provider_calls_sent": 0}
        if not apply:
            return result
        task_ids = [item["task_id"] for item in repo.many("tasks", {"work_id": wr["work_id"]})]
        calls = repo.many("call_attempts", {"task_id": task_ids})
        budgets = repo.many("budgets", {"work_id": wr["work_id"]})
        _deliver(bound, repo, gateway.with_kernel(bound), board, node, task)
        require(repo.many("call_attempts", {"task_id": task_ids}) == calls
                and repo.many("budgets", {"work_id": wr["work_id"]}) == budgets,
                "INVALID_EVIDENCE", "Completed delivery recovery cannot change model attempts or accounting")
        result.update(status="COMPLETED_DELIVERY_RECOVERED", new_board_ref=_save(bound, repo, principal, board), state=node["state"])
        bound._receipt(repo, principal, wr, COMMAND, idempotency_key, intent, result)
        return result
