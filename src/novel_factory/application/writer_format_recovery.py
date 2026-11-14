"""Administrative, zero-call adoption of one lossless writer metadata derivation.

The failed result and supplier audit remain immutable. A separate result and an
actual administrative receipt identify the program's derivation explicitly.
"""
import re
from uuid import UUID, uuid5

from ..contracts.registry import validate_contract
from ..domain.canonical import canonical_bytes, fingerprint
from ..domain.temporal import point_key
from .authorization import Principal
from .kernel import require

COMMAND = "RECOVER_WRITER_FORMAT"
KIND = "WRITER_FORMAT_DERIVATION"
VERSION = "failed-writer-format-v1"


def _ref(row):
    return {key: row[key] for key in ("artifact_id", "artifact_revision_id", "sha256")}


def _source(kernel, repo, gateway, task, source_result_id, node_id):
    from .postpaid_delivery import verify_postpaid_transport

    call = repo.one("call_attempts", {"task_id": task["task_id"], "generation": task["generation"],
                                      "call_key": "supervised-candidate"})
    require(call, "INVALID_EVIDENCE", "Writer derivation has no original supervised call")
    result = repo.one("task_results", {"result_id": source_result_id, "task_id": task["task_id"],
                                       "generation": task["generation"], "disposition": "CURRENT"})
    audit_row = gateway._read_audit(repo, call["attempt_id"])
    require(result and audit_row and result["result_id"] == str(uuid5(UUID(call["attempt_id"]), "task-result"))
            and fingerprint(result["payload"]) == result["payload_hash"]
            and result["attempt_id"] == result["payload"]["attempt_id"] == task["attempt_id"]
            and result["payload"]["status"] == "FAILED"
            and result["payload"]["task_id"] == task["task_id"]
            and result["payload"]["generation"] == task["generation"]
            and result["payload"]["context_bundle_id"] == task["context_bundle_id"]
            and result["payload"]["dependency_fingerprint"] == task["dependency_fingerprint"]
            and result["payload"]["artifacts"] == [_ref(audit_row)],
            "INVALID_EVIDENCE", "The original failed execution result does not match its immutable audit")
    kernel.objects.verify(result["payload_hash"])
    audit = audit_row["payload"]
    response = kernel.artifact(repo, audit["response_ref"], task["work_id"], kind="AGENT_RESPONSE")
    verify_postpaid_transport(kernel, repo, task, call, audit_row, response,
                              syntax_source=True, source_result=result, board_node=node_id)
    return call, result, audit_row, response


def _live_writer(kernel, repo, controller, world_ref, task_id, generation, expected_board_sha256):
    from .planning import validate_task_plan_binding
    from .production_pipeline import _current_owner, _key, _load
    from .release import validate_writing_task_binding
    from .semantic_dependencies import check_task_dependencies
    from .volume_successor import _selected

    work, branch = world_ref["work_id"], world_ref["branch_id"]
    kernel.authorize(repo, controller, work, "ADMIN", branch)
    board_ref = _selected(repo, work, _key(branch))
    require(board_ref and board_ref["sha256"] == expected_board_sha256,
            "TASK_REBIND_REQUIRED", "The expected production board changed")
    board = _load(kernel, repo, work, branch)
    require(board["world_ref"] == world_ref and _current_owner(kernel, repo, board) == controller,
            "VIEW_DENIED", "Only the current production owner can adopt returned writer bytes")
    matches = [(key, value) for key, value in board["nodes"].items() if value.get("task_id") == task_id]
    require(len(matches) == 1, "TASK_REBIND_REQUIRED", "Writer is not the uniquely selected production task")
    node_id, node = matches[0]
    task = repo.one("tasks", {"task_id": task_id}, lock="update")
    require(task and task["state"] == "FAILED" and task["generation"] == generation
            and node["stage"] == "draft" and node["state"] in {"ENROLLED", "STOPPED"}
            and node.get("generation", generation) == generation
            and task["work_id"] == work and task["branch_id"] == branch
            and task["role_id"] == "writer" and task["task_kind"] in {"scene_draft", "targeted_revision"}
            and task["deadline"] > repo.now(),
            "TASK_NOT_ACCEPTABLE", "Only a current, unexpired failed writer can be derived")
    job = repo.one("supervisor_jobs", {"task_id": task_id})
    require(job and job["config"].get("production_node") == node_id,
            "TASK_REBIND_REQUIRED", "Writer no longer owns its frozen production job")
    config = job["config"]
    actor = Principal(task["grant_id"], task["principal_id"])
    grant = kernel.authorize(repo, actor, work, "ARTIFACT_WRITE", branch)
    inputs = repo.one("task_inputs", {"task_id": task_id, "generation": generation})
    require(inputs and fingerprint(inputs["spec"]) == inputs["input_hash"],
            "INVALID_EVIDENCE", "Writer input integrity failed")
    spec = inputs["spec"]
    context = repo.one("contexts", {"context_bundle_id": task["context_bundle_id"],
                                     "task_id": task_id, "generation": generation,
                                     "grant_id": task["grant_id"]})
    require(context and grant["epoch"] == config["grant_epoch"] == context["read_cut"]["permission_epoch"]
            and grant["role_id"] == task["role_id"] == spec["role_id"]
            and spec["policy_version"] == repo.one("works", {"work_id": work})["policy_version"]
            and spec["world_ref"] == node.get("world_ref", world_ref)
            and point_key(spec["story_time"]) <= point_key(grant["max_story_time"]),
            "TASK_REBIND_REQUIRED", "Writer authority, policy, cut or context changed")
    kernel.tasks._check_context(repo.conn, {**task, "world_ref": spec["world_ref"]},
                               task["context_bundle_id"], task["dependency_fingerprint"])
    check_task_dependencies(repo, task_id, generation)
    validate_task_plan_binding(repo, kernel, task, spec)
    scene = kernel.artifact(repo, node["scene_ref"], work, kind="SCENE_PLAN")["payload"]
    scene_id = scene["scene_contract"]["scene_id"]
    selected = repo.one("selections", {"work_id": work, "selection_kind": "SCENE:" + scene_id})
    require(selected and selected["artifact_ref"] == node["scene_ref"]
            and scene["scene_contract"]["input_world_ref"] == spec["world_ref"]
            and node["scene_ref"] in spec["input_artifacts"] and scene["plan_ref"] in spec["input_artifacts"]
            and point_key(scene["scene_contract"]["story_interval"]["end"]) <= point_key(spec["story_time"])
            and all(ref in spec["input_artifacts"] for ref in node["handoff_refs"])
            and not repo.one("selections", {"work_id": work, "selection_kind": "NARRATIVE_SCENE:" + scene_id})
            and not node.get("output_ref"),
            "NARRATIVE_SELECTION_CONFLICT", "Writer scene, handoff or already accepted prose changed")
    if scene.get("batch_ref") is not None:
        binding = validate_writing_task_binding(repo, kernel, task, node["scene_ref"])
        require(binding["predecessor_acceptance_refs"] == node["handoff_refs"],
                "STALE_SCENE_HANDOFF", "Writer handoffs differ from their immutable binding")
    if node.get("prior_draft_ref"):
        require(node["prior_draft_ref"] in spec["input_artifacts"],
                "TASK_REBIND_REQUIRED", "Prior writer draft is not frozen in this task")
    return board_ref, board, node_id, node, task


def recover_writer_format(kernel, gateway, controller, world_ref, *, task_id, generation,
                          expected_board_sha256, idempotency_key, source_revision, apply=False):
    """Preview by default; apply appends one explicit derivation without any call or quota."""
    from .dynamic_runtime import protect_candidate
    from .narrative import NarrativeService
    from .production_pipeline import _artifact, _save
    from .writer_format_normalization import derive_completed_writer, record_normalization

    validate_contract("WorldRef", world_ref)
    validate_contract("UUID", task_id)
    require(type(generation) is int and generation >= 1 and type(apply) is bool
            and isinstance(idempotency_key, str) and 0 < len(idempotency_key) <= 200
            and isinstance(source_revision, str) and re.fullmatch(r"[0-9a-f]{40}", source_revision)
            and isinstance(expected_board_sha256, str) and re.fullmatch(r"[0-9a-f]{64}", expected_board_sha256),
            "INVALID_ARGUMENT", "Invalid writer derivation command")
    intent = {"world_ref": world_ref, "task_id": task_id, "generation": generation,
              "expected_board_sha256": expected_board_sha256, "source_revision": source_revision}
    with kernel.objects.lock(), kernel.db.uow(write=apply) as repo:
        work, branch = world_ref["work_id"], world_ref["branch_id"]
        repo.one("works", {"work_id": work}, lock="update" if apply else "share")
        kernel.authorize(repo, controller, work, "ADMIN", branch)
        prior = kernel._prior(repo, controller, work, COMMAND, idempotency_key, intent)
        if prior:
            return prior
        repo.one("branches", {"work_id": work, "branch_id": branch}, lock="update" if apply else "share")
        board_ref, board, node_id, node, task = _live_writer(
            kernel, repo, controller, world_ref, task_id, generation, expected_board_sha256)
        call, source_result, audit_row, response = _source(
            kernel, repo, gateway, task, task["current_result_id"], node_id)
        envelope, normalization = derive_completed_writer(kernel, repo, task, audit_row["payload"], response)
        candidate = envelope["payload"]
        summary = {"status": "WRITER_FORMAT_DERIVABLE", "node_id": node_id,
                   "source_result_id": source_result["result_id"], "source_attempt_id": call["attempt_id"],
                   "source_response_ref": audit_row["payload"]["response_ref"],
                   "unchanged_payload_sha256": fingerprint(candidate), "provider_calls_sent": 0}
        if not apply:
            return summary
        normalization_ref = record_normalization(kernel, repo, task, normalization)
        candidate_ref = _artifact(kernel, repo, work, controller, "AGENT_CANDIDATE", candidate)
        audit = audit_row["payload"]
        if audit.get("dynamic_assignment_ref"):
            protect_candidate(kernel, repo, task, audit["world_ref"], candidate_ref, audit["dynamic_assignment_ref"],
                              adaptive_ref=audit.get("adaptive_instruction_ref"), context_ref=audit.get("creative_context_ref"))
        result_id = str(uuid5(UUID(call["attempt_id"]), VERSION + ":result"))
        proof = {"version": VERSION, "delivery_kind": "PROGRAM_DERIVED", "intent": intent,
                 "controller": {"principal_id": controller.principal_id, "grant_id": controller.grant_id},
                 "idempotency_key": idempotency_key, "source_board_ref": board_ref, "node_id": node_id,
                 "source_result_id": source_result["result_id"], "source_result_sha256": source_result["payload_hash"],
                 "source_audit_ref": _ref(audit_row), "source_response_ref": audit["response_ref"],
                 "source_input_ref": audit["input_ref"], "normalization_ref": normalization_ref,
                 "candidate_ref": candidate_ref, "derived_result_id": result_id,
                 "world_ref": audit["world_ref"], "task_id": task_id, "generation": generation,
                 "context_bundle_id": task["context_bundle_id"], "execution_attempt_id": task["attempt_id"],
                 "dependency_fingerprint": task["dependency_fingerprint"]}
        proof_ref = _artifact(kernel, repo, work, controller, KIND, proof)
        result = {**source_result["payload"], "result_id": result_id, "status": "SUCCEEDED",
                  "artifacts": [proof_ref, _ref(audit_row), candidate_ref, normalization_ref],
                  "safe_summary": "PROGRAM_DERIVED: lossless writer metadata delimiter; original FAILED result retained"}
        validate_contract("TaskResult", result, "2.0")
        result_ref = kernel.objects.put(canonical_bytes(result))
        kernel.tasks._register_object(repo.conn, result_ref, "task_result", result_id)
        for ref in result["artifacts"]:
            kernel.tasks._register_object(repo.conn, kernel.objects.verify(ref["sha256"]), "task_result", result_id)
        repo.add("task_results", {"result_id": result_id, "task_id": task_id, "generation": generation,
                                 "attempt_id": task["attempt_id"], "payload_hash": result_ref.sha256,
                                 "payload": result, "disposition": "CURRENT"})
        repo.change("tasks", {"task_id": task_id}, {"current_result_id": result_id, "state": "RESULT_STORED", "updated_at": repo.now()})
        kernel.tasks._emit(repo.conn, task_id, generation, "RESULT_STORED")
        # This is the ordinary writer gate, on the actual restored state, not a
        # fabricated successful source task. Failure rolls back the adoption.
        NarrativeService(kernel)._writer_binding(repo, Principal(task["grant_id"], task["principal_id"]),
                                                task_id, generation, node["scene_ref"])
        node.update(state="ENROLLED", generation=generation, format_derivation_ref=proof_ref)
        node.pop("reason", None)
        node.pop("input_requirement", None)
        resumed = []
        descendants = {node_id}
        while True:
            before = len(descendants)
            descendants.update(key for key, other in board["nodes"].items()
                               if any(parent in descendants for parent in [*other.get("parents", []),
                                   *([other["knowledge_node"]] if other.get("knowledge_node") else [])]))
            if before == len(descendants):
                break
        while True:
            changed = False
            for key, other in board["nodes"].items():
                parents = [*other.get("parents", []), *([other["knowledge_node"]] if other.get("knowledge_node") else [])]
                if (key in descendants and other["state"] == "STOPPED" and other.get("reason") == "PREDECESSOR_STOPPED"
                        and not other.get("task_id") and not other.get("output_ref")
                        and all(board["nodes"][parent]["state"] != "STOPPED" for parent in parents)):
                    other.update(state="PENDING")
                    other.pop("reason", None)
                    resumed.append(key)
                    changed = True
            if not changed:
                break
        _save(kernel, repo, controller, board)
        receipt = {**summary, "status": "WRITER_FORMAT_DERIVED", "derivation_ref": proof_ref,
                   "normalization_ref": normalization_ref, "candidate_ref": candidate_ref,
                   "derived_result_id": result_id, "source_audit_ref": _ref(audit_row), "resumed_unstarted_nodes": resumed}
        kernel._receipt(repo, controller, world_ref, COMMAND, idempotency_key, intent, receipt)
        return receipt


def is_derived_writer_result(kernel, repo, task):
    row = repo.one("task_results", {"result_id": task["current_result_id"], "task_id": task["task_id"],
                                    "generation": task["generation"]}) if task.get("current_result_id") else None
    if not row or not row["payload"]["artifacts"]:
        return False
    artifact = kernel.artifact(repo, row["payload"]["artifacts"][0], task["work_id"])
    return artifact["kind"] == KIND


def verify_derived_writer_result(kernel, repo, gateway, task):
    """Verify the administrative chain, never upgrade the rejected model audit."""
    from .writer_format_normalization import derive_completed_writer

    result = repo.one("task_results", {"result_id": task["current_result_id"], "task_id": task["task_id"],
                                       "generation": task["generation"], "disposition": "CURRENT"})
    require(result and result["payload"]["artifacts"], "INVALID_EVIDENCE", "Missing program-derived result")
    proof_ref = result["payload"]["artifacts"][0]
    proof_row = kernel.artifact(repo, proof_ref, task["work_id"], kind=KIND)
    proof = proof_row["payload"]
    require(fingerprint(proof) == proof_ref["sha256"] and proof["version"] == VERSION
            and proof["delivery_kind"] == "PROGRAM_DERIVED"
            and proof["task_id"] == task["task_id"] and proof["generation"] == task["generation"]
            and proof["context_bundle_id"] == task["context_bundle_id"]
            and proof["execution_attempt_id"] == task["attempt_id"]
            and proof["dependency_fingerprint"] == task["dependency_fingerprint"]
            and proof["derived_result_id"] == result["result_id"]
            and proof_row["created_by"] == proof["controller"]["principal_id"],
            "INVALID_EVIDENCE", "Program derivation scope differs from its execution")
    receipt = repo.one("receipts", {"work_id": task["work_id"], "branch_id": task["branch_id"],
                                    "principal_id": proof["controller"]["principal_id"],
                                    "command_type": COMMAND, "idempotency_key": proof["idempotency_key"]})
    require(receipt and receipt["request_hash"] == fingerprint(proof["intent"])
            and receipt["payload"].get("derivation_ref") == proof_ref
            and receipt["payload"].get("derived_result_id") == result["result_id"],
            "INVALID_EVIDENCE", "Program derivation has no actual administrative receipt")
    board = kernel.artifact(repo, proof["source_board_ref"], task["work_id"], kind="PRODUCTION_BOARD")["payload"]
    node = board["nodes"].get(proof["node_id"])
    require(proof["source_board_ref"]["sha256"] == proof["intent"]["expected_board_sha256"]
            and board["world_ref"] == proof["intent"]["world_ref"]
            and node and node["stage"] == "draft" and node.get("task_id") == task["task_id"]
            and board["authorization"]["principal_id"] == proof["controller"]["principal_id"]
            and board["authorization"]["grant_id"] == proof["controller"]["grant_id"],
            "INVALID_EVIDENCE", "Program derivation changed its frozen production handoff")
    call, source, audit_row, response = _source(kernel, repo, gateway, task, proof["source_result_id"], proof["node_id"])
    audit = audit_row["payload"]
    envelope, normalization = derive_completed_writer(kernel, repo, task, audit, response)
    normalized = kernel.artifact(repo, proof["normalization_ref"], task["work_id"], kind="NORMALIZED_WRITER_FORMAT")
    candidate = kernel.artifact(repo, proof["candidate_ref"], task["work_id"], kind="AGENT_CANDIDATE")
    require(normalized["payload"] == normalization and fingerprint(normalization) == proof["normalization_ref"]["sha256"]
            and candidate["payload"] == envelope["payload"] and fingerprint(candidate["payload"]) == proof["candidate_ref"]["sha256"]
            and source["payload_hash"] == proof["source_result_sha256"]
            and _ref(audit_row) == proof["source_audit_ref"]
            and audit["response_ref"] == proof["source_response_ref"] and audit["input_ref"] == proof["source_input_ref"]
            and audit["world_ref"] == proof["world_ref"]
            and result["result_id"] == str(uuid5(UUID(call["attempt_id"]), VERSION + ":result"))
            and result["payload"]["status"] == "SUCCEEDED" and result["attempt_id"] == task["attempt_id"]
            and result["payload"] == {**source["payload"], "result_id": result["result_id"], "status": "SUCCEEDED",
                "artifacts": [proof_ref, _ref(audit_row), proof["candidate_ref"], proof["normalization_ref"]],
                "safe_summary": "PROGRAM_DERIVED: lossless writer metadata delimiter; original FAILED result retained"}
            and fingerprint(result["payload"]) == result["payload_hash"],
            "INVALID_EVIDENCE", "Derived writer candidate is not an exact replay of its original bytes")
    if audit.get("creative_context_ref"):
        from .creative_context_store import check_candidate_snapshot
        check_candidate_snapshot(kernel, repo, task, {**audit, "output_ref": proof["candidate_ref"],
                                                     "derivation_ref": proof_ref})
    return {**audit, "output_ref": proof["candidate_ref"], "derivation_ref": proof_ref,
            "normalization_ref": proof["normalization_ref"], "delivery_kind": "PROGRAM_DERIVED"}, candidate["payload"]
