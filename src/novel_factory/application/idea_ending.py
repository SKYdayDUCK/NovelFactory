"""Compose a returned ending plan with its own frozen opening for a fresh, explicit review."""
from novel_factory.contracts.registry import validate_contract
from novel_factory.domain.canonical import strict_json_loads
from .kernel import require
from .production_pipeline import _artifact


def replay_ending_plan(kernel, repo, wr, designer, envelope, audit):
    require(designer.get("revision_purpose") == "SHORT_ENDING", "VIEW_DENIED", "No authorized ending revision")
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
            and assignment["review_ref"] == designer["review_ref"] and assignment["short_arc"] == review["short_arc"]
            and review["short_arc"]["decision"] == "ENDING_PLAN_REQUIRED", "INVALID_EVIDENCE",
            "Ending revision lost its source design or prior independent ruling")
    raw = envelope["payload"]
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
    check = review["ending_plan_review"]
    answers = check["answers"]
    require(review["short_arc"]["decision"] == "COMPLETE" and check["candidate_ref"] == candidate_ref
            and check["opening_and_chapters_confirmed"]
            and len(answers) == len(source["unresolved_questions"])
            and all(answer["question"] == question and answer["answer"].strip()
                    for answer, question in zip(answers, source["unresolved_questions"])),
            "REVIEW_REQUIRED", "The independent review has not confirmed the exact opening, chapters and answers")
