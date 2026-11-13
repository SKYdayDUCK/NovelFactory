"""Project fixed response fields from a task's authorized inputs, without choosing its decision."""

from uuid import UUID, uuid5

from ..domain.scheduling import after
from ..domain.temporal import point_key


def proposal_prompt(binding, editorial=None, joint=None):
    task, spec = binding["task"], binding["spec"]
    name = binding["input"]["output_contract"]["name"]

    def candidate_id(field):
        return str(uuid5(UUID(task["task_id"]), f"proposal:{task['generation']}:{field}"))

    fixed = {"schema_version": "2.0", "dependency_fingerprint": task["dependency_fingerprint"]}
    if name == "ActionProposal" and task["role_id"] == "character_actor":
        at = spec["story_time"]
        proposal_id = candidate_id("action")
        fixed.update(
            proposal_id=proposal_id,
            task_id=task["task_id"],
            generation=task["generation"],
            context_bundle_id=task["context_bundle_id"],
            world_ref=spec["world_ref"],
            actor_id=binding["grant"]["actor_id"],
            idempotency_key=proposal_id,
            story_interval={"start": at, "end": after(at), "bounds": "[)"},
        )
        return {
            "copy_into_payload": fixed,
            "complete": "自行决定 intent、targets、resource_requests、preconditions、evidence_refs。"
            "可提议说话、观察、等待、请求、尝试行动。仅明确知道的实体可作目标。"
            "无可引用的实体、资源或证据时，相应数组填 []。提议不能断定行为已成功。",
        }
    if name == "AdjudicationProposal" and joint:
        fixed.update(
            proposal_id=candidate_id("adjudication"),
            input_world_ref=spec["world_ref"],
            interaction_id=joint["interaction_id"],
            ruleset_ref=joint["ruleset_ref"],
            story_time=joint["story_time"],
            effect_group_id=candidate_id("effect-group"),
            representation_id=candidate_id("representation"),
            action_proposal_refs=[item["ref"] for item in joint["action_proposals"]],
        )
        loc = joint["allowed_location"]
        choices = [
            {
                "kind": "RELATION_SET",
                "effect_key": "location",
                "subject_id": loc["subject_id"],
                "predicate": "located_in",
                "object_id": dest,
                "value_status": "VALUE",
            }
            for dest in [loc["destination_id"], *loc["alternative_destination_ids"]]
        ]
        if gift := joint.get("allowed_item_gift"):
            choices.extend(
                {
                    "kind": "RELATION_SET",
                    "effect_key": "gift",
                    "subject_id": gift["item_id"],
                    "predicate": "legal_owner",
                    "object_id": recipient,
                    "value_status": "VALUE",
                }
                for recipient in gift["recipient_ids"]
            )
        return {
            "copy_into_payload": fixed,
            "authorized_effect_choices": choices,
            "complete": "综合两方或多方的独立提议、已知位置及规则，自主判断实际后果。"
            "effects 可为 []，或选用一个获准且确有依据的效果。保持原位时用 []。"
            "对白和试探可在 reasoning_summary 中说明，明确区分意图与判定。"
            "证据不足且无法裁决时，可在 unresolved_questions 写明疑问。",
        }
    if name == "WindowPlan" and not editorial:
        fixed.update(window_plan_id=candidate_id("window"), status="DRAFT", input_world_ref=spec["world_ref"])
        return {"copy_into_payload": fixed, "story_time": spec["story_time"],
                "complete": "按本次获准卷契约与任务给出的范围填写候选窗口及目标。"}
    if name == "WindowPlan" and editorial and editorial["operation"] == "propose_window":
        materials = editorial["materials"]
        change = materials["change_point_ref"]["content"]
        contract = materials["volume_contract_ref"]["content"]
        nodes = {n["node_id"]: n for n in materials["prior_plan_ref"]["content"]["nodes"]}
        times = [nodes[key]["target_time"] for key in change["node_ids"]]
        start = min((t.get("at") or t["earliest"] for t in times), key=point_key)
        last = max((t.get("at") or t["latest"] for t in times), key=point_key)
        fixed.update(
            window_plan_id=candidate_id("window"),
            status="DRAFT",
            input_world_ref=spec["world_ref"],
            volume_contract_revision_id=contract["contract_revision_id"],
            story_interval={"start": start, "end": after(last), "bounds": "[)"},
            adopted_event_refs=change["event_refs"],
        )
        return {
            "copy_into_payload": fixed,
            "maximum_goals": len(times),
            "available_focus_subject_ids": contract["focus_subject_ids"],
            "complete": "按变更点的场次顺序提出一至 maximum_goals 个未来目标和理由。"
            "scope_ids 至少含一个有依据的主体；未指定过程或焦点人物时相应数组可留空。"
            "另填 resolution_strategy、assumptions、expected_outputs。",
        }
    return None
