"""Continuity publication automatically funds one bounded next-volume planning candidate."""

import json
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from test_continuity import entry, qualify, request, reviewer, volume
from test_p5_supervisor import backend
from test_planning import make_plan

from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.authorization import Principal
from novel_factory.application.continuity import ContinuityService
from novel_factory.application.planning import PlanningService
from novel_factory.application.rolling_plans import RollingPlanning
from novel_factory.application.supervisor import RuntimeSupervisor
from novel_factory.application.volume_successor import register_next_volume
from novel_factory.fixtures import point, run_command

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


@pytest.mark.parametrize("disposition", ["bounded", "over-limit", "cancelled"])
def test_publication_starts_and_saves_one_planner(kernel_env, disposition):
    kernel = kernel_env["kernel"]
    f, release, _ = volume(kernel)
    owner, original = f["principal"], f["world_ref"]
    work, branch = original["work_id"], original["branch_id"]
    plans = PlanningService(kernel)
    target, _ = make_plan(plans, f)
    target["nodes"][0]["goal"] = "最初的计划是去北岸；此为可偏离的创作参考"
    reference = plans.save_plan(owner, target, "next-reference")
    plans.adopt_plan(owner, original, reference, None, "next-adopt")
    RollingPlanning(kernel).enable(
        owner, original, target["volume_id"], scenes=1, idempotency_key="freeze"
    )
    series = str(uuid4())
    with backend() as (http, requests, server):
        gateway = AgentGateway(kernel, {"test": http})
        opts = {
            "world_ref": original,
            "series_id": series,
            "from_volume_id": f["volume_id"],
            "next_volume_id": target["volume_id"],
            "story_time": point(12),
            "subject_ids": [f["ids"]["protagonist"], f["ids"]["south"]],
            "predicates": ["located_in"],
            "adapter_id": "test",
            "budget_id": kernel.tasks.create_budget(work, "2"),
            "reserve_amount": "0.1",
            "token_reservation": 100000,
            "deadline_seconds": 3600,
            "idempotency_key": "one-transition",
        }
        receipt = register_next_volume(kernel, gateway, owner, **opts)

        def tick():
            return RuntimeSupervisor(kernel, gateway).run_once(owner, work, branch)

        assert (
            receipt["status"] == "AWAITING_CONTINUITY"
            and tick()["status"] == "IDLE"
            and not requests
        )
        aftermath = run_command(
            kernel,
            owner,
            original,
            "SET_RELATION",
            {
                "subject_id": f["ids"]["protagonist"],
                "predicate": "located_in",
                "object_value": {"kind": "ENTITY", "entity_id": f["ids"]["south"]},
                "at": point(9),
            },
        )
        live = {**f, "world_ref": aftermath["world_ref"]}
        memory = entry(f, release, 1, kind="FORESHADOW")
        memory.update(status="OPEN", planned_payoff_at=point(80))
        continuity = ContinuityService(kernel)
        body = request(live, [release], [memory], series_id=series)
        approval, _, _ = qualify(continuity, owner, reviewer(kernel, live), work, body)
        snapshot = continuity.publish(owner, work, approval)["snapshot_ref"]
        assert register_next_volume(kernel, gateway, owner, **opts) == receipt

        if disposition == "cancelled":
            ready = tick()
            assert ready["status"] == "VOLUME_PLANNER_READY"
            task = kernel.tasks.get(ready["task_id"])
            actor = Principal(task["grant_id"], task["principal_id"])
            kernel.cancel_task(actor, task["task_id"], task["generation"], "cancel-next-volume")
            stopped = tick()
            assert stopped == {
                "status": "VOLUME_SUCCESSOR_STOPPED",
                "reason": "TASK_CANCELLED",
                "task_id": task["task_id"],
            }
            assert tick()["status"] == "IDLE" and not requests
            with kernel.db.uow(write=False) as repo:
                transitions = repo.many("outbox", {
                    "topic": "volume.successor.changed", "aggregate_id": target["volume_id"],
                })
                assert [e["payload"] for e in transitions].count(stopped) == 1
                assert kernel.head(repo, work, branch) == live["world_ref"]
            return

        def candidate(ctx):
            packet = json.loads(requests[-1]["messages"][1]["content"])
            assert target["nodes"][0]["goal"] in packet["assignment"]
            assert target["nodes"][1]["goal"] not in packet["assignment"]
            material = json.loads(packet["authorized_materials"][0]["content"])
            assert material["snapshot_ref"] == snapshot and "planned_payoff_at" not in str(material)
            assert material["world_ref"] == ctx["world_ref"] == live["world_ref"]
            assert (
                material["world_effects_applied"] == 0
                and packet["read_cut"]["knowledge_cut"]["plane"] == "PLAN"
            )
            with kernel.db.uow(write=False) as repo:
                contract = kernel.artifact(repo, target["volume_contract_ref"], work)["payload"]
            return {
                "schema_version": "2.0",
                "window_plan_id": str(uuid4()),
                "volume_contract_revision_id": contract["contract_revision_id"],
                "input_world_ref": ctx["world_ref"],
                "story_interval": {"start": point(13), "end": point(14), "bounds": "[)"},
                "scope_ids": [f["ids"]["south"]],
                "process_ids": [],
                "focus_actor_ids": [f["ids"]["protagonist"]],
                "resolution_strategy": "MIXED",
                "candidate_goals": ["人物在南岸求证前卷未解的问题"]
                + (["另一个未授权场次"] if disposition == "over-limit" else []),
                "next_decision_reason": "等待人物的独立选择",
                "assumptions": [],
                "adopted_event_refs": [],
                "expected_outputs": ["REPLAN_REPORT"],
                "dependency_fingerprint": ctx["dependency_fingerprint"],
                "status": "DRAFT",
            }

        server.candidate = candidate
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: tick(), range(2)))
        results.extend([tick(), tick(), tick()])
        assert [r["status"] for r in results].count("VOLUME_PLANNER_READY") == 1, results
        if disposition == "over-limit":
            (stopped,) = [r for r in results if r["status"] == "VOLUME_SUCCESSOR_STOPPED"]
            assert stopped["reason"] == "WINDOW_CONFLICT"
            assert tick()["status"] == "IDLE" and len(requests) == 1
            with kernel.db.uow(write=False) as repo:
                assert (
                    repo.one("tasks", {"task_id": stopped["task_id"]})["state"] == "WAITING_REVIEW"
                )
                assert kernel.head(repo, work, branch) == live["world_ref"]
            return
        (saved,) = [r for r in results if r["status"] == "VOLUME_WINDOW_STORED"]
        assert tick()["status"] == "IDLE" and len(requests) == 1
        assert (
            continuity.publish(owner, work, approval)["snapshot_ref"] == snapshot
            and tick()["status"] == "IDLE"
        )
        with kernel.db.uow(write=False) as repo:
            assert repo.one("tasks", {"task_id": saved["task_id"]})["state"] == "ACCEPTED"
            assert (
                kernel.artifact(repo, saved["window_ref"], work, kind="WINDOW_PLAN")["payload"][
                    "input_world_ref"
                ]
                == live["world_ref"]
            )
            assert (
                repo.one(
                    "selections",
                    {"work_id": work, "selection_kind": plans.selection_key(target["volume_id"])},
                )["artifact_ref"]
                == reference
            )
            assert kernel.head(repo, work, branch) == live["world_ref"]
