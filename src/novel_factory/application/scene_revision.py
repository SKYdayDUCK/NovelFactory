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
              "enrolled_at", "reason", "revision_feedback", "prior_draft_ref", "revision_brief_ref") if key in node}
    if record:
        node.setdefault("history", []).append({**record, "state": node["state"]})
    node["state"] = "PENDING"


def _writer_world(kernel, repo, board, writer):
    """A registered historical writer retains its independently proved source."""
    wr = writer.get("world_ref", board["world_ref"])
    if wr != board["world_ref"]:
        from .world_coverage_recovery import prove_pure_coverage

        require(board.get("writer_source_world_ref") == wr
                and board.get("coverage_registration_proof") == prove_pure_coverage(kernel, repo, wr, board["world_ref"]),
                "INVALID_EVIDENCE", "Writer source cut lacks its registered coverage compatibility")
    return wr


def revise_scene(kernel, repo, board):
    nodes = board["nodes"]
    work = board["world_ref"]["work_id"]
    for reader in nodes.values():
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
            if board.get("production_mode") == "quality":
                from .production_reassessment import repair_budget
                if reason := repair_budget(writer, phase="revision"):
                    writer.update(state="STOPPED", reason=reason)
                    return target
            if reader["stage"] == "facts":
                record = kernel.artifact(repo, reader["output_ref"], work, kind="ROLLING_FACT_REPORT")["payload"]
                report = kernel.artifact(repo, record["reconciliation_ref"], work, kind="RECONCILIATION_REPORT")["payload"]
                require(report["draft_ref"] == prior, "NARRATIVE_SELECTION_CONFLICT", "Fact report belongs to another draft")
                extracted = kernel.artifact(repo, record["extraction_ref"], work)["payload"]["assertions"]
                summaries = {a["assertion_id"]: a["summary"] for a in extracted}
                unresolved = [b for b in report["hard_blockers"] if b["code"].startswith("UNRESOLVED_")]
                if unresolved:
                    reader.update(reason="FACT_VERIFICATION_REQUIRED")
                    return next(key for key, value in nodes.items() if value is reader)
                issues = [{"code": b["code"], "reason": b["message"],
                           "passage": summaries.get(b["assertion_id"])} for b in report["hard_blockers"]]
            else:
                kind = "EPISTEMIC_READING" if reader["stage"] == "epistemic" else "CHAPTER_READING_ADVICE"
                record = kernel.artifact(repo, reader["output_ref"], work, kind=kind)["payload"]
                report = kernel.artifact(repo, record["candidate_ref"], work, kind="AGENT_CANDIDATE")["payload"]
                if reader["stage"] == "epistemic":
                    require(record["draft_ref"] == prior, "NARRATIVE_SELECTION_CONFLICT", "Reading belongs to another draft")
                    from .rolling_facts import epistemic_blockers
                    issues = epistemic_blockers(report["issues"])
                else:
                    issues = [i for i in editorial_blockers(report["issues"])
                              if by_draft.get(i["subject_ref"]["artifact_revision_id"]) == target]
                issues = [{k: i[k] for k in ("description", "suggested_change")} for i in issues]
            feedback = {"report_ref": reader["output_ref"], "source_draft_ref": prior, "findings": issues}
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
                    from .production_strategy import request_strategy
                    return request_strategy(board, target, reader, issues, plan)
                feedback["strategy_instruction"] = plan["method"]
                feedback.update(reflection_ref=plan["reflection_ref"], error_ref=plan["error_ref"])
            for key in (target, "knowledge:" + target[6:], "facts:" + target[6:], "review:" + reader["chapter_id"]):
                _archive(nodes[key])
                nodes[key].pop("revision_targets", None)
            writer.update(prior_draft_ref=prior, revision_feedback=feedback,
                          revision_count=writer.get("revision_count", 0) + 1)
            return target
    return None


def resume_extent_revision(kernel, repo, owner, board, identity):
    """Legacy length holds require explicit adoption of the advisory policy."""
    return False


def restore_unsent_extent_source(kernel, repo, board, identity, task):
    """Do not revive a paid rewrite solely to meet a historical length target."""
    return False


def repair_scene_extent(kernel, repo, owner, board, writer, task, audit, draft_ref, prose):
    """Record the observed size, then continue the normal independent readings."""
    from .production_extent import _count_han
    writer["extent_observation"] = {"policy": "ADVISORY", "draft_ref": draft_ref,
                                    "actual_han": _count_han(prose), "historical_target": writer.get("han_characters")}
    return False
