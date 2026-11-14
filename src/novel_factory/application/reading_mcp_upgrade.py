"""One-time, administrator-controlled migration of a legacy exact-draft reading.

This enrolls no model calls and does not reinterpret old CLEAR as MCP proof.
The source revision is the operator's recorded source commit, not a package hash.
"""
import re
from copy import deepcopy
from dataclasses import dataclass
from uuid import UUID, uuid5

from sqlalchemy import text

from ..contracts.registry import validate_contract
from ..domain.canonical import canonical_bytes, fingerprint
from . import model_routing
from . import production_pipeline as boards
from .candidate_delivery import CandidateDelivery
from .kernel import require
from .volume_successor import _selected

COMMAND = "P6_READING_MCP_UPGRADE"


@dataclass(frozen=True)
class ReadingMcpUpgrade:
    work_id: str
    branch_id: str
    expected_board_sha256: str
    writer_task_id: str
    expected_writer_generation: int
    draft_ref: dict
    source_revision: str


def _drained(kernel, repo, task):
    """Check actual durable state, including abandoned generations and Host turns."""
    require(not task["lease_expiry"] or task["lease_expiry"] <= repo.now(),
            "CALL_RECONCILIATION_REQUIRED", "Drain the reading's active worker lease first")
    abandoned = []
    for call in repo.many("call_attempts", {"task_id": task["task_id"]}):
        require(call["state"] not in {"PREPARED", "SENT", "RUNNING"},
                "CALL_RECONCILIATION_REQUIRED", "Reconcile or reclaim the original reading attempt first")
        if call["state"] != "UNKNOWN":
            continue
        host = repo.one("host_turns", {"attempt_id": call["attempt_id"]})
        if host and host["phase"] == "COMPLETED":
            continue
        require(task["state"] == "CANCELLED" and not task["current_result_id"],
                "CALL_RECONCILIATION_REQUIRED", "An unfinished Host return must be durably fenced first")
        abandoned.append({"task_id": task["task_id"], "generation": call["generation"],
                          "attempt_id": call["attempt_id"], "state": "UNKNOWN",
                          "task_state": task["state"], "current_generation": task["generation"],
                          "host_phase": host and host["phase"], "lease_expiry": task["lease_expiry"]
                          and task["lease_expiry"].isoformat()})
    return abandoned


def upgrade_reading_policy(kernel, gateway, principal, request: ReadingMcpUpgrade, *, apply=False):
    """Archive obsolete readings; normal scheduling commissions new independent readers."""
    require(type(apply) is bool, "INVALID_ARGUMENT", "Explicit apply flag required")
    for value in (request.work_id, request.branch_id, request.writer_task_id):
        validate_contract("UUID", value)
    validate_contract("ArtifactRef", request.draft_ref)
    require(type(request.expected_writer_generation) is int and request.expected_writer_generation >= 0
            and isinstance(request.expected_board_sha256, str)
            and re.fullmatch(r"[0-9a-f]{64}", request.expected_board_sha256)
            and isinstance(request.source_revision, str) and re.fullmatch(r"[0-9a-f]{40}", request.source_revision),
            "INVALID_ARGUMENT", "Exact board, writer generation and source commit are required")
    key = request.draft_ref["artifact_revision_id"]
    intent = vars(request)
    with kernel.objects.lock(), kernel.db.uow() as repo:
        repo.one("works", {"work_id": request.work_id}, lock="update")
        kernel.authorize(repo, principal, request.work_id, "ADMIN", request.branch_id)
        board = boards._load(kernel, repo, request.work_id, request.branch_id)
        require(board, "TASK_REBIND_REQUIRED", "No production board")
        owner = boards._current_owner(kernel, repo, board)
        require(principal == owner, "VIEW_DENIED", "Only the original production controller may upgrade readings")
        prior = kernel._prior(repo, principal, request.work_id, COMMAND, key, intent)
        if prior:
            return prior
        selected = _selected(repo, request.work_id, boards._key(request.branch_id))
        require(selected and selected["sha256"] == request.expected_board_sha256,
                "STALE_GENERATION", "The selected production board changed")
        repo.one("branches", {"work_id": request.work_id, "branch_id": request.branch_id}, lock="share")
        matches = [(identity, node) for identity, node in board["nodes"].items()
                   if node.get("stage") == "draft" and node.get("task_id") == request.writer_task_id]
        require(len(matches) == 1, "STALE_GENERATION", "The selected writer changed")
        identity, writer = matches[0]
        task = repo.one("tasks", {"task_id": request.writer_task_id, "work_id": request.work_id,
                                  "branch_id": request.branch_id}, lock="update")
        draft = kernel.artifact(repo, request.draft_ref, request.work_id, kind="NARRATIVE_DRAFT")["payload"]
        require(writer["state"] == "DRAFT_STORED" and writer.get("output_ref") == request.draft_ref
                and task and task["role_id"] == "writer" and task["state"] == "ACCEPTED"
                and task["generation"] == draft["generation"] == request.expected_writer_generation
                and draft["task_id"] == request.writer_task_id and draft["scene_ref"] == writer["scene_ref"],
                "STALE_GENERATION", "The exact accepted writer and selected draft must remain unchanged")
        scene = identity.removeprefix("draft:")
        require(not _selected(repo, request.work_id, "P6_FROZEN_SCENE:" + scene)
                and not _selected(repo, request.work_id, "NARRATIVE_SCENE:" + scene),
                "NARRATIVE_SELECTION_CONFLICT", "Accepted or frozen prose cannot use this reading migration")
        affected = ["knowledge:" + scene, "facts:" + scene, "review:" + writer["chapter_id"]]
        require(all(k in board["nodes"] for k in affected), "TASK_REBIND_REQUIRED", "Rolling reading nodes required")
        knowledge = board["nodes"][affected[0]]
        old_task = repo.one("tasks", {"task_id": knowledge.get("task_id")})
        old_job = old_task and repo.one("supervisor_jobs", {"task_id": old_task["task_id"]})
        require(knowledge["state"] == "KNOWLEDGE_CLEAR" and old_task and old_task["state"] == "ACCEPTED"
                and (old_task["role_id"], old_task["task_kind"]) == ("reviewer", "epistemic")
                and old_task["principal_id"] != task["principal_id"] and old_task["grant_id"] != task["grant_id"]
                and old_job and not old_job["config"].get("required_review_mcp"),
                "TASK_REBIND_REQUIRED", "Only an accepted legacy CLEAR reading may be migrated")
        record = kernel.artifact(repo, knowledge["output_ref"], request.work_id, kind="EPISTEMIC_READING")["payload"]
        evidence, _ = CandidateDelivery(kernel, gateway).audited_candidate(repo, old_task, production_accounting=True)
        require(record["draft_ref"] == request.draft_ref and record["text_ref"] == draft["text_ref"]
                and record["reader_task"] == old_task["task_id"] and record["generation"] == old_task["generation"]
                and record["candidate_ref"] == evidence["output_ref"] and record["attempt_id"] == evidence["attempt_id"]
                and record["world_ref"] == board["world_ref"] and record["status"] == "CLEAR",
                "INVALID_EVIDENCE", "Legacy CLEAR belongs to another task, draft or fixed world")
        profile = board["routes"]["review"]
        for role, kind in (("reviewer", "epistemic"), ("reviewer", "literary"), ("evidence_maintainer", "assertion_extract")):
            chosen = model_routing.select(kernel, repo, board["world_ref"], {"role_id": role, "task_kind": kind},
                                           gateway.adapters)["profile_id"] if profile == model_routing.POLICY_ROUTE else profile
            adapter = gateway.adapters.get(chosen)
            require(adapter and adapter.configuration.get("adapter_kind") == "codex-cli",
                    "MODEL_UNAVAILABLE", "Every upgraded reader requires the native MCP review route")
        revision = str(uuid5(UUID(key), COMMAND + ":" + request.branch_id))
        require(not repo.one("artifacts", {"work_id": request.work_id, "artifact_revision_id": revision}),
                "CAPACITY_LIMIT", "This exact draft already consumed its reading policy migration")
        related, abandoned = [], []
        current_tasks = {board["nodes"][k].get("task_id") for k in affected}
        for reader in repo.many("tasks", {"work_id": request.work_id, "branch_id": request.branch_id}):
            job = repo.one("supervisor_jobs", {"task_id": reader["task_id"]})
            ref = job and job["config"].get("dispatch", {}).get("input_ref")
            frozen = ref and kernel.artifact(repo, ref, request.work_id, kind="AGENT_INPUT")["payload"]
            if reader["task_id"] not in current_tasks and not (frozen and request.draft_ref in frozen["material_refs"]):
                continue
            require(reader["role_id"] in {"reviewer", "evidence_maintainer"},
                    "TASK_REBIND_REQUIRED", "Drain other consumers of this manuscript before migration")
            reader = repo.one("tasks", {"task_id": reader["task_id"]}, lock="update")
            abandoned.extend(_drained(kernel, repo, reader))
            related.append(reader)
        result = {"status": "READING_MCP_UPGRADE_PREVIEW", "provider_calls_sent": 0,
                  "draft_ref": request.draft_ref, "writer_task_id": request.writer_task_id,
                  "nodes": affected, "source_revision": request.source_revision,
                  "accounting_state": "OLD_LEDGERS_PRESERVED", "cancelled_unknown_attempts": abandoned}
        if not apply:
            return result
        bound = kernel.in_transaction(repo)
        for reader in related:
            if reader["task_id"] in current_tasks and reader["state"] not in {"ACCEPTED", "CANCELLED"}:
                bound.tasks.cancel(reader["task_id"], reader["generation"], principal_id=principal.principal_id,
                    idempotency_key=COMMAND + ":" + reader["task_id"], reason="Superseded legacy reading; all old results and accounting retained")
        for node_id in affected:
            node = board["nodes"][node_id]
            node.setdefault("history", []).append({k: deepcopy(v) for k, v in node.items() if k != "history"})
            for field in ("task_id", "output_ref", "generation", "completed_at", "enrolled_at", "reason",
                          "revision_targets", "input_requirement", "active_format_repair", "verification_repair",
                          "internal_resolution_refs"):
                node.pop(field, None)
            node.update(state="PENDING", reason="READING_POLICY_UPGRADED")
        authorization = {"record_version": 1, "request": intent, "previous_board_ref": selected,
                         "old_knowledge_ref": knowledge["history"][-1]["output_ref"],
                         "cancelled_unknown_attempts": abandoned}
        ref = {"artifact_id": str(uuid5(UUID(key), COMMAND)), "artifact_revision_id": revision,
               "sha256": fingerprint(authorization)}
        bound.objects.put(canonical_bytes(authorization))
        bound._register_artifact(repo, ref, request.work_id, authorization, "READING_MCP_UPGRADE", owner.principal_id)
        result.update(status="READING_MCP_UPGRADED", authorization_ref=ref,
                      board_ref=boards._save(bound, repo, owner, board))
        kernel._receipt(repo, principal, board["world_ref"], COMMAND, key, intent, result)
        repo.conn.execute(text("SELECT pg_notify('nf_supervisor_wake', '')"))
        return result
