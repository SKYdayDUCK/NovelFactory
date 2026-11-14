"""Allow narrative attribution only after an independent exact-draft MCP reading."""
from ..domain.reconciliation import verify_span
from .kernel import require


def attributed_assertions(kernel, repo, draft_ref, draft, extraction, world_ref):
    from .review_mcp import require_review_queries
    candidates = [a for a in extraction["assertions"] if a["classification"] == "ATTRIBUTED_STATEMENT"]
    if not candidates:
        return set()
    require(extraction["draft_ref"] == draft_ref and extraction["text_ref"] == draft["text_ref"],
            "INVALID_EVIDENCE", "Attribution extraction belongs to another exact text")
    selection = repo.one("selections", {"work_id": world_ref["work_id"],
        "selection_kind": "MCP_EPISTEMIC:" + draft_ref["artifact_revision_id"]})
    if not selection:
        return set()
    reading = kernel.artifact(repo, selection["artifact_ref"], world_ref["work_id"], kind="EPISTEMIC_READING")["payload"]
    task = repo.one("tasks", {"task_id": reading["reader_task"]})
    origin = repo.one("tasks", {"task_id": draft["task_id"]})
    require(task and origin and task["state"] == "ACCEPTED"
            and (task["role_id"], task["task_kind"]) == ("reviewer", "epistemic")
            and task["generation"] == reading["generation"]
            and origin["generation"] == draft["generation"]
            and all(task[key] == origin[key] == world_ref[key] == reading["world_ref"][key]
                    for key in ("work_id", "branch_id"))
            and task["principal_id"] != origin["principal_id"] and task["grant_id"] != origin["grant_id"]
            and reading["draft_ref"] == draft_ref and reading["text_ref"] == draft["text_ref"]
            and reading["status"] == "CLEAR" and repo.ancestor(reading["world_ref"], world_ref),
            "INVALID_EVIDENCE", "Narrative attribution requires an independent reading of this exact text")
    call = repo.one("call_attempts", {"attempt_id": reading["attempt_id"]})
    require(call and call["task_id"] == task["task_id"] and call["generation"] == task["generation"],
            "INVALID_EVIDENCE", "Attribution reading has no bound native attempt")
    from ..domain.review_gate import blocking_review_issues
    from .agent_gateway import AgentGateway
    from .candidate_delivery import CandidateDelivery
    audit, candidate = CandidateDelivery(kernel, AgentGateway(kernel, {})).audited_candidate(
        repo, task, production_accounting=True)
    require(audit["attempt_id"] == reading["attempt_id"]
            and audit["world_ref"] == reading["world_ref"]
            and audit["output_ref"] == reading["candidate_ref"]
            and not blocking_review_issues(candidate["issues"]),
            "INVALID_EVIDENCE", "Attribution review candidate changed or still has mandatory findings")
    require_review_queries(kernel, repo, task, audit)
    text = kernel.objects.read(draft["text_ref"]["sha256"]).decode("utf-8")
    for assertion in candidates:
        verify_span(text, assertion["span"])
    return {a["assertion_id"] for a in candidates}
