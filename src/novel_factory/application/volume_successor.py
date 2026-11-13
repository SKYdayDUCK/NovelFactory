"""One preauthorized next-volume planning turn after independent continuity publication."""

from datetime import datetime, timedelta

from sqlalchemy import text

from novel_factory.application.authorization import Principal
from novel_factory.application.candidate_delivery import CandidateDelivery
from novel_factory.application.kernel import require
from novel_factory.application.planning import PlanningService
from novel_factory.application.rolling_plans import _anchor_name
from novel_factory.application.runtime_tasks import RuntimeTaskService
from novel_factory.contracts.registry import validate_contract
from novel_factory.domain.canonical import fingerprint
from novel_factory.domain.errors import DomainError
from novel_factory.domain.scheduling import state_from_revision
from novel_factory.domain.temporal import point_key


def _save(kernel, repo, owner, work, body, kind):
    ref = kernel._blob(body)
    kernel._register_artifact(repo, ref, work, body, kind, owner.principal_id)
    return ref


def register_next_volume(
    kernel,
    gateway,
    owner,
    *,
    world_ref,
    series_id,
    from_volume_id,
    next_volume_id,
    story_time,
    subject_ids,
    predicates,
    adapter_id,
    budget_id,
    reserve_amount,
    token_reservation,
    deadline_seconds,
    idempotency_key,
):
    """Bind one distinct target volume and allow exactly one projection of its frozen reference."""
    PlanningService._key(idempotency_key)
    validate_contract("WorldRef", world_ref)
    validate_contract("StoryPoint", story_time)
    for uid in (series_id, from_volume_id, next_volume_id, budget_id):
        validate_contract("UUID", uid)
    require(
        from_volume_id != next_volume_id
        and type(deadline_seconds) is int
        and 1 <= deadline_seconds <= 86400,
        "INVALID_ARGUMENT",
        "Choose a different next volume and a finite activation deadline",
    )
    validate_contract(
        "InputManifest", {"subject_ids": subject_ids, "predicates": predicates}, "kernel"
    )
    work, branch = world_ref["work_id"], world_ref["branch_id"]
    request = {
        "world_ref": world_ref,
        "series_id": series_id,
        "from_volume_id": from_volume_id,
        "next_volume_id": next_volume_id,
        "story_time": story_time,
        "subject_ids": subject_ids,
        "predicates": predicates,
        "adapter_id": adapter_id,
        "budget_id": budget_id,
        "reserve_amount": reserve_amount,
        "token_reservation": token_reservation,
        "deadline_seconds": deadline_seconds,
    }
    with kernel.objects.lock(), kernel.db.uow() as repo:
        current_work = repo.one("works", {"work_id": work}, lock="update")
        owner_grant = kernel.authorize(repo, owner, work, "ADMIN", branch)
        prior = kernel._prior(repo, owner, work, "REGISTER_NEXT_VOLUME", idempotency_key, request)
        if prior:
            return prior
        require(
            kernel.head(repo, work, branch) == world_ref,
            "HEAD_CONFLICT",
            "Register against the accepted head",
        )
        anchor = repo.one(
            "selections",
            {"work_id": work, "selection_kind": _anchor_name(world_ref, next_volume_id)},
        )
        require(
            anchor
            and repo.one("budgets", {"budget_id": budget_id, "work_id": work})
            and (adapter_id == "@policy" or adapter_id in gateway.adapters),
            "VIEW_DENIED",
            "Frozen target reference, model route and work budget are required",
        )
        validate_contract(
            "AgentDispatchRequest",
            {
                "schema_version": "2.1",
                "task_id": next_volume_id,
                "generation": 1,
                "worker_id": "registration",
                "call_key": "window",
                "input_ref": anchor["artifact_ref"],
                "budget_id": budget_id,
                "reserve_amount": reserve_amount,
                "token_reservation": token_reservation,
            },
            "agent",
        )
        key = {"work_id": work, "selection_kind": f"VOLUME_SUCCESSOR:{branch}:{next_volume_id}"}
        require(
            not repo.one("selections", key),
            "IDEMPOTENCY_CONFLICT",
            "This target already has a registered allowance",
        )
        body = {
            **request,
            "grant_id": owner.grant_id,
            "principal_id": owner.principal_id,
            "epoch": owner_grant["epoch"],
            "policy_version": current_work["policy_version"],
            "anchor_ref": anchor["artifact_ref"],
            "model_sha256": fingerprint(gateway.adapters[adapter_id].configuration) if adapter_id != "@policy" else None,
            "deadline": (repo.now() + timedelta(seconds=deadline_seconds)).isoformat(),
        }
        ref = _save(kernel, repo, owner, work, body, "VOLUME_SUCCESSOR")
        repo.add("selections", {**key, "artifact_ref": ref, "epoch": 1})
        result = {"status": "AWAITING_CONTINUITY", "allowance_ref": ref}
        kernel._receipt(
            repo, owner, world_ref, "REGISTER_NEXT_VOLUME", idempotency_key, request, result
        )
        repo.outbox(
            "volume.successor.registered",
            "volume",
            next_volume_id,
            result,
            "volume-register:" + ref["artifact_revision_id"],
        )
        # A matching continuity publication may already exist. Wake the listener
        # after commit even in installations already running migration 0007.
        repo.conn.execute(text("SELECT pg_notify('nf_supervisor_wake', '')"))
        return result


def _authorize(kernel, repo, body):
    wr = body["world_ref"]
    owner = Principal(body["grant_id"], body["principal_id"])
    grant = kernel.authorize(repo, owner, wr["work_id"], "ADMIN", wr["branch_id"])
    require(
        grant["epoch"] == body["epoch"]
        and repo.one("works", {"work_id": wr["work_id"]})["policy_version"]
        == body["policy_version"],
        "VIEW_DENIED",
        "Next-volume authorization changed",
    )
    require(
        repo.now() < datetime.fromisoformat(body["deadline"]),
        "TASK_EXPIRED",
        "Next-volume allowance expired",
    )
    return owner


def _selected(repo, work, key):
    row = repo.one("selections", {"work_id": work, "selection_kind": key})
    return row and row["artifact_ref"]


def _start(kernel, repo, gateway, owner, body, allowance_ref, snapshot_ref):
    from novel_factory.application.supervisor import RuntimeSupervisor

    work, branch = body["world_ref"]["work_id"], body["world_ref"]["branch_id"]
    snapshot = kernel.artifact(repo, snapshot_ref, work, kind="CONTINUITY_SNAPSHOT")["payload"]
    wr = snapshot["request"]["world_ref"]
    require(
        wr == kernel.head(repo, work, branch)
        and repo.ancestor(body["world_ref"], wr)
        and snapshot["volumes"][-1]["volume_id"] == body["from_volume_id"]
        and all(v["volume_id"] != body["next_volume_id"] for v in snapshot["volumes"]),
        "CONTINUITY_REVALIDATION_REQUIRED",
        "Published continuity must end with the authorized previous volume at current HEAD",
    )
    adapter = gateway.adapters.get(body["adapter_id"])
    require(
        body["adapter_id"] == "@policy" or (adapter and fingerprint(adapter.configuration) == body["model_sha256"]),
        "MODEL_CONFIG_CHANGED",
        "Model route changed",
    )
    volume = body["next_volume_id"]
    require(
        _selected(repo, work, _anchor_name(wr, volume)) == body["anchor_ref"],
        "REFERENCE_FROZEN",
        "Reference changed",
    )
    anchor = kernel.artifact(repo, body["anchor_ref"], work, kind="REFERENCE_TIMELINE")["payload"]
    reference = kernel.artifact(repo, anchor["initial_plan_ref"], work, kind="PLAN_REVISION")[
        "payload"
    ]
    plan_ref = _selected(repo, work, PlanningService.selection_key(volume))
    manifest_ref = _selected(repo, work, "PLAN_MANIFEST:" + volume)
    selected = kernel.artifact(repo, plan_ref, work, kind="PLAN_REVISION")["payload"]
    contract_ref = selected["volume_contract_ref"]
    contract = kernel.artifact(repo, contract_ref, work, kind="VOLUME_CONTRACT")["payload"]
    require(
        repo.ancestor(selected["input_world_ref"], wr)
        and reference["volume_contract_ref"] == contract_ref,
        "STALE_PLAN",
        "Destination plan is no longer on its authorized lineage",
    )
    goals = [n["goal"] for n in reference["nodes"][: anchor["scene_limit"]]]
    cut = {"plane": "PLAN", "plan_manifest_ref": manifest_ref}
    seconds = int((datetime.fromisoformat(body["deadline"]) - repo.now()).total_seconds())
    require(seconds > 0, "TASK_EXPIRED", "The planning turn has no time remaining")
    bound_gateway = gateway.with_kernel(kernel)
    job = RuntimeTaskService(kernel, bound_gateway).prepare(
        owner,
        wr,
        role_id="volume_director",
        task_kind="window_plan",
        knowledge_cut=cut,
        subject_ids=body["subject_ids"],
        predicates=body["predicates"],
        story_time=body["story_time"],
        material_refs=[snapshot_ref],
        input_artifacts=[manifest_ref, body["anchor_ref"], contract_ref],
        assignment=(
            f"根据上一卷已审核的连续性和当前获准资料，为卷契约 {contract['contract_revision_id']} 写有限未来窗口。"
            f"原始参考线中最初的目标均为可偏离的规划：{goals}。"
            "保留已接受历史，对下一卷仍未发生的事使用候选或假设表述。"
        ),
        adapter_id=body["adapter_id"],
        queue_state="READY",
        max_amount=body["reserve_amount"],
        token_budget=body["token_reservation"],
        deadline_seconds=seconds,
        lease_seconds=min(600, seconds),
    )
    task_id = job.task["task_id"]
    RuntimeSupervisor(kernel, bound_gateway).enroll(
        owner,
        task_id,
        manifest_ref=job.manifest_ref,
        input_ref=job.agent_input["input_ref"],
        knowledge_cut=cut,
        context_tokens=20000,
        budget_id=body["budget_id"],
        reserve_amount=body["reserve_amount"],
        token_reservation=body["token_reservation"],
    )
    record = {
        "allowance_ref": allowance_ref,
        "snapshot_ref": snapshot_ref,
        "world_ref": wr,
        "manifest_ref": manifest_ref,
        "contract_ref": contract_ref,
        "task_id": task_id,
        "series_id": body["series_id"],
        "scene_limit": anchor["scene_limit"],
        "from_volume_id": body["from_volume_id"],
    }
    ref = _save(kernel, repo, owner, work, record, "NEXT_VOLUME_JOB")
    repo.add(
        "selections",
        {
            "work_id": work,
            "selection_kind": "NEXT_VOLUME_TASK:" + task_id,
            "artifact_ref": ref,
            "epoch": 1,
        },
    )
    return ref, {
        "status": "VOLUME_PLANNER_READY",
        "task_id": task_id,
        "world_ref": wr,
        "snapshot_ref": snapshot_ref,
    }


def check_planner_cut(kernel, repo, task, spec, cut, snapshot_ref):
    work = task["work_id"]
    ref = _selected(repo, work, "NEXT_VOLUME_TASK:" + task["task_id"])
    require(ref, "VIEW_DENIED", "This planner lacks a continuity allowance")
    record = kernel.artifact(repo, ref, work, kind="NEXT_VOLUME_JOB")["payload"]
    body = kernel.artifact(repo, record["allowance_ref"], work, kind="VOLUME_SUCCESSOR")["payload"]
    owner = _authorize(kernel, repo, body)
    require(
        spec["role_id"] == "volume_director"
        and spec["task_kind"] == "window_plan"
        and record["snapshot_ref"] == snapshot_ref
        and record["world_ref"] == spec["world_ref"]
        and record["manifest_ref"] in spec["input_artifacts"]
        and snapshot_ref in spec["input_artifacts"]
        and cut == {"plane": "PLAN", "plan_manifest_ref": record["manifest_ref"]}
        and _selected(repo, work, "PLAN_MANIFEST:" + body["next_volume_id"])
        == record["manifest_ref"]
        and _selected(repo, work, "CONTINUITY:" + body["series_id"]) == snapshot_ref
        and kernel.head(repo, work, task["branch_id"]) == spec["world_ref"],
        "CONTINUITY_REVALIDATION_REQUIRED",
        "Next-volume cut changed",
    )
    return owner, record


def guard_volume_send(kernel, repo, task, spec, job, request=None):
    """Recheck a preauthorized volume at paid dispatch and at the Host forwarding boundary."""
    if "book_review_packet" in job:
        from .book_review import guard_job
        guard_job(kernel, repo, task, job, request)
    if "production_node" in job:
        from .production_pipeline import guard_send
        guard_send(kernel, repo, task, job, request)
    if (task["role_id"], task["task_kind"]) != ("volume_director", "window_plan"):
        return
    ref = _selected(repo, task["work_id"], "NEXT_VOLUME_TASK:" + task["task_id"])
    if ref:
        record = kernel.artifact(repo, ref, task["work_id"], kind="NEXT_VOLUME_JOB")["payload"]
        check_planner_cut(kernel, repo, task, spec, job["knowledge_cut"], record["snapshot_ref"])
        if request is not None:
            require(
                request["call_key"] == "supervised-candidate"
                and all(request[k] == value for k, value in job["dispatch"].items()),
                "VIEW_DENIED",
                "Next-volume planning requires the enrolled call and budget",
            )


_READY = text("""
    SELECT s.selection_kind, s.epoch, s.artifact_ref, c.artifact_ref AS snapshot
    FROM nf_selections s JOIN nf_artifacts a ON a.artifact_revision_id=(s.artifact_ref->>'artifact_revision_id')::uuid
    LEFT JOIN nf_selections c ON c.work_id=s.work_id AND c.selection_kind='CONTINUITY:'||(a.payload->>'series_id')
    LEFT JOIN nf_artifacts snap ON snap.artifact_revision_id=(c.artifact_ref->>'artifact_revision_id')::uuid
    LEFT JOIN nf_tasks t ON t.task_id=(a.payload->>'task_id')::uuid
    WHERE s.work_id=:work AND s.selection_kind LIKE :prefix AND (
        (s.epoch=1 AND (snap.payload->'volumes'->-1->>'volume_id'=a.payload->>'from_volume_id'
                       OR (a.payload->>'deadline')::timestamptz <= clock_timestamp()))
        OR (s.epoch=2 AND t.state IN ('RESULT_STORED','CANCELLED','FAILED','STALE')))
    ORDER BY s.selection_kind LIMIT 1
""")


def _check_candidate(kernel, repo, task, spec, record, memory, candidate):
    require(
        candidate["input_world_ref"] == spec["world_ref"]
        and candidate["dependency_fingerprint"] == task["dependency_fingerprint"]
        and 0 < len(candidate["candidate_goals"]) <= record["scene_limit"]
        and point_key(candidate["story_interval"]["start"]) > point_key(spec["story_time"]),
        "WINDOW_CONFLICT",
        "Next volume must propose a bounded future window",
    )
    allowance = kernel.artifact(
        repo, record["allowance_ref"], task["work_id"], kind="VOLUME_SUCCESSOR"
    )["payload"]
    subjects = set(allowance["subject_ids"])
    require(
        set(candidate["scope_ids"] + candidate["focus_actor_ids"]) <= subjects,
        "VIEW_DENIED",
        "Window exceeds the authorized subjects",
    )
    if candidate["process_ids"]:
        revision = kernel.revision(repo, spec["world_ref"])
        processes = state_from_revision(revision)["processes"]
        for pid in candidate["process_ids"]:
            process = processes.get(pid, {}).get("definition")
            require(
                process and set(process["scope_ids"] + process["participant_ids"]) <= subjects,
                "VIEW_DENIED",
                "Window process is outside the authorized scope",
            )
    permitted = [
        source["event_ref"] for entry in memory["entries"] for source in entry["source_events"]
    ]
    context = repo.one("contexts", {"context_bundle_id": task["context_bundle_id"]})["payload"]
    permitted.extend(
        ref["event_ref"] for ref in context["permitted_evidence_refs"] if ref.get("kind") == "EVENT"
    )
    require(
        all(ref in permitted for ref in candidate["adopted_event_refs"]),
        "VIEW_DENIED",
        "An event citation exceeds the delivered evidence",
    )


def advance_volume(kernel, gateway, controller, work, branch):
    params = {"work": work, "prefix": "VOLUME_SUCCESSOR:" + branch + ":%"}
    with kernel.db.uow(write=False) as repo:
        kernel.authorize(repo, controller, work, "ADMIN", branch)
        if not repo.conn.execute(_READY, params).first():
            return None
    with kernel.objects.lock(), kernel.db.uow() as repo:
        repo.one("works", {"work_id": work}, lock="update")
        kernel.authorize(repo, controller, work, "ADMIN", branch)
        repo.one("branches", {"work_id": work, "branch_id": branch}, lock="update")
        ready = repo.conn.execute(_READY, params).mappings().first()
        if not ready:
            return None
        bound = kernel.in_transaction(repo)
        ref, epoch = ready["artifact_ref"], ready["epoch"]
        try:
            with repo.conn.begin_nested():
                if epoch == 1:
                    body = bound.artifact(repo, ref, work, kind="VOLUME_SUCCESSOR")["payload"]
                    owner = _authorize(bound, repo, body)
                    ref, outcome = _start(bound, repo, gateway, owner, body, ref, ready["snapshot"])
                    next_epoch = 2
                else:
                    job = bound.artifact(repo, ref, work, kind="NEXT_VOLUME_JOB")["payload"]
                    task = repo.one("tasks", {"task_id": job["task_id"]}, lock="update")
                    if task["state"] in ("CANCELLED", "FAILED", "STALE"):
                        raise DomainError("TASK_" + task["state"], "The next-volume planner closed")
                    spec = repo.one(
                        "task_inputs",
                        {"task_id": task["task_id"], "generation": task["generation"]},
                    )["spec"]
                    cut = {"plane": "PLAN", "plan_manifest_ref": job["manifest_ref"]}
                    owner, record = check_planner_cut(
                        bound, repo, task, spec, cut, job["snapshot_ref"]
                    )
                    from novel_factory.application.continuity import ContinuityService

                    actor = Principal(task["grant_id"], task["principal_id"])
                    memory = ContinuityService(bound).read_for_task(
                        actor, task["task_id"], task["generation"], job["snapshot_ref"]
                    )
                    _, candidate = CandidateDelivery(bound, gateway).audited_candidate(repo, task)
                    _check_candidate(bound, repo, task, spec, record, memory, candidate)
                    window = PlanningService(bound).save_window_plan(
                        owner, candidate, record["contract_ref"], "next-volume:" + task["task_id"]
                    )
                    bound.tasks.accept_in_transaction(
                        repo.conn, task["task_id"], task["generation"]
                    )
                    outcome = {
                        "status": "VOLUME_WINDOW_STORED",
                        "task_id": task["task_id"],
                        "window_ref": window,
                    }
                    next_epoch = 3
        except DomainError as error:
            outcome = {"status": "VOLUME_SUCCESSOR_STOPPED", "reason": error.code}
            next_epoch = 3
            if epoch == 2:
                job = bound.artifact(repo, ready["artifact_ref"], work, kind="NEXT_VOLUME_JOB")[
                    "payload"
                ]
                task = repo.one("tasks", {"task_id": job["task_id"]}, lock="update")
                if task["state"] == "RESULT_STORED":
                    repo.change(
                        "tasks",
                        {"task_id": task["task_id"]},
                        {
                            "state": "WAITING_REVIEW",
                            "lease_owner": None,
                            "lease_expiry": None,
                            "updated_at": repo.now(),
                        },
                    )
                    bound.tasks._emit(
                        repo.conn, task["task_id"], task["generation"], "WAITING_REVIEW"
                    )
                outcome["task_id"] = task["task_id"]
        repo.change(
            "selections",
            {"work_id": work, "selection_kind": ready["selection_kind"]},
            {"artifact_ref": ref, "epoch": next_epoch},
        )
        repo.outbox(
            "volume.successor.changed",
            "volume",
            ready["selection_kind"].rsplit(":", 1)[1],
            outcome,
            f"volume-successor:{ready['artifact_ref']['artifact_revision_id']}:{epoch}",
        )
        return outcome
