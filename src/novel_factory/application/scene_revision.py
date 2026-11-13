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
                    from .rolling_facts import epistemic_blockers
                    issues = epistemic_blockers(report["issues"])
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
                if plan["decision"] == PAUSED:
                    plan = {**plan, "decision":"CHANGED_STRATEGY", "strategy_version":2,
                            "method":"按本次逐条审稿意见先核对原稿对应段落，再逐项修改，并逐项核实保留的情节和视角。"}
                plan = reflect_repair(kernel, repo, _current_owner(kernel, repo, board), board["world_ref"],
                                      reader["task_id"], call["attempt_id"], reader["output_ref"], plan, target)
                plan["review_stage"] = reader["stage"]
                history.append(plan)
                feedback["strategy_instruction"] = plan["method"]
            for key in (target, "knowledge:" + target[6:], "facts:" + target[6:], "review:" + reader["chapter_id"]):
                _archive(nodes[key])
                nodes[key].pop("revision_targets", None)
            writer.update(prior_draft_ref=prior, revision_feedback=feedback,
                          revision_count=writer.get("revision_count", 0) + 1)
            return target
    return None


def resume_extent_revision(kernel, repo, owner, board, identity):
    """Resume a measured length plateau without replacing the current authorized draft."""
    from .production_extent import _count_han
    writer = board["nodes"][identity]
    history = writer.get("extent_repairs", [])
    if not (writer["stage"] == "draft" and writer["state"] == "STOPPED" and writer.get("reason") == PAUSED
            and history and history[-1]["decision"] == PAUSED and writer.get("output_ref")):
        return False
    wr = board["world_ref"]
    require(not repo.one("selections", {"work_id":wr["work_id"], "selection_kind":"NARRATIVE_SCENE:" + identity[6:]}),
            "NARRATIVE_SELECTION_CONFLICT", "A frozen scene cannot be revised here")
    def editorial_findings(record):
        return [f for f in record.get("revision_feedback", {}).get("findings", [])
                if not isinstance(f, dict) or "actual_han" not in f]
    findings = editorial_findings(writer)
    extent = writer["han_characters"]
    target = (extent["min"] + extent["max"]) // 2
    current = writer["output_ref"]
    draft = kernel.artifact(repo, current, wr["work_id"], kind="NARRATIVE_DRAFT")["payload"]
    require(draft["input_world_ref"] == wr and draft["scene_ref"] == writer["scene_ref"]
            and draft["handoff_refs"] == writer["handoff_refs"], "INVALID_EVIDENCE", "Writer source changed")
    measured = _count_han(kernel.objects.read(draft["text_ref"]["sha256"]).decode("utf-8"))
    require(measured == history[-1]["actual"], "INVALID_EVIDENCE", "Length measurement differs from the latest draft")
    best = min(history, key=lambda item:item["distance"])["actual"]
    task = repo.one("tasks", {"task_id":writer["task_id"], "generation":writer["generation"]})
    require(task and task["state"] == "ACCEPTED", "INVALID_EVIDENCE", "Length recovery requires an accepted writer task")
    call = repo.one("call_attempts", {"task_id":task["task_id"], "generation":task["generation"],
                                      "call_key":"supervised-candidate"})
    method = (f"以本次提供的最新原稿为底本。实测{measured}个汉字，目标{target}个。此前最接近区间的实测值为{best}个。"
              f"请{'删减' if measured > target else '补足'}约{abs(measured-target)}个汉字；"
              "保留已处理的审稿意见和场景事件，逐段核对后提交完整正文。")
    plan = {**history[-1], "decision":"CHANGED_STRATEGY", "method":method, "strategy_version":2}
    plan = reflect_repair(kernel, repo, owner, wr, task["task_id"], call["attempt_id"], current,
                          plan, "CURRENT_DRAFT_EXTENT_REVISION")
    _archive(writer)
    writer.update(prior_draft_ref=current, revision_count=writer.get("revision_count", 0)+1,
                  revision_feedback={"report_ref":plan["reflection_ref"], "strategy_instruction":method,
                                     "findings":[*findings, {"actual_han":measured, "target_han":extent}]})
    return True


def restore_unsent_extent_source(kernel, repo, board, identity, task):
    """Recover old length tasks whose input draft already has a known successor."""
    writer = board["nodes"][identity]
    plans = writer.get("extent_repairs", [])
    records = writer.get("history") or []
    if (writer["stage"] != "draft" or task["state"] != "PAUSED" or not records or not plans
            or plans[-1]["decision"] != PAUSED or not writer.get("prior_draft_ref")
            or records[-1].get("reason") != PAUSED or not records[-1].get("output_ref")
            or records[-1]["output_ref"] == writer["prior_draft_ref"]):
        return False
    wr = board["world_ref"]
    require(not repo.many("call_attempts", {"task_id":task["task_id"]}), "CALL_RECONCILIATION_REQUIRED",
            "Old extent task has provider accounting and needs reconciliation")
    latest = records[-1]
    old_draft = kernel.artifact(repo, latest["output_ref"], wr["work_id"], kind="NARRATIVE_DRAFT")["payload"]
    require(old_draft["task_id"] == latest["task_id"] and old_draft["generation"] == latest["generation"]
            and old_draft["scene_ref"] == writer["scene_ref"] and old_draft["input_world_ref"] == wr
            and any(row.get("output_ref") == writer["prior_draft_ref"] for row in records),
            "INVALID_EVIDENCE", "The earlier extent source does not belong to this scene")
    restored = dict(latest)
    _archive(writer)
    writer.update(restored, state="STOPPED", reason=PAUSED)
    return True


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
