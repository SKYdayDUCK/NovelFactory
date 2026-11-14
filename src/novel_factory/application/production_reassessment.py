"""Bounded, evidence-bound editorial diagnosis for one production scene.

This does not publish a reusable Skill, accept prose, or clear a review. A
chief-editor task proposes a local method; the next writer and independent
readers retain their existing contracts. Reusable changes still require the
adaptive instruction evaluation and promotion services.
"""
import json
from copy import deepcopy
from uuid import UUID, uuid5

from ..contracts.registry import validate_contract
from ..domain.canonical import canonical_bytes, fingerprint
from .kernel import require

MAX_REVISIONS = 12
MAX_REASSESSMENTS = 2
MAX_METHOD_CHARACTERS = 1000
MAX_ASSIGNMENT_CHARACTERS = 32000
READY = "STRATEGY_READY"
BLOCKED = "REASSESSMENT_BLOCKED"
EXHAUSTED = "REPAIR_BUDGET_EXHAUSTED"


def repair_budget(writer, *, phase="request"):
    """Charge all failures to the scene, even when findings change signatures.

    Call ``request`` only before reserving a new diagnosis. A diagnosis already
    reserved as round two may still be consumed with phase ``revision``.
    """
    require(phase in {"request", "revision"}, "INVALID_ARGUMENT", "Invalid repair phase")
    revisions = writer.get("revision_count", 0)
    diagnoses = len(writer.get("reassessments", []))
    allowance = writer.get("repair_allowance", {})
    require(isinstance(allowance, dict) and type(revisions) is int and revisions >= 0,
            "INVALID_ARGUMENT", "Invalid scene repair allowance")
    baseline = allowance.get("revision_baseline", 0)
    diagnosis_baseline = allowance.get("reassessment_baseline", 0)
    require(type(baseline) is int and 0 <= baseline <= revisions
            and type(diagnosis_baseline) is int and 0 <= diagnosis_baseline <= diagnoses,
            "INVALID_ARGUMENT", "Invalid scene repair baseline")
    if revisions - baseline >= MAX_REVISIONS:
        return EXHAUSTED
    if phase == "request" and diagnoses - diagnosis_baseline >= MAX_REASSESSMENTS:
        return EXHAUSTED
    return None


def _history(writer):
    return {name: writer.get(name, []) for name in ("repair_plans", "extent_repairs")}


def _methods(writer):
    rows = [*writer.get("repair_plans", []), *writer.get("extent_repairs", []),
            *writer.get("reassessments", [])]
    return list(dict.fromkeys(row["method"] for row in rows if row.get("method")))


def reassessment_request(board, target, reader, findings, plan):
    """Freeze a request before the caller reserves its round on the board.

    ``reader`` is the rejected writer itself for an extent plateau. The caller
    persists the returned request in a reassessment node and reserves exactly
    one entry in ``writer.reassessments`` in the same board transaction.
    """
    writer = board["nodes"][target]
    require(not repair_budget(writer), EXHAUSTED, "Scene repair allowance is exhausted")
    require(writer["state"] in {"DRAFT_STORED", "STOPPED"} and writer.get("output_ref")
            and writer.get("task_id") and writer.get("generation"),
            "TASK_REBIND_REQUIRED", "Diagnosis requires the current stored draft")
    stage = "extent" if reader["stage"] == "draft" else reader["stage"]
    require(stage in {"review", "epistemic", "facts", "extent"} and bool(findings),
            "INVALID_ARGUMENT", "Diagnosis requires specific failed findings")
    report = plan["reflection_ref"] if stage == "extent" else reader["output_ref"]
    request = {
        "record_version": 1, "world_ref": board["world_ref"], "target": target,
        "writer_task_id": writer["task_id"], "writer_generation": writer["generation"],
        "revision_count": writer.get("revision_count", 0),
        "repair_allowance": writer.get("repair_allowance", {}),
        "round": len(writer.get("reassessments", [])) + 1,
        "source_draft_ref": writer["output_ref"], "report_ref": report,
        "review_stage": stage, "findings": findings, "failed_signature": plan["signature"],
        "prior_methods": _methods(writer), "history": _history(writer),
        "history_fingerprint": fingerprint(_history(writer)),
    }
    validate_contract("WorldRef", request["world_ref"])
    for key in ("source_draft_ref", "report_ref"):
        validate_contract("ArtifactRef", request[key])
    return deepcopy(request)


def _method_summary(method):
    if len(method) <= MAX_METHOD_CHARACTERS:
        return method
    return {"sha256": fingerprint(method), "characters": len(method)}


def _recent_history(history):
    """Interleave family tails: no family hides the latest other failure type."""
    recent = []
    for index in range(1, 7):
        for family, rows in history.items():
            if len(rows) < index:
                continue
            row = rows[-index]
            summary = {"family": family}
            for key in ("method", "decision", "signature", "actual", "distance"):
                value = row.get(key)
                if value is not None:
                    summary[key] = _method_summary(value) if key == "method" else value
            recent.append(summary)
            if len(recent) == 6:
                return list(reversed(recent))
    return list(reversed(recent))


def reassessment_assignment(request):
    """Bound the task assignment; complete findings arrive as scoped materials.

    The board retains the full immutable request for validation and audit. This
    rendering never copies its unbounded findings, histories or private fields.
    """
    methods = list(dict.fromkeys(request["prior_methods"]))
    history = request["history"]
    compact = {key: request[key] for key in (
        "record_version", "world_ref", "target", "writer_task_id", "writer_generation",
        "revision_count", "round", "source_draft_ref", "report_ref", "review_stage",
        "failed_signature", "history_fingerprint",
    )}
    compact.update(request_sha256=fingerprint(request), findings_sha256=fingerprint(request["findings"]),
                   findings_count=len(request["findings"]),
                   history_counts={family: len(rows) for family, rows in history.items()},
                   history=_recent_history(history), prior_methods_count=len(methods),
                   prior_methods_sha256=fingerprint(methods),
                   prior_methods=[method for method in methods if len(method) <= MAX_METHOD_CHARACTERS][-6:],
                   oversized_prior_methods=[_method_summary(method) for method in methods
                                            if len(method) > MAX_METHOD_CHARACTERS][-6:])
    instructions = (
        "本任务仅诊断重复返工，不能批准原稿、关闭审稿问题、降低验收要求、改写正文或改变世界事实。"
        "完整问题清单在 authorized_materials 的精确 report_ref 材料中；以下仅为历史摘要，不能据摘要忽略硬问题。"
        "阅读本次给定原稿、独立审稿或实测证据，解释旧方法为何没有收敛；比较历史方法，提出具体且不同的新修订步骤。"
        "只改变本场后续写作方法，保留人物视角、已接受事件与所有独立审核。禁止要求审核者通过或忽略问题。"
        "按 AgentDecisionOutput 返回：decision 为给 Flash 写作者的可执行新方法，最多1000字符；reason 为有本次证据的根因诊断；"
        "若材料不足或无法在当前范围内解决，在 deferred_questions 写明具体阻塞，不猜测或伪造解决。"
        "evidence_refs 必须引用 source_draft_ref 和 report_ref，kind 均为 ARTIFACT；只引用本任务冻结输入。"
        "此方法尚未验证，不能宣称它已成为通用 Skill。后续仍由独立评审决定是否满足原验收要求。\n"
    )
    assignment = instructions + json.dumps(compact, ensure_ascii=False)
    # JSON control-character escapes can expand a 1000-character method sixfold.
    # Remove only historical summaries; complete current findings stay in the
    # separately authorized report material and all originals remain on board.
    for field in ("history", "prior_methods"):
        while len(assignment) > MAX_ASSIGNMENT_CHARACTERS and compact[field]:
            compact[field].pop(0)
            assignment = instructions + json.dumps(compact, ensure_ascii=False)
    require(len(assignment) <= MAX_ASSIGNMENT_CHARACTERS,
            "CAPACITY_LIMIT", "Diagnosis assignment metadata exceeds its frozen contract")
    return assignment


def _normalized_method(method):
    return " ".join(method.split()).casefold()


def assignment_request_sha256(assignment):
    """Find one frozen request digest, retaining appended format-repair guidance."""
    require(isinstance(assignment, str), "INVALID_EVIDENCE", "Diagnosis assignment is unavailable")
    digests = set()
    for line in assignment.split("\n"):
        try:
            value = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(value, dict) or "request_sha256" not in value:
            continue
        digest = value["request_sha256"]
        require(isinstance(digest, str) and len(digest) == 64
                and all(char in "0123456789abcdef" for char in digest.lower()),
                "INVALID_EVIDENCE", "Diagnosis request digest is invalid")
        digests.add(digest.lower())
    require(len(digests) == 1, "INVALID_EVIDENCE", "Diagnosis assignment requires one unambiguous request digest")
    return digests.pop()


def record_reassessment(kernel, repo, owner, board, node, task, evidence, candidate):
    """Store an already audited Codex diagnosis inside the caller's work lock.

    ``CandidateDelivery.audited_candidate`` must run first. This additionally
    rejects stale scene cuts and unbound evidence; failed judgments produce an
    immutable BLOCKED result rather than another paid retry.
    """
    request = node["reassessment_request"]
    wr = board["world_ref"]
    kernel.authorize(repo, owner, wr["work_id"], "ADMIN", wr["branch_id"])
    writer = board["nodes"][request["target"]]
    require(request["world_ref"] == wr
            and writer.get("output_ref") == request["source_draft_ref"]
            and writer.get("task_id") == request["writer_task_id"]
            and writer.get("generation") == request["writer_generation"]
            and writer.get("revision_count", 0) == request["revision_count"]
            and writer.get("repair_allowance", {}) == request["repair_allowance"]
            and fingerprint(_history(writer)) == request["history_fingerprint"]
            and len(writer.get("reassessments", [])) == request["round"],
            "TASK_REBIND_REQUIRED", "Diagnosis no longer refers to the selected failed draft")
    require((task["role_id"], task["task_kind"]) == ("chief_editor", "editorial_decision")
            and (task["work_id"], task["branch_id"]) == (wr["work_id"], wr["branch_id"])
            and evidence["task_id"] == task["task_id"]
            and evidence["generation"] == task["generation"]
            and evidence["world_ref"] == wr
            and evidence["model_configuration"].get("adapter_kind") == "codex-cli",
            "INVALID_EVIDENCE", "Only the bound Codex editorial task may diagnose a production repair")
    spec = repo.one("task_inputs", {"task_id": task["task_id"], "generation": task["generation"]})["spec"]
    require(evidence["input_ref"] in spec["input_artifacts"],
            "INVALID_EVIDENCE", "Diagnosis input is outside its frozen task manifest")
    frozen = kernel.artifact(repo, evidence["input_ref"], wr["work_id"], kind="AGENT_INPUT")["payload"]
    require(assignment_request_sha256(frozen["assignment"]) == fingerprint(request),
            "INVALID_EVIDENCE", "Diagnosis request differs from its frozen assignment")
    sources = [request["source_draft_ref"], request["report_ref"]]
    require(all(ref in spec["input_artifacts"] for ref in sources),
            "VIEW_DENIED", "Diagnosis sources are outside its frozen task input")
    for ref in sources:
        kernel.artifact(repo, ref, wr["work_id"])
    validate_contract("AgentDecisionOutput", candidate, "agent")
    citations = candidate["evidence_refs"]
    require(all(item["kind"] == "ARTIFACT" and item["artifact_ref"] in spec["input_artifacts"]
                for item in citations)
            and all(any(item["artifact_ref"] == ref for item in citations) for ref in sources),
            "INVALID_EVIDENCE", "Diagnosis must cite both exact sources and only delivered artifacts")
    method = candidate["decision"].strip()
    reason = None
    if candidate["deferred_questions"]:
        reason = "REASSESSMENT_NEEDS_INPUT"
    elif not method or len(method) > MAX_METHOD_CHARACTERS or not candidate["reason"].strip():
        reason = "REASSESSMENT_INVALID_METHOD"
    elif _normalized_method(method) in {_normalized_method(old) for old in request["prior_methods"]}:
        reason = "REASSESSMENT_REPEATED_METHOD"
    elif repair_budget(writer, phase="revision"):
        reason = EXHAUSTED
    outcome = BLOCKED if reason else READY
    payload = {"record_version": 1, "request": request, "outcome": outcome, "reason": reason,
               "method": method if outcome == READY else None, "diagnosis": candidate["reason"],
               "deferred_questions": candidate["deferred_questions"], "reusable": False,
               "task_id": task["task_id"], "generation": task["generation"],
               "attempt_id": evidence["attempt_id"], "candidate_ref": evidence["output_ref"]}
    label = "production-reassessment"
    ref = {"artifact_id": str(uuid5(UUID(task["task_id"]), label)),
           "artifact_revision_id": str(uuid5(UUID(task["task_id"]), label + ":revision")),
           "sha256": fingerprint(payload)}
    if repo.one("artifacts", {"work_id": wr["work_id"], "artifact_revision_id": ref["artifact_revision_id"]}):
        kernel.artifact(repo, ref, wr["work_id"], kind="PRODUCTION_REASSESSMENT")
    else:
        kernel.objects.put(canonical_bytes(payload))
        kernel._register_artifact(repo, ref, wr["work_id"], payload, "PRODUCTION_REASSESSMENT", owner.principal_id)
    return ref, outcome
