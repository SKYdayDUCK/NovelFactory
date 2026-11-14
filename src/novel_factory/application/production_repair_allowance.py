"""One explicit, CAS-bound migration from legacy unbounded scene repair.

This authorizes a finite policy allowance. It does not renew money, leases,
tasks or model calls, rewrite repair history, or unblock an unresolved Attempt.
"""
import re
from contextlib import nullcontext
from copy import deepcopy
from uuid import UUID, uuid5

from ..contracts.registry import validate_contract
from ..domain.canonical import canonical_bytes, fingerprint
from .kernel import require
from .planning import PlanningService
from .production_pipeline import _key, _save
from .production_reassessment import MAX_REASSESSMENTS, MAX_REVISIONS
from .technical_repair_budget import MAX_TECHNICAL_REPAIRS, repair_count
from .volume_successor import _selected

POLICY = "nf-scene-repair-allowance-v1"
COMMAND = "PRODUCTION_REPAIR_ALLOWANCE"


def _technical_bindings(repo, board, writer_id, *, lock):
    writer = board["nodes"][writer_id]
    scene = writer_id.removeprefix("draft:")
    identities = [writer_id, "knowledge:" + scene, "facts:" + scene, "review:" + writer["chapter_id"]]
    expected_stages = ["draft", "epistemic", "facts", "review"]
    result = []
    for identity, stage in zip(identities, expected_stages, strict=True):
        node = board["nodes"].get(identity)
        require(node and node.get("stage") == stage and "technical_repair_allowance" not in node,
                "REPAIR_ALLOWANCE_ALREADY_AUTHORIZED", "Technical migration requires four eligible legacy nodes")
        task_id, generation = node.get("task_id"), None
        if task_id:
            task = repo.one("tasks", {"task_id": task_id}, lock="share" if lock else None)
            wr = board["world_ref"]
            require(task and (task["work_id"], task["branch_id"]) == (wr["work_id"], wr["branch_id"]),
                    "STALE_GENERATION", "Technical repair task belongs to another production scope")
            require(task["role_id"] == {"draft": "writer", "epistemic": "reviewer",
                    "facts": "evidence_maintainer", "review": "reviewer"}[stage],
                    "STALE_GENERATION", "Technical repair task role differs from its node")
            generation = task["generation"]
            if "generation" in node:
                require(node["generation"] == generation, "STALE_GENERATION", "Technical node generation changed")
            elif node.get("state") != "ENROLLED":
                history = [*node.get("schema_repairs", []), *node.get("fact_format_repairs", [])]
                require(stage != "draft" and node.get("state") == "STOPPED"
                        and any(row.get("task_id") == task_id and row.get("generation") == generation
                                for row in history),
                        "STALE_GENERATION", "Missing node generation requires exact technical failure evidence")
        else:
            require("generation" not in node and not node.get("output_ref")
                    and node.get("state") in {"PENDING", "STOPPED", "WAITING_HANDOFF"},
                    "STALE_GENERATION", "A technical generation or result has no current task")
        result.append({"node_id": identity, "task_id": task_id, "generation": generation,
                       "repair_count": repair_count(node)})
    return result


def authorize_repair_allowance(kernel, owner, *, world_ref, expected_board_sha256, node_id,
                               expected_task_id, expected_generation, expected_revision,
                               source_revision, idempotency_key, technical_bindings=None, apply=False):
    """Preview by default; explicit apply grants this legacy scene 12 repairs/2 diagnoses.

    The same applied request/key returns its immutable receipt even after the
    board advances. A different key cannot grant a second allowance, including
    after an administrative selection of an older board.
    """
    validate_contract("WorldRef", world_ref)
    validate_contract("UUID", expected_task_id)
    PlanningService._key(idempotency_key)
    require(not apply or isinstance(technical_bindings, list) and len(technical_bindings) == 4,
            "INVALID_ARGUMENT", "Apply requires the four explicit technical bindings returned by preview")
    require(type(apply) is bool and isinstance(node_id, str) and node_id.startswith("draft:")
            and len(node_id) <= 200
            and isinstance(expected_board_sha256, str) and re.fullmatch(r"[0-9a-f]{64}", expected_board_sha256)
            and isinstance(source_revision, str) and re.fullmatch(r"[0-9a-f]{40}", source_revision)
            and type(expected_generation) is int and expected_generation >= 1
            and type(expected_revision) is int and expected_revision >= 0,
            "INVALID_ARGUMENT", "Repair authorization requires exact source, board, task and revision bindings")
    request = {"policy": POLICY, "world_ref": world_ref, "expected_board_sha256": expected_board_sha256,
               "node_id": node_id, "expected_task_id": expected_task_id,
               "expected_generation": expected_generation, "expected_revision": expected_revision,
               "source_revision": source_revision, "technical_bindings": technical_bindings}
    work, branch = world_ref["work_id"], world_ref["branch_id"]
    identity = str(uuid5(UUID(work), POLICY + ":" + branch + ":" + node_id))
    revision = str(uuid5(UUID(identity), "authorization"))
    with kernel.objects.lock() if apply else nullcontext(), kernel.db.uow(write=apply) as repo:
        repo.one("works", {"work_id": work}, lock="update" if apply else None)
        grant, _ = PlanningService(kernel)._authorize(repo, owner, world_ref, capability="ADMIN")
        previous = kernel._prior(repo, owner, work, COMMAND, idempotency_key, request)
        if previous:
            return previous
        selected = _selected(repo, work, _key(branch))
        require(selected and selected["sha256"] == expected_board_sha256,
                "STALE_PLAN", "Production board changed before repair authorization")
        board = deepcopy(kernel.artifact(repo, selected, work, kind="PRODUCTION_BOARD")["payload"])
        require(board["world_ref"] == world_ref, "STALE_PLAN", "Production world cut differs")
        writer = board["nodes"].get(node_id)
        require(writer and writer.get("stage") == "draft"
                and writer.get("task_id") == expected_task_id
                and (writer.get("generation") == expected_generation
                     or ("generation" not in writer and writer.get("state") == "ENROLLED"))
                and writer.get("revision_count", 0) == expected_revision,
                "STALE_GENERATION", "The selected writer task, generation or repair count changed")
        task = repo.one("tasks", {"task_id": expected_task_id}, lock="share" if apply else None)
        require(task and task["work_id"] == work and task["branch_id"] == branch
                and task["generation"] == expected_generation and task["role_id"] == "writer",
                "STALE_GENERATION", "The writer's authoritative task binding differs")
        require("repair_allowance" not in writer
                and not repo.one("artifacts", {"work_id": work, "artifact_revision_id": revision}),
                "REPAIR_ALLOWANCE_ALREADY_AUTHORIZED", "This scene already received its one-time policy migration")
        diagnoses = writer.get("reassessments", [])
        require(isinstance(diagnoses, list), "INVALID_ARGUMENT", "Invalid historical diagnosis list")
        current_bindings = _technical_bindings(repo, board, node_id, lock=apply)
        require(technical_bindings is None or current_bindings == technical_bindings,
                "STALE_GENERATION", "Technical task, generation or repair count changed since preview")
        allowance = {"revision_baseline": expected_revision, "reassessment_baseline": len(diagnoses)}
        result = {"status": "PREVIEW", "board_ref": selected, "repair_allowance": allowance,
                  "remaining_revisions": MAX_REVISIONS, "remaining_reassessments": MAX_REASSESSMENTS,
                  "technical_bindings": current_bindings, "remaining_technical_repairs_per_node": MAX_TECHNICAL_REPAIRS,
                  "provider_calls_sent": 0}
        if not apply:
            return result
        authorization = {"record_version": 1, "request": request, "previous_board_ref": selected,
                         "repair_allowance": allowance, "max_revisions": MAX_REVISIONS,
                         "max_reassessments": MAX_REASSESSMENTS, "max_technical_repairs": MAX_TECHNICAL_REPAIRS,
                         "technical_bindings": current_bindings, "principal_id": owner.principal_id,
                         "grant_id": owner.grant_id, "grant_epoch": grant["epoch"],
                         "authorized_at": repo.now().isoformat(), "provider_calls_sent": 0}
        authorization_ref = {"artifact_id": identity, "artifact_revision_id": revision,
                             "sha256": fingerprint(authorization)}
        kernel.objects.put(canonical_bytes(authorization))
        kernel._register_artifact(repo, authorization_ref, work, authorization,
                                  "PRODUCTION_REPAIR_AUTHORIZATION", owner.principal_id)
        writer["repair_allowance"] = allowance
        for binding in current_bindings:
            board["nodes"][binding["node_id"]]["technical_repair_allowance"] = {"repair_baseline": binding["repair_count"]}
        result.update(status="APPLIED", board_ref=_save(kernel, repo, owner, board),
                      authorization_ref=authorization_ref)
        kernel._receipt(repo, owner, world_ref, COMMAND, idempotency_key, request, result)
        return result
