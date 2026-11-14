"""Authorized independent reviews and editorial decisions on the frozen volume."""

import itertools
import json
from datetime import datetime
from uuid import UUID, uuid5

from ..domain.canonical import canonical_bytes, fingerprint
from ..domain.editorial import text_span
from ..domain.errors import DomainError
from .authorization import Principal
from .editorial import ReviewService
from .idea_intake import _audited_design_result
from .kernel import require
from .narrative import NarrativeService
from .planning import PlanningService
from .production_pipeline import _artifact, _current_owner
from .quality_memory import PAUSED, plan_repair, reflect_repair
from .rolling_production import _load, _window
from .runtime_tasks import RuntimeTaskService
from .volume_successor import _selected


def _state(kernel, repo, work, branch):
    ref = _selected(repo, work, "P6_FROZEN_EDITING:" + branch)
    return kernel.artifact(repo, ref, work, kind="P6_FROZEN_EDITING")["payload"] if ref else None


def _save(kernel, repo, owner, state):
    wr = state["binding"]["world_ref"]
    ref = _artifact(kernel, repo, wr["work_id"], owner, "P6_FROZEN_EDITING", state)
    PlanningService(kernel)._select(repo, wr, "P6_FROZEN_EDITING:" + wr["branch_id"], ref)
    return {
        "status": state["status"],
        "state_ref": ref,
        "reports": len(state["reports"]),
        "decisions": len(state["decisions"]),
        "active_task": state["active"],
        "approval_ref": state.get("approval_ref"),
        "reason": state.get("reason"),
        **({"edition_ref": state["edition_ref"]} if "edition_ref" in state else {}),
    }


def start(kernel, owner, work, branch):
    """Enroll only this frozen volume, spending exclusively against its existing rolling allowance."""
    with kernel.objects.lock(), kernel.db.uow() as repo:
        repo.one("works", {"work_id": work}, lock="update")
        kernel.authorize(repo, owner, work, "ADMIN", branch)
        cursor = _load(kernel, repo, work, branch)
        require(
            cursor and cursor["state"] == "FROZEN", "RELEASE_INCOMPLETE", "No frozen volume to edit"
        )
        require(
            _current_owner(kernel, repo, cursor) == owner,
            "VIEW_DENIED",
            "Only the enrolled production owner can authorize editing",
        )
        volume = _window(kernel, repo, cursor)["volume_id"]
        if cursor["index"] + 1 < cursor["window_count"]:
            following = kernel.artifact(
                repo, cursor["windows"][cursor["index"] + 1], work, kind="ROLLING_WINDOW"
            )["payload"]
            require(
                following["volume_id"] != volume,
                "RELEASE_INCOMPLETE",
                "This volume has unfrozen chapters",
            )
        if cursor["index"] + 1 == cursor["window_count"]:
            require(
                cursor["frozen_chapters"] == cursor["target_chapters"],
                "RELEASE_INCOMPLETE",
                "Future chapters are still authorized",
            )
        wr = kernel.head(repo, work, branch)
        from .rolling_production import revalidate_frozen_volume

        scenes = revalidate_frozen_volume(kernel.in_transaction(repo), repo, owner, cursor, volume, wr)
        releases = [s["acceptance_ref"] for s in scenes]
        previous = _state(kernel, repo, work, branch)
        if (
            previous
            and previous["binding"]["ordered_acceptance_refs"] == releases
            and previous["binding"]["world_ref"] == wr
        ):
            return {
                "status": previous["status"],
                "active_task": previous["active"],
                "approval_ref": previous.get("approval_ref"),
            }
        require(
            not previous or previous["status"] not in {"EDITING_ACTIVE", "EDITION_REVISING"},
            "CAPACITY_LIMIT",
            "An earlier editorial session remains active",
        )
        first = kernel.artifact(repo, scenes[0]["scene_ref"], work, kind="SCENE_PLAN")["payload"][
            "scene_contract"
        ]
        accepted = [
            kernel.artifact(repo, r, work, kind="NARRATIVE_ACCEPTANCE")["payload"] for r in releases
        ]
        binding = {
            "world_ref": wr,
            "plan_ref": _selected(repo, work, PlanningService.selection_key(volume)),
            "ordered_draft_refs": [s["draft_ref"] for s in scenes],
            "ordered_acceptance_refs": releases,
            "reconciliation_refs": [a["reconciliation_ref"] for a in accepted],
            "narrative_revision_id": first["narrative_revision_id"],
            "arrangement_version": first["arrangement_version"],
            "style_refs": first["style_refs"],
            "literary_refs": [],
            "disclosure_revision_id": str(uuid5(UUID(volume), fingerprint(releases))),
        }
        bound = kernel.in_transaction(repo)
        review = ReviewService(bound)
        review._binding(repo, binding, require_acceptances=True)
        session = review.start_editing(
            owner,
            {
                "schema_version": "2.1",
                "binding": binding,
                "max_rounds": 3,
                "max_issue_decisions": 100,
                "idempotency_key": fingerprint(binding),
            },
        )
        # Check actual chapter boundaries, including plans containing multiple scenes per chapter.
        chapters, checkpoint = [], cursor["last_checkpoint"]
        while checkpoint:
            window = kernel.artifact(repo, checkpoint, work, kind="ROLLING_CHECKPOINT")["payload"]
            if window["volume_id"] != volume:
                break
            chapters[:0] = [[s["draft_ref"] for s in c["scenes"]] for c in window["chapters"]]
            checkpoint = window["previous"]
        drafts = binding["ordered_draft_refs"]
        jobs = [{"kind": "fact", "scale": "SCENE", "scope": [r]} for r in drafts]
        jobs += [{"kind": "epistemic", "scale": "CHAPTER", "scope": group} for group in chapters]
        jobs += [
            {"kind": "continuity", "scale": "SEAM", "scope": [a, b]}
            for a, b in itertools.pairwise(drafts)
        ]
        jobs += [{"kind": "literary", "scale": "VOLUME", "scope": drafts}]
        jobs += [
            {"phase": phase, "scope": drafts}
            for phase in ("STRUCTURE", "SEAM", "LANGUAGE", "APPROVAL")
        ]
        state = {
            "status": "EDITING_ACTIVE",
            "binding": binding,
            "volume": volume,
            "run": cursor["run_id"],
            "session": session,
            "jobs": jobs,
            "reports": [],
            "decisions": [],
            "receipts": [],
            "active": None,
            "index": 0,
            "book_rework_history": previous.get("book_rework_history", []) if previous else [],
        }
        return _save(bound, repo, owner, state)


def _assign(kernel, repo, gateway, owner, state, cursor):
    from .supervisor import RuntimeSupervisor

    job = state["jobs"][state["index"]]
    binding = state["binding"]
    wr = binding["world_ref"]
    phase = job.get("phase")
    role = "chief_editor" if phase else "reviewer"
    kind = (
        ("release_review" if phase == "APPROVAL" else "editorial_decision")
        if phase
        else job["kind"]
    )
    assignment = {
        "phase": phase or job["scale"],
        "dimension": job.get("kind"),
        "instructions": "独立阅读每份给定原文并核查本维度。列出具体可定位的问题及原文证据；证据不足请返回疑问。",
    }
    if phase:
        assignment["reviews"] = [
            kernel.artifact(repo, r, wr["work_id"], kind="REVIEW_REPORT")["payload"]["submission"]
            for r in state["reports"]
        ]
        # The model needs actual judgments, not repeated copies of the full signed binding.
        assignment["reviews"] = [
            {
                k: r[k]
                for k in (
                    "scale",
                    "dimension",
                    "summary",
                    "issues",
                    "counterinterpretations",
                    "outcome",
                )
            }
            for r in assignment["reviews"]
        ]
        assignment["prior_decisions"] = [
            kernel.artifact(repo, r, wr["work_id"], kind="EDITORIAL_DECISION")["payload"][
                "request"
            ]["summary"]
            for r in state["decisions"]
        ]
        if phase == "APPROVAL":
            assignment["instructions"] = (
                "通读原文、正式分维度审核与三阶段编辑决定。给出最终编辑批准 APPROVED 或退回 RETURNED，并提供理由。"
            )
        elif phase == "BOOK_REWORK":
            assignment["instructions"] = (
                "整书双路盲评未通过。亲自通过MCP读取原稿及两份评分，逐条核对低分和否决项的原句及当前版本。"
                "decision填写RETURNED，reason给出明确且有证据的退修方法、需保留部分及逐场修改范围，"
                "证据不足的意见只说明不能证实，不能编造矛盾；不写正文。deferred_questions仅用于真实作者决策。"
            )
            assignment["book_review_refs"] = job["review_refs"]
            gateway.output_contracts[(role, kind)] = ("AgentDecisionOutput", "agent")
        else:
            assignment["instructions"] = (
                "独立审议本阶段，仅原文可维持时决定 APPROVED，需改写则 RETURNED。STRUCTURE 按原文顺序填写每场 scene（从1起）、功能和唯一的原句 quote，提供至少两种有代价的选项，仅选一种。余阶段上述两数组留空。逐项裁定所列问题，不重复前一阶段处置。"
            )
            gateway.output_contracts[(role, kind)] = ("FrozenPhaseAdvice", "production_profile")
    cut = {"plane": "WORLD"}
    extra = []
    if phase:
        manifest = _selected(repo, wr["work_id"], "PLAN_MANIFEST:" + state["volume"])
        require(manifest, "STALE_PLAN", "Editors require the current plan manifest")
        cut = {"plane": "PLAN", "plan_manifest_ref": manifest}
        extra.append(manifest)
    correction = state.get("technical_correction")
    if correction:
        assignment.update(repair_strategy=correction["method"], validation_errors=[correction["diagnostic"]],
                          source_attempt_id=correction["attempt_id"])
    auth = cursor["allowance"]
    seconds = int(
        (datetime.fromisoformat(cursor["authorization"]["deadline"]) - repo.now()).total_seconds()
    )
    kwargs = {
        "adapter_id": auth["routes"]["review"],
        "subject_ids": auth["subject_ids"],
        "predicates": auth["predicates"],
        "story_time": auth["story_time"],
        "input_artifacts": [*binding["ordered_draft_refs"], *extra],
        "knowledge_cut": cut,
        "knowledge_reread_reason": "known_error" if correction else None,
        "material_refs": [*job["scope"], *job.get("review_refs", [])],
        "max_amount": auth["reserve_amount"],
        "token_budget": auth["token_reservation"],
        "currency": repo.one("budgets", {"budget_id": auth["budget_id"]})["currency"],
        "queue_state": "READY",
        "deadline_seconds": seconds,
        "lease_seconds": min(600, seconds),
    }
    prepared = RuntimeTaskService(kernel, gateway).prepare(
        owner,
        wr,
        role_id=role,
        task_kind=kind,
        review_belief=role == "reviewer" and kind == "epistemic",
        assignment=json.dumps(assignment, ensure_ascii=False),
        **kwargs,
    )
    if correction and "runtime_sha256" in correction:
        new_input = kernel.artifact(repo, prepared.agent_input["input_ref"], wr["work_id"], kind="AGENT_INPUT")["payload"]
        require(all(new_input[key] == correction[key] for key in
                    ("runtime_sha256", "model_config_sha256", "output_contract")),
                "TASK_REBIND_REQUIRED", "Editor format correction cannot change the role, model or contract")
    sup = RuntimeSupervisor(kernel, gateway)
    config, _ = sup.dispatch_config(
        repo,
        prepared.spec,
        repo.one("grants", {"grant_id": prepared.principal.grant_id}),
        manifest_ref=prepared.manifest_ref,
        knowledge_cut=cut,
        context_tokens=20000,
        input_ref=prepared.agent_input["input_ref"],
        budget_id=auth["budget_id"],
        reserve_amount=auth["reserve_amount"],
        token_reservation=auth["token_reservation"],
    )
    config["frozen_editing"] = {
        "session_ref": state["session"],
        "index": state["index"],
        "scope": job["scope"],
        **({"review_refs": job["review_refs"]} if job.get("review_refs") else {}),
    }
    frozen_input = kernel.artifact(repo, prepared.agent_input["input_ref"], wr["work_id"], kind="AGENT_INPUT")["payload"]
    if gateway.adapters[frozen_input["adapter_id"]].configuration.get("auth_mode") == "host-login":
        config["required_review_mcp"] = True
    sup.install_job(repo, prepared.task, config)
    state["active"] = prepared.task["task_id"]


def _current_job(kernel, repo, task, mark):
    state = _state(kernel, repo, task["work_id"], task["branch_id"])
    require(
        state
        and state["active"] == task["task_id"]
        and state["session"] == mark["session_ref"]
        and state["index"] == mark["index"]
        and state["status"] == "EDITING_ACTIVE"
        and state["jobs"][state["index"]]["scope"] == mark["scope"],
        "TASK_REBIND_REQUIRED",
        "Frozen editing task is no longer selected",
    )
    return state


def prepare_read(kernel, task, mark):
    with kernel.objects.lock(), kernel.db.uow() as repo:
        state = _current_job(kernel, repo, task, mark)
        cursor = _load(kernel, repo, task["work_id"], task["branch_id"])
        controller = _current_owner(kernel, repo, cursor)
        require(
            cursor["run_id"] == state["run"]
            and kernel.head(repo, task["work_id"], task["branch_id"])
            == state["binding"]["world_ref"],
            "TASK_REBIND_REQUIRED",
            "Frozen editorial cut changed",
        )
        NarrativeService(kernel.in_transaction(repo)).bind_reader_task(
            controller,
            task["task_id"],
            task["generation"],
            mark["scope"],
            "frozen-read:" + task["task_id"],
        )


def material(kernel, principal, task, ref, mark):
    with kernel.db.uow(write=False) as repo:
        _current_job(kernel, repo, task, mark)
    if ref in mark.get("review_refs", []):
        from .book_rework import material as book_material
        return book_material(kernel, principal, task, ref, mark)
    require(ref in mark["scope"], "VIEW_DENIED", "Draft outside frozen editing scope")
    delivered = ReviewService(kernel).read_text(
        principal,
        task["task_id"],
        task["generation"],
        ref,
        idempotency_key="frozen-coverage:" + ref["sha256"],
    )
    return canonical_bytes(delivered)


def _deliver(kernel, repo, owner, state, task, proof, output):
    wr = state["binding"]["world_ref"]
    job = state["jobs"][state["index"]]
    actor = Principal(task["grant_id"], task["principal_id"])
    key = task["task_id"] + ":" + str(task["generation"])
    review = ReviewService(kernel)
    state["receipts"].append(
        {
            "task_id": task["task_id"],
            "attempt_id": proof["attempt_id"],
            "candidate_ref": proof["output_ref"],
        }
    )
    covers = []
    for r in job["scope"]:
        read = {
            "task_id": task["task_id"],
            "generation": task["generation"],
            "draft_ref": r,
            "start": None,
            "end": None,
            "read_mode": "FULL_TEXT",
        }
        previous = kernel._prior(
            repo, actor, wr["work_id"], "EDITORIAL_READ", "frozen-coverage:" + r["sha256"], read
        )
        require(previous, "READING_COVERAGE_INCOMPLETE", "Missing actual task delivery")
        covers.append(previous["coverage_ref"])
    phase = job.get("phase")
    if phase == "BOOK_REWORK":
        require(output["decision"] == "RETURNED" and output["reason"].strip() and not output["deferred_questions"],
                "INVALID_EVIDENCE", "Book revision needs a concrete editor decision against the failed round")
        kernel.tasks.accept_in_transaction(repo.conn, task["task_id"], task["generation"])
        state.update(index=state["index"] + 1, active=None, status="EDITING_RETURNED")
        from .returned_edition import begin
        begin(kernel, repo, owner, state, _load(kernel, repo, wr["work_id"], wr["branch_id"]), task, proof, output)
        return
    if not phase:
        report = review.submit_review(
            actor,
            {
                "schema_version": "2.1",
                "task_id": task["task_id"],
                "generation": task["generation"],
                "context_bundle_id": task["context_bundle_id"],
                "binding": state["binding"],
                "scope_draft_refs": job["scope"],
                "scale": job["scale"],
                "dimension": job["kind"].upper(),
                "coverage_refs": covers,
                "agent_output_ref": proof["output_ref"],
                **{k: output[k] for k in ("issues", "summary", "counterinterpretations")},
                "outcome": "CHANGES_REQUESTED" if output["issues"] else "PASS",
                "idempotency_key": key,
            },
        )
        state["reports"].append(report)
    elif output["decision"] != "APPROVED" or (phase == "APPROVAL" and output["deferred_questions"]):
        state.update(
            status="EDITING_RETURNED", reason="EDITOR_RETURNED", returned_ref=proof["output_ref"]
        )
    elif phase == "APPROVAL":
        state["approval_ref"] = review.approve_editorial(
            owner,
            {
                "schema_version": "2.1",
                "binding": state["binding"],
                "review_refs": state["reports"],
                "edit_decision_refs": state["decisions"],
                "unresolved_choices": [],
                "idempotency_key": key,
            },
        )
        state["status"] = "EDITORIAL_APPROVED"
    else:
        functions = []
        for i, row in enumerate(output["scene_functions"]):
            require(
                i < len(job["scope"]) and row["scene"] == i + 1,
                "INVALID_EVIDENCE",
                "Scene order changed",
            )
            draft_ref = job["scope"][i]
            draft = kernel.artifact(repo, draft_ref, wr["work_id"], kind="NARRATIVE_DRAFT")[
                "payload"
            ]
            prose = kernel.objects.read(draft["text_ref"]["sha256"]).decode()
            quote = row["quote"]
            require(
                prose.count(quote) == 1,
                "INVALID_EVIDENCE",
                "Scene-function quotation must be unique in its original",
            )
            start = prose.index(quote)
            functions.append(
                {
                    "draft_ref": draft_ref,
                    "function": row["function"],
                    "choice_evidence_refs": [
                        {
                            "kind": "ARTIFACT",
                            "artifact_ref": draft["text_ref"],
                            "span": text_span(prose, start, start + len(quote)),
                        }
                    ],
                }
            )
        options = [
            {**o, "option_id": str(uuid5(UUID(task["task_id"]), str(i)))}
            for i, o in enumerate(output["considered_options"])
        ]
        state["decisions"].append(
            review.record_decision(
                owner,
                {
                    "schema_version": "2.1",
                    "session_ref": state["session"],
                    "expected_prior_decision_ref": state["decisions"][-1]
                    if state["decisions"]
                    else None,
                    "phase": phase,
                    "after_binding": state["binding"],
                    "review_refs": state["reports"],
                    "issue_decisions": output["issue_decisions"],
                    "scene_functions": functions,
                    "considered_options": options,
                    "summary": output["summary"],
                    "idempotency_key": key,
                },
            )
        )
    kernel.tasks.accept_in_transaction(repo.conn, task["task_id"], task["generation"])
    state.pop("technical_correction", None)
    state.update(index=state["index"] + 1, active=None)


def advance(kernel, gateway, controller, work, branch):
    with kernel.db.uow(write=False) as repo:
        state = _state(kernel, repo, work, branch)
        if not state or state["status"] not in {"EDITING_ACTIVE", "EDITION_REVISING", "EDITION_ACCEPTED"}:
            return None
    if state["status"] == "EDITION_ACCEPTED":
        return start(kernel, controller, work, branch)
    with kernel.objects.lock(), kernel.db.uow() as repo:
        repo.one("works", {"work_id": work}, lock="update")
        kernel.authorize(repo, controller, work, "ADMIN", branch)
        state = _state(kernel, repo, work, branch)
        if state["status"] not in {"EDITING_ACTIVE", "EDITION_REVISING"}:
            return None
        bound = kernel.in_transaction(repo)
        cursor = _load(bound, repo, work, branch)
        owner = _current_owner(bound, repo, cursor)
        require(
            cursor["run_id"] == state["run"]
            and bound.head(repo, work, branch) == state["binding"]["world_ref"],
            "TASK_REBIND_REQUIRED",
            "Frozen state changed",
        )
        task = proof = None
        try:
            with repo.conn.begin_nested():
                if state["status"] == "EDITION_REVISING":
                    from .returned_edition import advance as revise_returned
                    if not revise_returned(bound, repo, gateway.with_kernel(bound), owner, state, cursor):
                        return None
                elif state["active"]:
                    task = repo.one("tasks", {"task_id": state["active"]})
                    if task["state"] == "FAILED":
                        from .frozen_format_recovery import recover
                        recover(bound, repo, gateway, owner, state, task)
                        return _save(bound, repo, owner, state)
                    if task["state"] in {"PAUSED", "STALE", "CANCELLED", "WAITING_REVIEW"}:
                        state.update(status="EDITING_BLOCKED", reason="TASK_" + task["state"])
                        return _save(bound, repo, owner, state)
                    if task["state"] != "RESULT_STORED":
                        return None
                    proof, output = _audited_design_result(bound, gateway.with_kernel(bound), repo, task, state["binding"]["world_ref"])
                    _deliver(bound, repo, owner, state, task, proof, output)
                    if state["status"] == "EDITING_RETURNED" and cursor["profile"].get("production_mode") == "quality":
                        from .returned_edition import begin
                        begin(bound, repo, owner, state, cursor, task, proof, output)
                else:
                    _assign(bound, repo, gateway.with_kernel(bound), owner, state, cursor)
        except DomainError as error:
            if error.code == "CALL_RECONCILIATION_REQUIRED":
                if task and task["state"] == "FAILED":
                    state = _state(bound, repo, work, branch)
                    state.update(status="EDITING_BLOCKED", reason=error.code)
                    return _save(bound, repo, owner, state)
                return None
            state = _state(bound, repo, work, branch)
            index = str(state["index"])
            repairs_used = sum(len(state.get(name, {}).get(index, [])) for name in
                               ("format_history", "evidence_repairs", "technical_history"))
            if repairs_used >= 6:
                state.update(status="EDITING_BLOCKED", reason="TECHNICAL_REPAIR_BUDGET_EXHAUSTED")
                return _save(bound, repo, owner, state)
            from .review_recovery import ERRORS, recover_review
            if task and error.code in ERRORS and state["active"] == task["task_id"]:
                history = state.setdefault("evidence_repairs", {}).setdefault(str(state["index"]), [])
                review_node = {"task_id": task["task_id"], "state": "ENROLLED", "evidence_repairs": history}
                if recover_review(bound, repo, gateway, review_node, task, error) and review_node["state"] == "PENDING":
                    state.update(active=None, technical_correction={"method": history[-1]["method"],
                        "diagnostic": {"code": error.code}, "attempt_id": history[-1]["attempt_id"]})
                else:
                    state.update(status="EDITING_BLOCKED", reason=review_node.get("reason", error.code))
            elif (cursor["profile"].get("production_mode") == "quality" and proof and state["active"] == task["task_id"]
                    and error.code in {"INVALID_EVIDENCE", "INVALID_ARGUMENT", "INVALID_MODEL_OUTPUT"}):
                # Semantic validation of a *completed* structured return failed. The exact
                # candidate and transport/accounting evidence must still pass native delivery.
                history = state.setdefault("technical_history", {}).setdefault(str(state["index"]), [])
                issue = {"kind": error.code, "constraint": error.safe_message}
                chosen = reflect_repair(bound, repo, owner, state["binding"]["world_ref"], task["task_id"],
                                        proof["attempt_id"], proof["output_ref"], plan_repair("schema", issue, history),
                                        "FROZEN_EDITORIAL_CONTRACT")
                history.append(chosen)
                if chosen["decision"] == PAUSED:
                    state.update(status="EDITING_BLOCKED", reason=PAUSED)
                else:
                    state.update(active=None, technical_correction={**chosen, "diagnostic": issue,
                                                                  "attempt_id": proof["attempt_id"]})
            else:
                state.update(status="EDITION_REVISION_BLOCKED" if state["status"] == "EDITION_REVISING" else "EDITING_BLOCKED",
                             reason=error.code)
        return _save(bound, repo, owner, state)


def status(kernel, owner, work, branch):
    with kernel.db.uow(write=False) as repo:
        kernel.authorize(repo, owner, work, "ADMIN", branch)
        state = _state(kernel, repo, work, branch)
        if not state:
            return {"status": "NOT_ENROLLED"}
        return {
            key: state.get(key)
            for key in ("status", "active", "index", "reason", "approval_ref", "receipts", "returned_ref", "edition_ref")
        }
