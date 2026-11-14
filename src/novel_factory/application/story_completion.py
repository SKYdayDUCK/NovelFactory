"""Story-driven continuation on the exact accepted manuscript and approved plans.

This is an editorial judgment, never a word-count calculation or world effect.
The ordinary editorial approval and independent final blind readings still apply.
"""

from ..contracts.registry import validate_contract
from ..domain.canonical import fingerprint
from .kernel import require
from .planning import PlanningService
from .production_pipeline import _artifact, _current_owner
from .volume_successor import _selected


def _key(branch):
    return "STORY_COMPLETION:" + branch


def intake_source(kernel, repo, owner, cursor):
    return kernel._prior(repo, owner, cursor["world_ref"]["work_id"], "P6_IDEA_PROVENANCE", "initial", {})


def source_packet(kernel, repo, owner, cursor, binding):
    """Freeze the approved story trajectory and every preceding released passage."""
    wr = binding["world_ref"]
    source = intake_source(kernel, repo, owner, cursor)
    require(source, "VIEW_DENIED", "Story continuation requires the registered native story design")
    plan_ref = binding["plan_ref"]
    design_ref = source["design_ref"]
    design = kernel.artifact(repo, design_ref, wr["work_id"], kind="INITIAL_STORY_DESIGN")["payload"]
    commitments = [{"source_ref": design_ref, "source_path": "/storyline", "commitment": design["storyline"]}]
    commitments += [{"source_ref": design_ref, "source_path": f"/chapter_goals/{index}", "commitment": goal}
                    for index, goal in enumerate(design["chapter_goals"])]
    plans = set()

    def include_plan(ref):
        if ref["artifact_revision_id"] in plans:
            return
        plans.add(ref["artifact_revision_id"])
        plan = kernel.artifact(repo, ref, wr["work_id"], kind="PLAN_REVISION")["payload"]
        commitments.extend({"source_ref": ref, "source_path": f"/nodes/{index}/goal", "commitment": node["goal"]}
                           for index, node in enumerate(plan["nodes"]))
        contract_ref = plan["volume_contract_ref"]
        contract = kernel.artifact(repo, contract_ref, wr["work_id"], kind="VOLUME_CONTRACT")["payload"]
        commitments.extend({"source_ref": contract_ref, "source_path": f"/protected_commitments/{index}/description",
                            "commitment": item["description"]}
                           for index, item in enumerate(contract["protected_commitments"]))

    include_plan(plan_ref)
    previous = []
    for release_ref in cursor["release_refs"]:
        record = kernel.artifact(repo, release_ref, wr["work_id"], kind="RELEASE_RECORD")["payload"]
        require(_selected(repo, wr["work_id"], "RELEASE:" + record["volume_id"]) == release_ref,
                "RELEASE_SELECTION_CONFLICT", "An earlier story volume release changed")
        include_plan(record["plan_ref"])
        manifest = kernel.artifact(repo, record["manifest_ref"], wr["work_id"], kind="RELEASE_MANIFEST")["payload"]
        require(manifest["world_ref"]["branch_id"] == wr["branch_id"], "VIEW_DENIED", "Foreign story release")
        for item in manifest["ordered_artifacts"]:
            ref = item["artifact_ref"]
            kernel.artifact(repo, ref, wr["work_id"], kind="TEXT")
            previous.append({"release_ref": release_ref, "text_ref": ref,
                             "text": kernel.objects.read(ref["sha256"]).decode("utf-8")})
    return {"version": 1, "run_id": cursor["run_id"], "checkpoint_ref": cursor["last_checkpoint"],
            "binding": binding, "commitments": commitments, "previous_passages": previous,
            "instruction": "先阅读全部已发布前文及本次所有原稿。依据原计划的剧情承诺判断是否已实际完成故事。"
                           "开放结局可以完整；不能因字数、章数、个人偏好或疑似矛盾要求续写。"}


def record(kernel, repo, owner, cursor, state, task, evidence, output):
    """Only the normal, audited task delivery can create a completion decision."""
    validate_contract("StoryCompletionAdvice", output, "production_profile")
    wr = state["binding"]["world_ref"]
    packet_ref = state["jobs"][state["index"]]["story_sources_ref"]
    packet = kernel.artifact(repo, packet_ref, wr["work_id"], kind="STORY_COMPLETION_SOURCES")["payload"]
    require(packet["binding"] == state["binding"] and packet["run_id"] == cursor["run_id"]
            and packet["checkpoint_ref"] == cursor["last_checkpoint"]
            and task["role_id"] == "chief_editor" and task["task_kind"] == "editorial_decision"
            and kernel.artifact(repo, evidence["output_ref"], wr["work_id"], kind="AGENT_CANDIDATE")["payload"] == output,
            "INVALID_EVIDENCE", "Story judgment belongs to another frozen manuscript")
    findings = output["unfulfilled_commitments"]
    require(bool(findings) == (output["decision"] == "CONTINUE"), "INVALID_EVIDENCE",
            "Continuation requires an evidenced unfinished story commitment")
    seen = set()
    for finding in findings:
        source = {key: finding[key] for key in ("source_ref", "source_path", "commitment")}
        require(source in packet["commitments"] and finding["draft_ref"] in state["binding"]["ordered_draft_refs"],
                "INVALID_EVIDENCE", "Continuation is not grounded in this approved plan and manuscript")
        draft = kernel.artifact(repo, finding["draft_ref"], wr["work_id"], kind="NARRATIVE_DRAFT")["payload"]
        prose = kernel.objects.read(draft["text_ref"]["sha256"]).decode("utf-8")
        require(prose.count(finding["quote"]) == 1 and fingerprint(source) not in seen,
                "INVALID_EVIDENCE", "Unfinished story evidence must locate one original passage without repetition")
        seen.add(fingerprint(source))
    data = {"version": 1, "run_id": cursor["run_id"], "checkpoint_ref": cursor["last_checkpoint"],
            "binding": state["binding"], "sources_ref": packet_ref, "task_id": task["task_id"],
            "generation": task["generation"], "attempt_id": evidence["attempt_id"],
            "candidate_ref": evidence["output_ref"], "advice": output}
    ref = _artifact(kernel, repo, wr["work_id"], owner, "STORY_COMPLETION_DECISION", data)
    PlanningService(kernel)._select(repo, wr, _key(wr["branch_id"]), ref)
    kernel._receipt(repo, owner, wr, "STORY_COMPLETION", task["task_id"],
                    {"candidate_ref": evidence["output_ref"], "sources_ref": packet_ref}, {"decision_ref": ref})
    return ref


def current_decision(kernel, repo, cursor):
    """A judgment expires when its accepted chapter prefix or actual edition changes."""
    wr = cursor["world_ref"]
    ref = _selected(repo, wr["work_id"], _key(wr["branch_id"]))
    if not ref:
        return None
    data = kernel.artifact(repo, ref, wr["work_id"], kind="STORY_COMPLETION_DECISION")["payload"]
    if data["run_id"] != cursor["run_id"] or data["checkpoint_ref"] != cursor["last_checkpoint"]:
        return None
    from .frozen_editing import _state

    state = _state(kernel, repo, wr["work_id"], wr["branch_id"])
    if not state or state["binding"] != data["binding"]:
        return None
    require(state.get("story_completion_ref") == ref,
            "INVALID_EVIDENCE", "Story decision is not selected for the exact accepted edition")
    task = repo.one("tasks", {"task_id": data["task_id"], "work_id": wr["work_id"], "branch_id": wr["branch_id"]})
    owner = _current_owner(kernel, repo, cursor)
    receipt = kernel._prior(repo, owner, wr["work_id"], "STORY_COMPLETION", data["task_id"],
                            {"candidate_ref": data["candidate_ref"], "sources_ref": data["sources_ref"]})
    require(task and task["role_id"] == "chief_editor" and task["task_kind"] == "editorial_decision"
            and task["state"] == "ACCEPTED" and task["generation"] == data["generation"]
            and kernel.artifact(repo, data["candidate_ref"], wr["work_id"], kind="AGENT_CANDIDATE")["payload"] == data["advice"]
            and receipt == {"decision_ref": ref}, "INVALID_EVIDENCE", "Story judgment lacks formal accepted delivery")
    return {"decision_ref": ref, **data}


def require_complete(kernel, repo, cursor):
    owner = _current_owner(kernel, repo, cursor)
    if intake_source(kernel, repo, owner, cursor):
        decision = current_decision(kernel, repo, cursor)
        require(decision and decision["advice"]["decision"] == "COMPLETE", "STORY_COMPLETION_REQUIRED",
                "The exact final manuscript needs an independent, source-backed story-completion judgment")
