"""Two canonical interactions, finite editorial expansion and safe refusal at the boundary."""

import json
from uuid import uuid4

import pytest
from test_p5_autonomous_banquet import _candidate
from test_p5_supervisor import backend
from test_planning import make_plan
from test_simulation import process_definition, register

from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.planning import PlanningService
from novel_factory.application.scene_runtime import register_scene
from novel_factory.application.supervisor import RuntimeSupervisor
from novel_factory.fixtures import load_fixture, point

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


@pytest.mark.parametrize("choice", ["approved", "no_major_change", "wrong_trigger"])
def test_bounded_two_scene_chain(kernel_env, choice):
    kernel = kernel_env["kernel"]
    fixture = load_fixture(kernel, "white_bridge")
    ids, owner = fixture["ids"], fixture["principal"]
    scopes = [ids["warehouse"], ids["south"]]
    actors = [ids["protagonist"]]
    for name, due in [("invitation", 30), ("follow-up", 50)]:
        process = process_definition(
            kernel, fixture, name=name, scopes=scopes, actors=actors, due=due
        )
        fixture, _ = register(kernel, fixture, process)
    wr = fixture["world_ref"]
    work, branch = wr["work_id"], wr["branch_id"]
    plans = PlanningService(kernel)
    plan, _ = make_plan(plans, fixture)
    for i, node in enumerate(plan["nodes"]):
        node["target_time"] = {"kind": "EXACT", "at": point(50 + i)}
    ref = plans.save_plan(owner, plan, "adoptable")
    plans.adopt_plan(owner, wr, ref, None, "adopt")
    slot = {
        "node_id": plan["nodes"][0]["node_id"],
        "process_id": str(uuid4()) if choice == "wrong_trigger" else process["process_id"],
    }
    with backend() as (http, requests, server):
        gateway = AgentGateway(kernel, {"test": http})
        registration = {
            "world_ref": wr,
            "story_time": point(30),
            "scope_ids": scopes,
            "volume_id": plan["volume_id"],
            "scene_limit": 2,
            "participants": [{"actor_id": actors[0]}],
            "subject_id": actors[0],
            "destination_id": ids["south"],
            "budget_id": kernel.tasks.create_budget(work, "20"),
            "adapter_id": "test",
            "reserve_amount": "1",
            "token_reservation": 100000,
            "following_scenes": [slot],
            "idempotency_key": "two-scenes",
        }
        scene = register_scene(kernel, gateway, owner, **registration)

        def candidate(ctx):
            packet = json.loads(requests[-1]["messages"][1]["content"])
            body = _candidate(packet, {actors[0]: "去南岸核实证据。"}, scopes)
            if ctx["role_id"] == "character_actor":
                at = ctx["story_time"]
                body["story_interval"] = {"start": at, "end": point(at["tick"] + 1), "bounds": "[)"}
                assert slot["node_id"] not in requests[-1]["messages"][1]["content"]
            if packet.get("editorial", {}).get("operation") == "request_change" and (
                choice != "approved" or ctx["story_time"]["tick"] == 50
            ):
                body["important_deltas"] = []
            return body

        server.candidate = candidate
        deliveries = []
        for _ in range(28):
            # Each iteration constructs a new worker against the same persisted queue.
            receipt = RuntimeSupervisor(kernel, gateway).run_once(owner, work, branch)
            assert receipt["status"] not in {"FAILED", "WAITING_REVIEW", "PAUSED"}, receipt
            if "continuation" in receipt:
                deliveries.append(receipt["continuation"])
            if receipt["status"] == "IDLE":
                break
        assert len(deliveries) == 1, (deliveries, receipt)
        next_scene = deliveries[0]
        calls = len(requests)
        assert register_scene(kernel, gateway, owner, **registration) == scene
        assert RuntimeSupervisor(kernel, gateway).run_once(owner, work, branch)["status"] == "IDLE"
        assert len(requests) == calls
        with kernel.db.uow(write=False) as repo:
            interactions = repo.many("simulation_interactions", {"work_id": work})
            selected = repo.one(
                "selections",
                {"work_id": work, "selection_kind": plans.selection_key(plan["volume_id"])},
            )
            if choice == "wrong_trigger":
                assert next_scene == {
                    "status": "STOPPED",
                    "reason": "ADVANCE_BLOCKED",
                    "node_id": slot["node_id"],
                }
                assert calls == 3 and len(interactions) == 1
                assert (
                    repo.one("tasks", {"task_id": scene["director_task_id"]})["state"] == "ACCEPTED"
                )
                return
            assert next_scene["status"] == "STARTED" and next_scene["remaining_slots"] == 0
            assert len(interactions) == 2 and calls == (8 if choice == "approved" else 6)
            assert (selected["artifact_ref"] != ref) == (choice == "approved")
            assert selected["artifact_ref"] == next_scene["plan_ref"]
            assert next_scene["reference_timeline_ref"] == scene["reference_timeline_ref"]
            assert (
                next_scene["world_ref"] != wr and next_scene["plan_ref"] == selected["artifact_ref"]
            )
            assert next_scene["actor_tasks"][actors[0]] != scene["actor_tasks"][actors[0]]
            for registered in (scene, next_scene):
                assert (
                    repo.one("tasks", {"task_id": registered["control_task_id"]})["state"]
                    == "ACCEPTED"
                )
                assert (
                    repo.one("tasks", {"task_id": registered["director_task_id"]})["state"]
                    == "ACCEPTED"
                )
