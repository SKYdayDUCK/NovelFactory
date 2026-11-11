"""Two canonical interactions, finite editorial expansion and safe refusal at the boundary."""

import json
import os
from collections import Counter
from pathlib import Path
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


@pytest.mark.parametrize(
    "choice", ["approved", "no_major_change", "wrong_trigger", "single_end", "publish_approved"]
)
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
    series = str(uuid4())
    if choice == "approved":
        slot["volume_end"] = {"series_id": series}
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
        if choice in {"single_end", "publish_approved"}:
            from novel_factory.domain.errors import DomainError

            with pytest.raises(DomainError, match="final scene"):
                register_scene(
                    kernel, gateway, owner, **registration, volume_end={"series_id": series}
                )
            with pytest.raises(DomainError, match="last slot"):
                register_scene(
                    kernel,
                    gateway,
                    owner,
                    **{
                        **registration,
                        "following_scenes": [
                            {**slot, "volume_end": {"series_id": series}},
                            {"node_id": str(uuid4()), "process_id": str(uuid4())},
                        ],
                    },
                )
            del registration["following_scenes"]
            registration["volume_end"] = {
                "series_id": series,
                "publish_after_review": choice == "publish_approved",
            }
        if choice == "publish_approved":
            target_ref = authorize_next_volume_before_scene(
                kernel, fixture, gateway, registration, series
            )
            assert (
                RuntimeSupervisor(kernel, gateway).run_once(owner, work, branch)["status"] == "IDLE"
                and not requests
            )
        scene = register_scene(kernel, gateway, owner, **registration)

        def candidate(ctx):
            packet = json.loads(requests[-1]["messages"][1]["content"])
            body = _candidate(packet, {actors[0]: "去南岸核实证据。"}, scopes)
            if ctx["role_id"] == "character_actor":
                at = ctx["story_time"]
                body["story_interval"] = {"start": at, "end": point(at["tick"] + 1), "bounds": "[)"}
                assert slot["node_id"] not in requests[-1]["messages"][1]["content"]
            if packet.get("editorial", {}).get("operation") == "request_change" and (
                choice not in {"approved", "single_end", "publish_approved"}
                or ctx["story_time"]["tick"] == 50
            ):
                body["important_deltas"] = []
            return body

        server.candidate = candidate
        deliveries, settlements, statuses = [], [], []
        for _ in range(28):
            # Each iteration constructs a new worker against the same persisted queue.
            receipt = RuntimeSupervisor(kernel, gateway).run_once(owner, work, branch)
            statuses.append(receipt["status"])
            assert receipt["status"] not in {"FAILED", "WAITING_REVIEW", "PAUSED"}, receipt
            if "settlement" in receipt:
                settlements.append(receipt["settlement"])
            if "continuation" in receipt:
                deliveries.append(receipt["continuation"])
            if receipt["status"] == "IDLE":
                break
        if choice in {"approved", "single_end", "publish_approved"}:
            (settled,) = settlements
            assert settled["accepted_scenes"] == (2 if choice == "approved" else 1)
            with kernel.db.uow(write=False) as repo:
                candidate = kernel.artifact(
                    repo, settled["settlement_ref"], work, kind="VOLUME_SETTLEMENT_CANDIDATE"
                )["payload"]
                assert (
                    candidate["world_ref"] == kernel.head(repo, work, branch)
                    and candidate["world_effects_applied"] == 0
                )
                assert (
                    len({step["receipt"]["command_id"] for step in candidate["scenes"]})
                    == settled["accepted_scenes"]
                )
                assert (
                    repo.one(
                        "selections", {"work_id": work, "selection_kind": "CONTINUITY:" + series}
                    )
                    is None
                )
                assert (
                    repo.one(
                        "selections",
                        {"work_id": work, "selection_kind": "RELEASE:" + plan["volume_id"]},
                    )
                    is None
                )
                assert (
                    candidate["published_prefix_node_ids"] == []
                    and candidate["prior_continuity_ref"] is None
                )
            if choice in {"single_end", "publish_approved"}:
                assert not deliveries and len(requests) == 5
                assert register_scene(kernel, gateway, owner, **registration) == scene
                snapshot = exercise_independent_review(
                    kernel, fixture, settled, candidate, choice == "publish_approved"
                )
                if choice == "publish_approved":
                    finish_next_volume(
                        kernel,
                        fixture,
                        gateway,
                        target_ref,
                        snapshot,
                        candidate,
                        server,
                        requests,
                        statuses,
                    )
                return
        else:
            assert not settlements
            with kernel.db.uow(write=False) as repo:
                assert not repo.many(
                    "artifacts", {"work_id": work, "kind": "VOLUME_SETTLEMENT_CANDIDATE"}
                )
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


def exercise_independent_review(kernel, fixture, settled, candidate, auto_publish):
    from editorial_helpers import approve_binding, binding_for
    from test_continuity import reviewer
    from test_release import accept_draft, batch_for_plan, release_request, write_scene

    from novel_factory.application.continuity import ContinuityService
    from novel_factory.application.release import ReleaseService
    from novel_factory.domain.errors import DomainError

    wr = candidate["world_ref"]
    work, owner = wr["work_id"], fixture["principal"]
    with kernel.db.uow(write=False) as repo:
        plan = kernel.artifact(repo, candidate["plan_ref"], work, kind="PLAN_REVISION")["payload"]
    f = batch_for_plan(
        kernel,
        {**fixture, "world_ref": wr},
        plan,
        candidate["plan_ref"],
        narrative_id=str(uuid4()),
        key_prefix="terminal:",
    )
    accepted = [
        accept_draft(kernel, f, write_scene(kernel, f, i, text))
        for i, text in enumerate(["雨静了。", "两人到岸。", "等候回音。"])
    ]
    binding = binding_for(f, accepted)
    proof = approve_binding(kernel, f, binding)
    freeze = release_request(f, binding, proof["approval_ref"])
    release = ReleaseService(kernel).freeze(owner, freeze, "settled-release")
    assert ReleaseService(kernel).freeze(owner, freeze, "settled-release") == release
    with kernel.db.uow(write=False) as repo:
        (row,) = repo.many(
            "outbox",
            {"topic": "volume.settlement.continuity", "aggregate_id": candidate["volume_id"]},
        )
        handoff = row["payload"]
        assert (
            handoff["status"] == "AWAITING_INDEPENDENT_REVIEW"
            and handoff["settlement_ref"] == settled["settlement_ref"]
        )
        assert handoff["release_ref"] == release["release_ref"]
        assert (
            repo.one(
                "selections",
                {"work_id": work, "selection_kind": "CONTINUITY:" + candidate["series_id"]},
            )
            is None
        )
    service = ContinuityService(kernel)
    reader = reviewer(kernel, f)
    read = service.read_candidate(reader, work, handoff["candidate_ref"], "read-ending")
    request = {
        "schema_version": "2.3",
        "candidate_ref": handoff["candidate_ref"],
        "reading_ref": read["reading_ref"],
        "decision": "APPROVED",
        "reason": "已核实已接受证据及卷末正式版本。",
        "acknowledged_affected_ordinals": [],
        "idempotency_key": "settlement-review",
    }
    with pytest.raises(DomainError):
        service.review(owner, work, request)
    if auto_publish:
        service.review(
            reader,
            work,
            {**request, "decision": "CHANGES_REQUIRED", "idempotency_key": "return-settlement"},
        )
        with kernel.db.uow(write=False) as repo:
            assert (
                repo.one(
                    "selections",
                    {"work_id": work, "selection_kind": "CONTINUITY:" + candidate["series_id"]},
                )
                is None
            )
    approved = service.review(reader, work, request)
    with kernel.db.uow(write=False) as repo:
        pointer = repo.one(
            "selections",
            {"work_id": work, "selection_kind": "CONTINUITY:" + candidate["series_id"]},
        )
    if auto_publish:
        assert pointer is not None
        snapshot = pointer["artifact_ref"]
        assert service.review(reader, work, request) == approved
        with kernel.db.uow(write=False) as repo:
            (event,) = repo.many(
                "outbox",
                {"topic": "volume.settlement.publication", "aggregate_id": candidate["volume_id"]},
            )
            assert event["payload"]["snapshot_ref"] == snapshot
            assert (
                repo.one(
                    "selections",
                    {"work_id": work, "selection_kind": "CONTINUITY:" + candidate["series_id"]},
                )
                == pointer
            )
    else:
        assert pointer is None
        snapshot = service.publish(
            owner,
            work,
            {
                "schema_version": "2.3",
                "candidate_ref": handoff["candidate_ref"],
                "review_ref": approved,
                "idempotency_key": "publish-settlement",
            },
        )["snapshot_ref"]
    state = service.inspect(owner, work, snapshot)["snapshot"]
    assert state["request"]["entries"][0]["source_events"] == [
        {"world_ref": wr, "event_ref": ref}
        for ref in candidate["scenes"][0]["receipt"]["accepted_event_refs"]
    ]
    assert state["volumes"][-1]["release_ref"] == release["release_ref"]

    return snapshot


def authorize_next_volume_before_scene(kernel, fixture, gateway, registration, series):
    from novel_factory.application.rolling_plans import RollingPlanning
    from novel_factory.application.volume_successor import register_next_volume

    plans = PlanningService(kernel)
    target, target_ref = make_plan(plans, fixture)
    owner, wr = fixture["principal"], fixture["world_ref"]
    plans.adopt_plan(owner, wr, target_ref, None, "select-volume-two")
    RollingPlanning(kernel).enable(
        owner, wr, target["volume_id"], scenes=1, idempotency_key="lock-volume-two-reference"
    )
    saved = register_next_volume(
        kernel,
        gateway,
        owner,
        world_ref=wr,
        series_id=series,
        from_volume_id=registration["volume_id"],
        next_volume_id=target["volume_id"],
        story_time=point(60),
        subject_ids=[registration["subject_id"], registration["destination_id"]],
        predicates=["located_in"],
        adapter_id=registration["adapter_id"],
        budget_id=registration["budget_id"],
        reserve_amount=registration["reserve_amount"],
        token_reservation=registration["token_reservation"],
        deadline_seconds=3600,
        idempotency_key="preauthorize-next-volume",
    )
    assert saved["status"] == "AWAITING_CONTINUITY"
    return target_ref


def finish_next_volume(
    kernel, fixture, gateway, target_ref, snapshot, settlement, server, requests, statuses
):
    wr, ids, owner = settlement["world_ref"], fixture["ids"], fixture["principal"]
    work, branch = wr["work_id"], wr["branch_id"]

    def tick():
        return RuntimeSupervisor(kernel, gateway).run_once(owner, work, branch)

    with kernel.db.uow(write=False) as repo:
        plan = kernel.artifact(repo, target_ref, work, kind="PLAN_REVISION")["payload"]
        contract = kernel.artifact(repo, plan["volume_contract_ref"], work, kind="VOLUME_CONTRACT")[
            "payload"
        ]

    def candidate(ctx):
        packet = json.loads(requests[-1]["messages"][1]["content"])
        memory = json.loads(packet["authorized_materials"][0]["content"])
        assert memory["snapshot_ref"] == snapshot and ctx["world_ref"] == memory["world_ref"] == wr
        assert plan["nodes"][0]["goal"] in packet["assignment"]
        selected_event = settlement["scenes"][-1]["receipt"]["accepted_event_refs"][0]
        assert memory["entries"][-1]["source_events"][0]["event_ref"] == selected_event
        return {
            "schema_version": "2.0",
            "window_plan_id": str(uuid4()),
            "volume_contract_revision_id": contract["contract_revision_id"],
            "input_world_ref": wr,
            "story_interval": {"start": point(61), "end": point(62), "bounds": "[)"},
            "scope_ids": [ids["south"]],
            "process_ids": [],
            "focus_actor_ids": [ids["protagonist"]],
            "resolution_strategy": "MIXED",
            "candidate_goals": ["在南岸寻找进一步的信息。"],
            "next_decision_reason": "依据后续角色选择再决定。",
            "assumptions": [],
            "adopted_event_refs": [selected_event],
            "expected_outputs": ["REPLAN_REPORT"],
            "dependency_fingerprint": ctx["dependency_fingerprint"],
            "status": "DRAFT",
        }

    server.candidate = candidate
    activation, ran, saved, idle = tick(), tick(), tick(), tick()
    assert [x["status"] for x in (activation, ran, saved, idle)] == [
        "VOLUME_PLANNER_READY",
        "RESULT_STORED",
        "VOLUME_WINDOW_STORED",
        "IDLE",
    ]
    assert activation["task_id"] == ran["task_id"] == saved["task_id"]
    calls = len(requests)
    assert tick()["status"] == "IDLE" and len(requests) == calls == 6
    with kernel.db.uow(write=False) as repo:
        draft = kernel.artifact(repo, saved["window_ref"], work, kind="WINDOW_PLAN")["payload"]
        assert draft["input_world_ref"] == wr and draft["status"] == "DRAFT"
        assert (
            repo.one(
                "selections",
                {
                    "work_id": work,
                    "selection_kind": PlanningService.selection_key(plan["volume_id"]),
                },
            )["artifact_ref"]
            == target_ref
        )
        assert kernel.head(repo, work, branch) == wr
        server_version = repo.conn.exec_driver_sql("SHOW server_version").scalar_one().split()[0]
    if filename := os.environ.get("NF_P5_SERIES_EVIDENCE"):
        evidence = {
            "postgres": server_version,
            "provider": "synthetic loopback HTTP, no commercial inference",
            "workflow_status_counts": dict(
                Counter(statuses + [r["status"] for r in (activation, ran, saved, idle)])
            ),
            "provider_requests": calls,
            "next_volume_requests": 1,
            "additional_requests_on_idle_replay": len(requests) - calls,
            "terminal_scenes": len(settlement["scenes"]),
            "next_volume_window_status": draft["status"],
            "test_inputs": "Three fixture prose drafts. Independent reading, literary approval and continuity approval supplied by authenticated test identities.",
            "funded_model_stages": "Source actor, joint decision, volume review, replanning, upper review, destination planner",
            "preauthorized_before_first_scene": True,
            "paid_provider_usage": None,
        }
        Path(filename).write_text(json.dumps(evidence, indent=2, ensure_ascii=False) + "\n")
