"""Persist and consume a bounded, scene-local editorial diagnosis."""
from .kernel import require
from .scene_revision import _archive


def request_strategy(board, target, reader, findings, plan):
    from .production_reassessment import reassessment_request, repair_budget
    writer = board["nodes"][target]
    if reason := repair_budget(writer):
        writer.update(state="STOPPED", reason=reason)
        return target
    request = reassessment_request(board, target, reader, findings, plan)
    identity = f"reassessment:{target}:{request['round']}"
    writer.setdefault("reassessments", []).append({"node_id": identity})
    writer.update(state="STOPPED", reason="STRATEGY_DIAGNOSIS_PENDING")
    board["nodes"][identity] = {"stage": "reassessment", "state": "PENDING",
        "parents": [target], "chapter_id": writer["chapter_id"], "reassessment_request": request}
    return target


def resume_unsent_strategy(board):
    """Resume only a diagnosis rejected before task enrollment or provider send."""
    from .production_reassessment import reassessment_assignment
    for identity, node in board["nodes"].items():
        if (node["stage"] != "reassessment" or node["state"] != "STOPPED"
                or node.get("reason") != "INVALID_ARGUMENT" or node.get("task_id") or node.get("output_ref")):
            continue
        request = node["reassessment_request"]
        writer = board["nodes"][request["target"]]
        require(writer.get("output_ref") == request["source_draft_ref"]
                and writer.get("task_id") == request["writer_task_id"]
                and writer.get("generation") == request["writer_generation"]
                and writer.get("revision_count", 0) == request["revision_count"]
                and writer.get("repair_allowance", {}) == request["repair_allowance"],
                "STALE_GENERATION", "Unsent diagnosis no longer belongs to this draft")
        require(len(reassessment_assignment(request)) <= 32000,
                "INVALID_ARGUMENT", "Diagnosis instructions exceed the frozen input contract")
        node.update(state="PENDING")
        node.pop("reason")
        return identity
    return None


def consume_strategy(kernel, repo, board):
    from .production_reassessment import repair_budget
    work = board["world_ref"]["work_id"]
    for identity, node in board["nodes"].items():
        if node["stage"] != "reassessment" or node["state"] not in {"STRATEGY_READY", "REASSESSMENT_BLOCKED"}:
            continue
        request = node["reassessment_request"]
        writer = board["nodes"][request["target"]]
        require(writer.get("output_ref") == request["source_draft_ref"]
                and writer.get("task_id") == request["writer_task_id"]
                and writer.get("generation") == request["writer_generation"]
                and writer.get("revision_count", 0) == request["revision_count"]
                and writer.get("repair_allowance", {}) == request["repair_allowance"],
                "STALE_GENERATION", "Diagnosed draft changed before strategy consumption")
        record = kernel.artifact(repo, node["output_ref"], work, kind="PRODUCTION_REASSESSMENT")["payload"]
        require(record["request"] == request and record["outcome"] == node["state"],
                "INVALID_EVIDENCE", "Diagnosis input or outcome changed")
        entry = writer["reassessments"][request["round"] - 1]
        require(entry["node_id"] == identity, "INVALID_EVIDENCE", "Diagnosis round changed")
        entry.update(strategy_ref=node["output_ref"], outcome=node["state"], method=record["method"])
        reason = repair_budget(writer, phase="revision")
        if node["state"] == "REASSESSMENT_BLOCKED" or reason:
            writer.update(state="STOPPED", reason=reason or "REASSESSMENT_BLOCKED")
        else:
            target = request["target"]
            keys = [target, "knowledge:" + target[6:], "facts:" + target[6:], "review:" + writer["chapter_id"]]
            require(all(board["nodes"][key]["state"] != "ENROLLED" for key in keys),
                    "TASK_NOT_ACCEPTABLE", "Finish current readings before changing the draft")
            for key in keys:
                _archive(board["nodes"][key])
                board["nodes"][key].pop("revision_targets", None)
            if request["review_stage"] != "extent":
                previous = writer["repair_plans"][-1]
                writer["repair_plans"].append({**previous, "decision": "CHANGED_STRATEGY",
                    "method": record["method"], "reflection_ref": node["output_ref"],
                    "strategy_version": request["round"] + 2, "review_stage": request["review_stage"]})
            writer.update(prior_draft_ref=request["source_draft_ref"],
                revision_count=writer.get("revision_count", 0) + 1,
                revision_feedback={"report_ref": request["report_ref"], "findings": request["findings"],
                    "reflection_ref": node["output_ref"],
                    **({"error_ref": writer["repair_plans"][-1]["error_ref"]}
                       if request["review_stage"] != "extent" else {}),
                    "strategy_ref": node["output_ref"], "strategy_instruction": record["method"]})
        node["state"] = "STRATEGY_CONSUMED"
        return identity
    return None
