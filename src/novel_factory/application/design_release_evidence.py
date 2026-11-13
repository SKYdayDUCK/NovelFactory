"""Read-only verification of the adopted design, its revision chain and its independent review."""

from ..domain.canonical import fingerprint, strict_json_loads
from .idea_repair import normalized_payload, returned_design
from .kernel import require
from .postpaid_delivery import verify_postpaid_transport
from .statistics import is_fixture_configuration


def _check(value):
    require(
        value,
        "INVALID_EVIDENCE",
        "The edition does not match its recorded design and review lineage",
    )


def _receipt(repo, source, command, key):
    row = repo.one(
        "receipts",
        {
            "work_id": source["work_id"],
            "branch_id": source["branch_id"],
            "command_type": command,
            "idempotency_key": key,
        },
    )
    _check(row)
    return row["payload"]


def _task(repo, source, record, role):
    task = repo.one(
        "tasks",
        {
            "task_id": record["task_id"],
            "work_id": source["work_id"],
            "branch_id": source["branch_id"],
            "role_id": role,
        },
    )
    _check(task)
    return task


def _assignment(kernel, repo, source, record):
    bound = kernel.artifact(repo, record["input_ref"], source["work_id"], kind="AGENT_INPUT")
    return strict_json_loads(bound["payload"]["assignment"])


def _returned(
    kernel, repo, delivery, task, source, record, *, structural_source=False, ending_plan_read=False
):
    envelope, audit, audit_ref = returned_design(
        kernel, repo, delivery.gateway, task, source, record
    )
    call = repo.one("call_attempts", {"attempt_id": audit["attempt_id"]})
    _check(
        audit["execution_kind"] in {"REAL_EXTERNAL", "REAL_HOST"}
        and not is_fixture_configuration(audit["model_configuration"])
    )
    if call["state"] == "UNKNOWN":
        verify_postpaid_transport(
            kernel,
            repo,
            task,
            call,
            kernel.artifact(repo, audit_ref, source["work_id"]),
            kernel.artifact(repo, audit["response_ref"], source["work_id"]),
            structural_source=structural_source,
            ending_plan_read=ending_plan_read,
        )
    else:
        _check(
            call["state"] == "FAILED"
            and call["actual_amount"] is not None
            and call["actual_tokens"] is not None
        )
    return envelope, audit, audit_ref


def _ending_output(kernel, repo, delivery, source, record, task, selected_ref):
    from .idea_ending import replay_ending_plan
    from .production_extent import _real_candidate

    _check(record.get("revision_purpose") in {"SHORT_ENDING", "SHORT_ENDING_FIX"})
    if task["state"] == "FAILED":
        envelope, audit, audit_ref = _returned(
            kernel, repo, delivery, task, source, record, ending_plan_read=True
        )
    else:
        audit, _ = _real_candidate(delivery, repo, task, ending_plan_read=True)
        _check(audit["input_ref"] == record["input_ref"])
        row = delivery.gateway._read_audit(repo, audit["attempt_id"])
        audit_ref = {k: row[k] for k in ("artifact_id", "artifact_revision_id", "sha256")}
        raw = kernel.artifact(repo, audit["response_ref"], source["work_id"], kind="AGENT_RESPONSE")
        envelope = strict_json_loads(raw["payload"]["text"])
    candidate, provenance = replay_ending_plan(kernel, repo, source, record, envelope, audit)
    derived = _receipt(repo, source, "P6_ENDING_PLAN_CANDIDATE", audit["attempt_id"])
    _check(
        derived
        == {
            "attempt_id": audit["attempt_id"],
            "audit_ref": audit_ref,
            "response_ref": audit["response_ref"],
            "input_ref": record["input_ref"],
            "source": provenance,
            "output_ref": selected_ref,
        }
    )
    output = kernel.artifact(repo, selected_ref, source["work_id"], kind="ENDING_PLAN_CANDIDATE")
    _check(candidate == output["payload"] and fingerprint(candidate) == output["sha256"])
    return task, audit, candidate, derived


def _design_output(kernel, repo, delivery, source, record, selected_ref, kind):
    from .production_extent import _real_candidate

    task = _task(repo, source, record, "world_designer")
    if kind == "ENDING_PLAN_CANDIDATE":
        return _ending_output(kernel, repo, delivery, source, record, task, selected_ref)
    if task["state"] != "FAILED":
        _check(kind == "AGENT_CANDIDATE")
        audit, candidate = _real_candidate(delivery, repo, task)
        _check(audit["input_ref"] == record["input_ref"] and audit["output_ref"] == selected_ref)
        return task, audit, candidate, None
    envelope, audit, audit_ref = _returned(
        kernel, repo, delivery, task, source, record, structural_source=kind == "AGENT_RESPONSE"
    )
    if kind == "AGENT_RESPONSE":
        _check(selected_ref == audit["response_ref"])
        return task, audit, envelope["payload"], None
    _check(kind == "NORMALIZED_DESIGN_CANDIDATE")
    norm = _receipt(repo, source, "P6_DESIGN_NORMALIZED", audit["attempt_id"])
    _check(
        norm["attempt_id"] == audit["attempt_id"]
        and norm["audit_ref"] == audit_ref
        and norm["response_ref"] == audit["response_ref"]
        and norm["input_ref"] == record["input_ref"]
        and norm["output_ref"] == selected_ref
    )
    candidate = normalized_payload(kernel, repo, source, record, envelope, audit)
    adopted = kernel.artifact(
        repo, selected_ref, source["work_id"], kind="NORMALIZED_DESIGN_CANDIDATE"
    )
    _check(
        candidate is not None
        and candidate == adopted["payload"]
        and fingerprint(candidate) == adopted["sha256"]
    )
    removed = sorted(set(envelope["payload"]) - set(candidate))
    if removed == ["unresolved_questions"]:
        _check(norm["operation"] == "REMOVE_DUPLICATE_EMPTY_UNRESOLVED_QUESTIONS")
    else:
        _check(
            norm["operation"] == "REMOVE_CONTENT_FREE_DESIGN_METADATA"
            and norm.get("removed_fields") == removed
        )
    return task, audit, candidate, norm


def _review(
    kernel,
    repo,
    delivery,
    source,
    record,
    design_task,
    selected_ref,
    candidate,
    idea,
    *,
    ending_revision,
):
    from .production_extent import _real_candidate

    task = _task(repo, source, record, "reviewer")
    _check(
        record["design_task_id"] == design_task["task_id"]
        and record["candidate_ref"] == selected_ref
        and task["principal_id"] != design_task["principal_id"]
        and task["grant_id"] != design_task["grant_id"]
    )
    audit, report = _real_candidate(delivery, repo, task, design_revision_read=ending_revision)
    _check(audit["input_ref"] == record["input_ref"] and audit["world_ref"] == source)
    packet = _assignment(kernel, repo, source, record)
    _check(
        packet["candidate_ref"] == selected_ref
        and packet["candidate"] == candidate
        and packet["creative_input"] == idea
    )
    return task, audit, report


def _check_record(task, audit, record):
    _check(
        task["task_id"] == record["task_id"]
        and task["generation"] == record["generation"]
        and task["context_bundle_id"] == record["context_bundle_id"]
        and audit["attempt_id"] == record["attempt_id"]
        and audit["execution_kind"] == record["execution_kind"]
        and audit["model_configuration"]["model"] == record["model"]
    )


def verify_adopted_design(kernel, repo, delivery, receipt, adopted):
    from .scene_revision import editorial_blockers

    source = receipt["source_world_ref"]
    adoption = _receipt(repo, source, "P6_IDEA_MATERIALIZE", "initial")
    intake = _receipt(repo, source, "P6_IDEA_ENROLL", "initial")
    idea = intake["creative_input"]
    _check(
        adoption == receipt
        and idea == receipt["creative_input"]
        and receipt.get("model_routes") == intake.get("model_routes")
    )
    design = intake
    rounds = receipt.get("design_revision_round", 0)
    _check(type(rounds) is int and 0 <= rounds <= 2 + 2 * intake["profile"]["max_reworks"])
    if rounds:
        _check(receipt["original_input_ref"] == intake["input_ref"])
    structural = editorial = ending = fixes = 0
    for index in range(rounds + 1):
        final = index == rounds
        if final:
            selected_ref = receipt["result_ref"]
        else:
            revised = _receipt(repo, source, "P6_IDEA_REVISE", str(index + 1))
            _check(revised["round"] == index + 1 and revised["design_task_id"] == design["task_id"])
            selected_ref = revised["candidate_ref"]
        origin = kernel.artifact(repo, selected_ref, source["work_id"])
        task, audit, candidate, derivation = _design_output(
            kernel,
            repo,
            delivery,
            source,
            design,
            selected_ref,
            origin["kind"],
        )
        _check(audit["world_ref"] == source)
        if final or "review_ref" in revised:
            review = _receipt(
                repo, source, "P6_IDEA_REVIEW", "initial" if index == 0 else str(index)
            )
            review_task, review_audit, report = _review(
                kernel,
                repo,
                delivery,
                source,
                review,
                task,
                selected_ref,
                candidate,
                idea,
                ending_revision=not final and revised.get("revision_purpose") == "SHORT_ENDING",
            )
        if final:
            norm = derivation if origin["kind"] == "NORMALIZED_DESIGN_CANDIDATE" else None
            ending_plan = derivation if origin["kind"] == "ENDING_PLAN_CANDIDATE" else None
            _check(receipt.get("design_ending_plan") == ending_plan)
            if ending_plan:
                from .idea_ending import confirm_ending

                packet = _assignment(kernel, repo, source, review)
                _check(packet["ending_plan_source"] == ending_plan["source"])
                confirm_ending(report, ending_plan["source"], selected_ref)
            _check_record(task, audit, receipt)
            _check_record(review_task, review_audit, receipt["design_review"])
            _check(
                receipt["source_input_ref"] == design["input_ref"]
                and candidate == adopted
                and receipt.get("design_normalization") == norm
                and (
                    task["state"] == "ACCEPTED"
                    or task["state"] == "FAILED"
                    and derivation is not None
                )
                and review_task["state"] == "ACCEPTED"
                and review_audit["output_ref"] == receipt["design_review"]["result_ref"]
            )
            arc = report.get("short_arc")
            _check(
                receipt["design_review"].get("short_arc") == arc
                and (arc is None or arc["decision"] == "COMPLETE")
            )
            issues = report["issues"]
            _check(receipt["design_review"].get("advisories", []) == issues)
            _check(not (editorial_blockers(issues) if intake.get("model_routes") else issues))
            return
        packet = _assignment(kernel, repo, source, revised)
        _check(
            packet["candidate_ref"] == selected_ref
            and packet["candidate"] == candidate
            and packet["creative_input"] == idea
        )
        if "review_ref" in revised:
            _check(packet["review_ref"] == revised["review_ref"] == review_audit["output_ref"])
            if revised.get("revision_purpose") == "SHORT_ENDING":
                ending += 1
                _check(
                    ending == 1
                    and packet["short_arc"] == report["short_arc"]
                    and report["short_arc"]["decision"] == "ENDING_PLAN_REQUIRED"
                )
            elif revised.get("revision_purpose") == "SHORT_ENDING_FIX":
                fixes += 1
                permission = _receipt(
                    repo, source, "P6_ENDING_FIX_AUTHORIZATION", review_audit["attempt_id"]
                )
                _check(
                    fixes == 1
                    and design.get("revision_purpose") == "SHORT_ENDING"
                    and revised["editor_decision"] == packet["editor_decision"] == permission
                    and permission["attempt_id"] == review_audit["attempt_id"]
                    and permission["candidate_ref"] == selected_ref
                    and permission["review_ref"] == review_audit["output_ref"]
                    and permission["issue_ids"]
                    == [i["issue_id"] for i in editorial_blockers(report["issues"])]
                    and packet["ending_plan_source"] == derivation["source"]
                )
            else:
                editorial += 1
                _check(editorial <= intake["profile"]["max_reworks"])
        else:
            structural += 1
            _check(
                structural <= intake["profile"]["max_reworks"]
                and task["state"] == "FAILED"
                and selected_ref == audit["response_ref"]
            )
        design = revised
