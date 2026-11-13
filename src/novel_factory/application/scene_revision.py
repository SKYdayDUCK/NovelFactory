"""Stage an authorized writer revision after a fully delivered, version-bound reading."""
from ..domain.canonical import fingerprint
from .kernel import require
from .quality_memory import PAUSED, plan_repair, reflect_repair


def editorial_blockers(issues):
    return [issue for issue in issues if issue["disposition"] in {"OPEN", "ACCEPT"}
            and (issue["severity"] == "BLOCKER" or issue["issue_kind"] == "CHANGE_REQUEST")]


def _archive(node):
    # Keep the failed or superseded result, immutable task identity and report.
    record = {key: node.pop(key) for key in ("task_id", "output_ref", "generation", "completed_at",
              "enrolled_at", "reason", "revision_feedback", "prior_draft_ref") if key in node}
    if record:
        node.setdefault("history", []).append({**record, "state": node["state"]})
    node["state"] = "PENDING"


def revise_scene(kernel, repo, board):
    nodes = board["nodes"]
    work = board["world_ref"]["work_id"]
    for identity, reader in nodes.items():
        if (reader["stage"] not in {"review", "epistemic", "facts"} or reader["state"] != "STOPPED"
                or not reader.get("output_ref")):
            continue
        chapter = nodes["review:" + reader["chapter_id"]]
        if chapter["state"] == "ENROLLED":
            continue  # Finish recording that exact reading before invalidating it.
        if reader["reason"] not in {"EPISTEMIC_REVIEW_REQUIRED", "NARRATIVE_ALIGNMENT_REQUIRED",
                                     "CHAPTER_REWORK_REQUIRED"}:
            continue
        by_draft = {nodes[key]["output_ref"]["artifact_revision_id"]: key for key in chapter["parents"]
                    if nodes[key].get("output_ref")}
        if reader["stage"] == "review":
            targets = reader.get("revision_targets", [])
        else:
            targets = reader["parents"]
        for target in targets:
            writer = nodes[target]
            if (writer["state"] != "DRAFT_STORED" or (board.get("production_mode") != "quality" and writer.get("revision_count", 0) >= board["max_reworks"])
                    or any(nodes[key]["state"] == "ENROLLED" for key in
                           ("knowledge:" + target[6:], "facts:" + target[6:]))):
                continue
            if repo.one("selections", {"work_id": work, "selection_kind": "NARRATIVE_SCENE:" + target[6:]}):
                continue
            prior = writer["output_ref"]
            if reader["stage"] == "facts":
                record = kernel.artifact(repo, reader["output_ref"], work, kind="ROLLING_FACT_REPORT")["payload"]
                report = kernel.artifact(repo, record["reconciliation_ref"], work, kind="RECONCILIATION_REPORT")["payload"]
                require(report["draft_ref"] == prior, "NARRATIVE_SELECTION_CONFLICT", "Fact report belongs to another draft")
                extracted = kernel.artifact(repo, record["extraction_ref"], work)["payload"]["assertions"]
                summaries = {a["assertion_id"]: a["summary"] for a in extracted}
                issues = [{"reason": b["message"], "passage": summaries.get(b["assertion_id"])} for b in report["hard_blockers"]]
            else:
                kind = "EPISTEMIC_READING" if reader["stage"] == "epistemic" else "CHAPTER_READING_ADVICE"
                record = kernel.artifact(repo, reader["output_ref"], work, kind=kind)["payload"]
                report = kernel.artifact(repo, record["candidate_ref"], work, kind="AGENT_CANDIDATE")["payload"]
                if reader["stage"] == "epistemic":
                    require(record["draft_ref"] == prior, "NARRATIVE_SELECTION_CONFLICT", "Reading belongs to another draft")
                    issues = report["issues"]
                else:
                    issues = [i for i in editorial_blockers(report["issues"])
                              if by_draft.get(i["subject_ref"]["artifact_revision_id"]) == target]
                issues = [{k: i[k] for k in ("description", "suggested_change")} for i in issues]
            feedback = {"report_ref": reader["output_ref"], "findings": issues}
            if board.get("production_mode") == "quality":
                # Hash the diagnosed findings as well as their count. Different unresolved issues are
                # progress even when a review still reports the same number of blockers.
                findings = [reader["reason"], sorted(fingerprint(item) for item in issues)]
                history = writer.setdefault("repair_plans", [])
                plan = plan_repair("editorial", findings, history)
                task = repo.one("tasks", {"task_id": reader["task_id"]})
                call = repo.one("call_attempts", {"task_id": reader["task_id"], "generation": task["generation"],
                                                  "call_key": "supervised-candidate"})
                from .production_pipeline import _current_owner
                plan = reflect_repair(kernel, repo, _current_owner(kernel, repo, board), board["world_ref"],
                                      reader["task_id"], call["attempt_id"], reader["output_ref"], plan, target)
                plan["review_stage"] = reader["stage"]
                history.append(plan)
                if plan["decision"] == PAUSED:
                    reader["reason"] = PAUSED
                    return target
                feedback["strategy_instruction"] = plan["method"]
            for key in (target, "knowledge:" + target[6:], "facts:" + target[6:], "review:" + reader["chapter_id"]):
                _archive(nodes[key])
                nodes[key].pop("revision_targets", None)
            writer.update(prior_draft_ref=prior, revision_feedback=feedback,
                          revision_count=writer.get("revision_count", 0) + 1)
            return target
    return None


def repair_scene_extent(kernel, repo, owner, board, writer, task, audit, draft_ref, prose):
    extent = writer.get("han_characters")
    if board.get("production_mode") != "quality" or extent is None:
        return False
    from .production_extent import _count_han
    actual = _count_han(prose)
    if extent["min"] <= actual <= extent["max"]:
        return False
    history = writer.setdefault("extent_repairs", [])
    distance = max(extent["min"] - actual, actual - extent["max"])
    best_index = min(range(len(history)), key=lambda i: history[i]["distance"], default=0)
    best = history[best_index]["distance"] if history else distance + 1
    # Compare with the best measured result to distinguish net progress from oscillation.
    failures = [] if distance < best else history[best_index:]
    chosen = plan_repair("extent", extent, failures)
    chosen = reflect_repair(kernel, repo, owner, board["world_ref"], task["task_id"], audit["attempt_id"],
                            draft_ref, chosen, "SCENE_HAN_EXTENT")
    history.append({**chosen, "distance": distance, "actual": actual})
    writer.update(state="DRAFT_STORED", output_ref=draft_ref, generation=task["generation"],
                  completed_at=repo.now().isoformat(), reason="SCENE_LENGTH_OUTSIDE_TARGET")
    if chosen["decision"] == PAUSED:
        writer.update(state="STOPPED", reason=PAUSED)
        return True
    old = writer.get("revision_feedback", {}).get("findings", [])
    _archive(writer)
    findings = [finding for finding in old if not isinstance(finding, dict) or "actual_han" not in finding]
    findings.append({"actual_han": actual, "target_han": extent})
    writer.update(prior_draft_ref=draft_ref, revision_count=writer.get("revision_count", 0) + 1,
                  revision_feedback={"report_ref": chosen["reflection_ref"], "findings": findings,
                                     "strategy_instruction": chosen["method"]})
    return True
