"""Compose a returned ending plan with its own frozen opening for a fresh, explicit review."""
from novel_factory.contracts.registry import validate_contract
from novel_factory.domain.canonical import strict_json_loads
from .kernel import require
from .production_pipeline import _artifact
from .scene_revision import editorial_blockers


def replay_ending_plan(kernel, repo, wr, designer, envelope, audit):
    fix = designer.get("revision_purpose") == "SHORT_ENDING_FIX"
    require(fix or designer.get("revision_purpose") == "SHORT_ENDING", "VIEW_DENIED", "No authorized ending revision")
    validate_contract("AgentModelResponse", envelope, "agent")
    require(envelope["status"] == "SUCCEEDED" and all(envelope[k] == audit[k] for k in
            ("task_id", "generation", "context_bundle_id", "world_ref", "role_id", "actor_id")),
            "MODEL_IDENTITY_MISMATCH", "Ending response does not belong to its assigned designer")
    work = wr["work_id"]
    binding = kernel.artifact(repo, designer["input_ref"], work, kind="AGENT_INPUT")["payload"]
    assignment = strict_json_loads(binding["assignment"])
    prior = kernel.artifact(repo, designer["candidate_ref"], work)["payload"]
    review = kernel.artifact(repo, designer["review_ref"], work, kind="AGENT_CANDIDATE")["payload"]
    require(assignment["candidate"] == prior and assignment["candidate_ref"] == designer["candidate_ref"]
            and assignment["review_ref"] == designer["review_ref"]
            and (fix or (assignment["short_arc"] == review["short_arc"]
                         and review["short_arc"]["decision"] == "ENDING_PLAN_REQUIRED")), "INVALID_EVIDENCE",
            "Ending revision lost its source design or prior independent ruling")
    raw = envelope["payload"]
    if fix:
        return replay_fix(kernel, repo, wr, designer, audit, envelope, assignment, review, prior)
    if binding["output_contract"]["name"] == "ShortEndingPlan":
        patch = validate_contract("ShortEndingPlan", raw, "production_profile")
        design = {**prior, **patch}
    else:
        # The original full-design task may have supplied unnecessary world fields. Preserve those
        # fields in the raw return; its frozen remit is to amend only the storyline and chapter goals.
        patch = validate_contract("FirstWorldDesign", raw, "production_profile")
        design = {**prior, "storyline": patch["storyline"], "chapter_goals": patch["chapter_goals"]}
        if "opening" in patch:
            design["opening"] = patch["opening"]
    stages = design["chapter_goals"]
    count = len(prior["chapter_goals"])
    require(len(stages) >= count and count <= assignment["first_window_limit"], "INVALID_MODEL_OUTPUT",
            "The returned ending cannot cover its frozen chapter count")
    base, extra = divmod(len(stages), count)
    sizes = [base + int(i < extra) for i in range(count)]
    chapters = []
    start = 0
    for size in sizes:
        chapters.append("\n".join(stages[start:start + size]))
        start += size
    design["chapter_goals"] = chapters
    name = "EnvironmentalOpeningWorldDesign" if prior["opening"].get("environment") else "OpeningWorldDesign"
    validate_contract(name, design, "production_profile")
    return design, {"prior_design_ref": designer["candidate_ref"], "review_ref": designer["review_ref"],
                    "response_ref": audit["response_ref"], "ordered_stages": stages, "chapter_stage_counts": sizes,
                    "unresolved_questions": envelope["unresolved_questions"]}


def replay_fix(kernel, repo, wr, designer, audit, envelope, assignment, review, prior):
    permission = designer["editor_decision"]
    decision = kernel.artifact(repo, permission["decision_ref"], wr["work_id"], kind="ENDING_FIX_AUTHORIZATION")["payload"]
    issues = [issue["issue_id"] for issue in editorial_blockers(review["issues"])]
    require(decision == {k: v for k, v in permission.items() if k != "decision_ref"}
            and issues and decision["issue_ids"] == issues and assignment["editor_decision"] == permission
            and decision["candidate_ref"] == designer["candidate_ref"] and decision["review_ref"] == designer["review_ref"],
            "INVALID_EVIDENCE", "Revision is not covered by the editor's recorded authorization")
    patch = validate_contract("ShortEndingFix", envelope["payload"], "production_profile")
    chapters = list(prior["chapter_goals"])
    opening = dict(prior["opening"])
    touched = set()
    changes = set()
    for change in patch["chapter_changes"] + patch["opening_changes"]:
        require(set(change["issue_ids"]) <= set(issues), "INVALID_EVIDENCE", "Patch cites an unassigned issue")
        touched.update(change["issue_ids"])
        key = change.get("field", change.get("chapter"))
        require(key not in changes, "INVALID_EVIDENCE", "Duplicate patch for one location")
        changes.add(key)
        if "chapter" in change:
            require(key <= len(chapters), "INVALID_EVIDENCE", "Chapter is outside the reviewed manuscript")
            chapters[key - 1] = change["replacement"]
        else:
            opening[key] = change["replacement"]
    require(touched == set(issues), "REVIEW_REQUIRED", "Patch must address each assigned issue")
    design = {**prior, "opening": opening, "chapter_goals": chapters}
    validate_contract("EnvironmentalOpeningWorldDesign" if prior["opening"].get("environment") else "OpeningWorldDesign",
                      design, "production_profile")
    prior_source = assignment["ending_plan_source"]
    return design, {"prior_design_ref": designer["candidate_ref"], "review_ref": designer["review_ref"],
                    "response_ref": audit["response_ref"], "ordered_stages": prior_source["ordered_stages"],
                    "chapter_stage_counts": prior_source["chapter_stage_counts"], "issue_ids": issues,
                    "unresolved_questions": list(dict.fromkeys(prior_source["unresolved_questions"] +
                                                              envelope["unresolved_questions"]))}


def authorize_fix(kernel, gateway, owner, wr, attempt):
    from .idea_intake import _audited_design_result, design_round, intake_receipt
    validate_contract("UUID", attempt)
    work = wr["work_id"]
    with kernel.objects.lock(), kernel.db.uow() as repo:
        repo.one("works", {"work_id": work}, lock="update")
        kernel.authorize(repo, owner, work, "ADMIN", wr["branch_id"])
        existing = kernel._prior(repo, owner, work, "P6_ENDING_FIX_AUTHORIZATION", attempt, {})
        if existing:
            return existing
        intake = intake_receipt(kernel, repo, owner, wr)
        _, designer, active = design_round(kernel, repo, owner, wr, intake)
        require(kernel.head(repo, work, wr["branch_id"]) == wr and active and
                designer.get("revision_purpose") == "SHORT_ENDING", "VIEW_DENIED",
                "Only the present ending-plan reviewer can request this correction")
        task = repo.one("tasks", {"task_id": active["task_id"]}, lock="update")
        require(task["state"] == "RESULT_STORED", "REVIEW_REQUIRED", "No completed review")
        audit, review = _audited_design_result(kernel, gateway, repo, task, wr)
        issues = editorial_blockers(review["issues"])
        require(audit["attempt_id"] == attempt and review.get("ending_plan_review", {}).get("candidate_ref") == active["candidate_ref"]
                and issues and len({i["issue_id"] for i in issues}) == len(issues),
                "REVIEW_REQUIRED", "The specified return must identify unresolved issues for the current ending")
        body = {"attempt_id": attempt, "candidate_ref": active["candidate_ref"], "review_ref": audit["output_ref"],
                "issue_ids": [i["issue_id"] for i in issues], "principal_id": owner.principal_id}
        decision = {**body, "decision_ref": _artifact(kernel, repo, work, owner, "ENDING_FIX_AUTHORIZATION", body)}
        kernel._receipt(repo, owner, wr, "P6_ENDING_FIX_AUTHORIZATION", attempt, {}, decision)
        return decision


def derive_ending_plan(kernel, repo, owner, wr, designer, envelope, audit, audit_ref):
    design, source = replay_ending_plan(kernel, repo, wr, designer, envelope, audit)
    attempt = audit["attempt_id"]
    request = {"input_ref": designer["input_ref"], "audit_ref": audit_ref, "response_ref": audit["response_ref"]}
    receipt = kernel._prior(repo, owner, wr["work_id"], "P6_ENDING_PLAN_CANDIDATE", attempt, request)
    if receipt is None:
        ref = _artifact(kernel, repo, wr["work_id"], owner, "ENDING_PLAN_CANDIDATE", design)
        receipt = {**request, "attempt_id": attempt, "source": source, "output_ref": ref}
        kernel._receipt(repo, owner, wr, "P6_ENDING_PLAN_CANDIDATE", attempt, request, receipt)
    return {**audit, "output_ref": receipt["output_ref"], "ending_plan": receipt}, design


def confirm_ending(review, source, candidate_ref):
    if source.get("issue_ids"):
        findings = review["issue_resolutions"]
        require([i["issue_id"] for i in findings] == source["issue_ids"] and
                all(i["decision"] == "RESOLVED" and i["reason"].strip() for i in findings), "REVIEW_REQUIRED",
                "The new review did not resolve every authorized correction")
    check = review["ending_plan_review"]
    answers = check["answers"]
    require(review["short_arc"]["decision"] == "COMPLETE" and check["candidate_ref"] == candidate_ref
            and check["opening_and_chapters_confirmed"]
            and len(answers) == len(source["unresolved_questions"])
            and all(answer["question"] == question and answer["answer"].strip()
                    for answer, question in zip(answers, source["unresolved_questions"])),
            "REVIEW_REQUIRED", "The independent review has not confirmed the exact opening, chapters and answers")
