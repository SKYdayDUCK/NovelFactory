"""Create the two preauthorized planning successors inside their parent's delivery transaction."""

from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.kernel import require
from novel_factory.application.runtime_tasks import RuntimeTaskService
from novel_factory.application.supervisor import RuntimeSupervisor
from novel_factory.domain.canonical import fingerprint


def provision_successor(kernel, gateway, controller, task, spec, cfg, outcome):
    """The locked parent and its delivery receipt make a concurrent replay a no-op."""
    route = cfg["editorial"]
    operation = route["operation"]
    if operation == "request_change" and "change_point_ref" not in outcome:
        return None
    require(
        operation in {"request_change", "propose_window"},
        "VIEW_DENIED",
        "Only the bounded editorial chain may allocate another task",
    )
    wr = spec["world_ref"]
    dispatch = cfg["dispatch"]
    with kernel.db.uow(write=False) as repo:
        manifest = kernel.artifact(
            repo, cfg["manifest_ref"], task["work_id"], kind="INPUT_MANIFEST"
        )["payload"]
        parent_input = kernel.artifact(
            repo, dispatch["input_ref"], task["work_id"], kind="AGENT_INPUT"
        )["payload"]
        remaining = min(86400, int((task["deadline"] - repo.now()).total_seconds()))
        require(remaining > 0, "TASK_EXPIRED", "Editorial delivery has reached its deadline")
        child_route = {
            "volume_id": route["volume_id"],
            "reference_timeline_ref": route["reference_timeline_ref"],
            "change_point_ref": outcome["change_point_ref"],
        }
        if operation == "request_change":
            prior = kernel.artifact(
                repo, route["prior_plan_ref"], task["work_id"], kind="PLAN_REVISION"
            )["payload"]
            child_route.update(
                operation="propose_window",
                prior_plan_ref=route["prior_plan_ref"],
                volume_contract_ref=prior["volume_contract_ref"],
            )
            role, mode, skill = "volume_director", "replan", "roll-window"
            assignment = (
                "根据已接受事件、ChangePoint 和参考计划，为授权范围内的未来场次拟定滚动计划。"
            )
        else:
            child_route.update(
                operation="review_and_adopt",
                plan_ref=outcome["plan_ref"],
                window_ref=outcome["window_ref"],
            )
            role, mode, skill = "chief_editor", "editorial_decision", "review-change-point"
            assignment = (
                "独立复核此次偏离、候选计划和有限窗口，记录同意、退回或需要作者决定的问题。"
            )

    adapter_id = parent_input["adapter_id"]
    require(
        adapter_id in gateway.adapters
        and fingerprint(gateway.adapters[adapter_id].configuration)
        == parent_input["model_config_sha256"],
        "MODEL_CONFIG_CHANGED",
        "Parent's model route changed",
    )
    bound_gateway = AgentGateway(
        kernel,
        gateway.adapters,
        catalog=gateway.catalog,
        output_contracts=gateway.output_contracts,
        material_reader=gateway.material_reader,
    )
    child = RuntimeTaskService(kernel, bound_gateway).prepare(
        controller,
        wr,
        role_id=role,
        task_kind=mode,
        skill_name=skill,
        assignment=assignment,
        knowledge_cut=cfg["knowledge_cut"],
        subject_ids=manifest["subject_ids"],
        predicates=manifest["predicates"],
        input_artifacts=[
            cfg["knowledge_cut"]["plan_manifest_ref"],
            *(ref for key, ref in child_route.items() if key.endswith("_ref")),
        ],
        adapter_id=adapter_id,
        story_time=spec["story_time"],
        queue_state="READY",
        deadline_seconds=remaining,
        lease_seconds=min(600, remaining),
        context_token_budget=cfg["context_tokens"],
        **spec["budget"],
    )
    task_id = child.task["task_id"]
    RuntimeSupervisor(kernel, bound_gateway).enroll(
        controller,
        task_id,
        manifest_ref=child.manifest_ref,
        input_ref=child.agent_input["input_ref"],
        knowledge_cut=cfg["knowledge_cut"],
        context_tokens=cfg["context_tokens"],
        budget_id=dispatch["budget_id"],
        reserve_amount=dispatch["reserve_amount"],
        token_reservation=dispatch["token_reservation"],
        editorial=child_route,
        continue_editorial=(operation == "request_change"),
    )
    return task_id
