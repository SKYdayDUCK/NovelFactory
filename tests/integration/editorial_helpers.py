"""Reusable real-service P2 editorial workflow; prose judgments are fixtures.

No database table inserts or forged approvals. This helper is an engineering
fixture and must never be reported as an actual model or human literary review.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from novel_factory.application.editorial import ReviewService
from novel_factory.application.narrative import NarrativeService
from novel_factory.application.queries import WorldQueryService
from novel_factory.domain.canonical import fingerprint
from novel_factory.domain.editorial import text_span
from novel_factory.fixtures import point


def uid():
    return str(uuid4())


def make_review_task(k, f, binding, mode, *, role="reviewer", principal=None, bind_read_scope=True):
    wr = binding["world_ref"]
    principal = principal or k.issue_grant(f["principal"], wr["work_id"], role_id=role,
        capabilities=["READ", "ARTIFACT_WRITE", "TASK_CREATE", "TASK_CLAIM", "COMPILE"],
        planes=["WORLD"], max_story_time=point(1000))["principal"]
    manifest = k.put_artifact(f["principal"], wr["work_id"],
        {"subject_ids": [f["ids"]["sword"]], "predicates": ["possessed_by", "lifecycle"]}, "INPUT_MANIFEST")
    tid = uid()
    spec = {"schema_version": "2.0", "task_id": tid, "generation": 0, "role_id": role,
        "task_kind": mode, "state": "READY", "world_ref": wr, "story_time": point(1000),
        "context_bundle_id": None, "policy_version": "nf-policy-1", "dependency_fingerprint": None,
        "runtime_contract_version": fingerprint("P205_EXPLICIT_ENGINEERING_FIXTURE"), "capability_grant_id": principal.grant_id,
        "input_artifacts": [manifest, *binding["ordered_draft_refs"], *binding["reconciliation_refs"],
                            *binding["style_refs"], *binding["literary_refs"]],
        "budget": {"currency": "USD", "max_amount": "1", "token_budget": 100000},
        "wall_deadline": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}
    k.create_task(principal, spec, "create:" + tid)
    task = k.claim_task(principal, "editorial:" + tid, lease_seconds=3600, task_id=tid)
    recipe = {"reviewer": "REVIEWER", "evidence_maintainer": "MAINTAINER", "writer": "WRITER"}.get(role, "ADJUDICATOR")
    WorldQueryService(k).context_compile(principal, {"schema_version": "2.1", "task_id": tid, "generation": task["generation"],
        "recipe_id": recipe, "input_manifest_ref": manifest, "read_contract": {"revision": {"mode": "FIXED", "world_ref": wr},
        "process_policy": "ALLOW_PARTIAL", "index_policy": "AUTHORITATIVE", "max_wait_ms": 0},
        "knowledge_cut": {"plane": "WORLD"}, "subject_time": {"kind": "POINT", "at": point(1000)},
        "token_budget": 100000, "idempotency_key": "compile:" + tid})
    task = k.get_task(principal, tid)
    if bind_read_scope:
        NarrativeService(k).bind_reader_task(f["principal"], tid, task["generation"], binding["ordered_draft_refs"], "readbind:" + tid)
    return principal, task


def review_request(binding, task, coverage_refs, scale, dimension, scope, *, issues=()):
    return {"schema_version": "2.1", "task_id": task["task_id"], "generation": task["generation"],
        "context_bundle_id": task["context_bundle_id"], "binding": deepcopy(binding), "scope_draft_refs": list(scope),
        "scale": scale, "dimension": dimension, "coverage_refs": list(coverage_refs), "issues": list(issues),
        "summary": "工程夹具中的明确判断，仅验证原文证据与服务边界；不宣称模型或人类文学验收。",
        "counterinterpretations": ["保留笑与悲伤并存、人物各有语域的可能性。"],
        "outcome": "PASS" if not issues else "CHANGES_REQUESTED", "idempotency_key": "review:" + uid()}


def review_binding(k, f, binding, *, literary_issues=()):
    s = ReviewService(k)
    reports, reads, tasks, requests = [], [], {}, []
    for mode in ("fact", "epistemic", "literary", "continuity"):
        principal, task = make_review_task(k, f, binding, mode)
        delivered = [s.read_text(principal, task["task_id"], task["generation"], ref,
                                idempotency_key="full:" + ref["artifact_revision_id"])
                     for ref in binding["ordered_draft_refs"]]
        refs = [item["coverage_ref"] for item in delivered]
        tasks[mode] = {"principal": principal, "task": task, "delivered": delivered}
        reads += refs
        jobs = []
        if mode == "fact":
            jobs = [("SCENE", "FACT", [ref], []) for ref in binding["ordered_draft_refs"]]
        elif mode == "epistemic":
            jobs = [("CHAPTER", "EPISTEMIC", binding["ordered_draft_refs"], [])]
        elif mode == "literary":
            jobs = [("VOLUME", "LITERARY", binding["ordered_draft_refs"], list(literary_issues))]
        elif mode == "continuity":
            jobs = [("SEAM", "CONTINUITY", [a, b], []) for a, b in zip(binding["ordered_draft_refs"], binding["ordered_draft_refs"][1:])]
        for scale, dimension, scope, issues in jobs:
            request = review_request(binding, task, refs, scale, dimension, scope, issues=issues)
            reports.append(s.submit_review(principal, request)); requests.append(request)
    return {"review_refs": reports, "coverage_refs": reads, "reader_tasks": tasks, "review_requests": requests}


def decision_request(session_ref, prior_ref, phase, binding, review_refs, functions, *, issue_decisions=()):
    return {"schema_version": "2.1", "session_ref": session_ref, "expected_prior_decision_ref": prior_ref,
        "phase": phase, "after_binding": deepcopy(binding), "review_refs": list(review_refs),
        "issue_decisions": list(issue_decisions), "scene_functions": functions if phase == "STRUCTURE" else [],
        "considered_options": [{"option_id": uid(), "summary": "保留两条有差异的声线及原有次序", "disposition": "SELECTED",
            "reason": "当前证据支持并置，未见值得承担额外改写成本的收益。", "benefits": ["保留关系不对称"], "costs": ["需要读者保留未解疑问"]},
            {"option_id": uid(), "summary": "统一语言并合并场景", "disposition": "REJECTED",
             "reason": "会失去人物各自的观察距离。", "benefits": ["更短"], "costs": ["磨平多声部"]}] if phase == "STRUCTURE" else [],
        "summary": "工程fixture记录：保留或有范围调整，并将分歧连同证据存档。", "idempotency_key": "decision:" + uid()}


def approve_binding(k, f, binding, *, literary_issues=(), retain_decisions=(), approve=True):
    s = ReviewService(k)
    proof = review_binding(k, f, binding, literary_issues=literary_issues)
    session_request = {"schema_version": "2.1", "binding": deepcopy(binding), "max_rounds": 6,
                       "max_issue_decisions": 30, "idempotency_key": "session:" + uid()}
    session_ref = s.start_editing(f["principal"], session_request)
    functions = []
    with k.db.uow(write=False) as repo:
        for ref in binding["ordered_draft_refs"]:
            draft = k.artifact(repo, ref, binding["world_ref"]["work_id"], kind="NARRATIVE_DRAFT")["payload"]
            text = k.objects.read(draft["text_ref"]["sha256"]).decode("utf-8")
            functions.append({"draft_ref": ref, "function": "人物在各自观察距离内作出选择，保留尚未结束的情绪后果。",
                "choice_evidence_refs": [{"kind": "ARTIFACT", "artifact_ref": draft["text_ref"], "span": text_span(text, 0, len(text))}]})
    decisions, requests = [], []
    for phase in ("STRUCTURE", "SEAM", "LANGUAGE"):
        request = decision_request(session_ref, decisions[-1] if decisions else None, phase, binding,
            proof["review_refs"], functions, issue_decisions=retain_decisions if phase == "STRUCTURE" else [])
        decisions.append(s.record_decision(f["principal"], request)); requests.append(request)
    approval_request = {"schema_version": "2.1", "binding": deepcopy(binding), "review_refs": proof["review_refs"],
        "edit_decision_refs": decisions, "unresolved_choices": ["审美分歧只保留其明确理由，不将一致评分当文学质量。"],
        "idempotency_key": "approve:" + uid()}
    result = {**proof, "session_ref": session_ref, "session_request": session_request,
              "edit_decision_refs": decisions, "decision_requests": requests, "approval_request": approval_request}
    if approve:
        result["approval_ref"] = s.approve_editorial(f["principal"], approval_request)
    return result


def binding_for(f, accepted, *, world_ref=None):
    return {"world_ref": world_ref or f["world_ref"], "plan_ref": f["plan_ref"],
        "ordered_draft_refs": [x["draft_ref"] for x in accepted], "reconciliation_refs": [x["reconciliation_ref"] for x in accepted],
        "ordered_acceptance_refs": [x["acceptance_ref"] for x in accepted], "narrative_revision_id": f["narrative_revision_id"],
        "arrangement_version": "arrangement-1", "disclosure_revision_id": uid(), "style_refs": [], "literary_refs": []}
