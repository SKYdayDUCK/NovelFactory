"""Joint adjudication receives three sealed actor slots at a fixed cut."""

import json
from uuid import uuid4

import pytest

from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.joint_handoff import create_control_task
from novel_factory.application.runtime_tasks import RuntimeTaskService
from novel_factory.application.simulation import WorldSchedulerService
from novel_factory.application.supervisor import RuntimeSupervisor
from novel_factory.domain.errors import DomainError
from novel_factory.domain.world import state_at
from novel_factory.fixtures import load_fixture, point
from novel_factory.infrastructure.database import tables
from novel_factory.infrastructure.repository import KernelRepository
from test_p5_supervisor import backend
from test_simulation import process_definition, register

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


@pytest.mark.parametrize(
    "case",
    [
        "primary",
        "alternative",
        "no-movement",
        "item-winner",
        "item-two-winners",
        "item-unlisted-recipient",
        "item-wrong-source",
        "other-place",
        "resource",
        "unresolved",
        "bad-refs",
        "revoke-actor",
        "revoked-before-send",
    ],
)
def test_joint_adjudication_is_bounded_durable_and_wakes_director(kernel_env, case, monkeypatch):
    rejected = case not in {"primary", "alternative", "no-movement", "item-winner"}
    kernel = kernel_env["kernel"]
    world = load_fixture(kernel, "white_bridge")
    west = world["ids"]["south"]
    alternatives = [world["ids"]["warehouse"]] if case == "alternative" else []
    process = process_definition(kernel, world, scopes=[west, world["ids"]["warehouse"]])
    world, _ = register(kernel, world, process)
    admin, wr = world["principal"], world["world_ref"]
    work, branch = wr["work_id"], wr["branch_id"]
    actors = process["required_actor_ids"]
    choices = dict(zip(actors, ["今晚不赴宴，我留守粮仓。", "只在收到消息后出发。", "核对库单。"], strict=True))
    item_case = case.startswith("item-")
    gift = {"item_id": world["ids"]["sword"], "from_id": world["ids"]["smith"],
            "recipient_ids": actors[:2]} if item_case else None
    if case == "item-wrong-source":
        gift["from_id"] = world["ids"]["master"]
    gift_options = {"allowed_item_gift": gift} if item_case else {}
    if item_case:
        choices.update({actor: "我要求领取这件物品。" for actor in actors[:2]})
    at = process["due_at"]
    with backend() as (http, requests, provider):
        gateway = AgentGateway(kernel, {"test": http})
        runtime = RuntimeTaskService(kernel, gateway)
        supervisor = RuntimeSupervisor(kernel, gateway)
        scheduler = WorldSchedulerService(kernel)
        budget = kernel.tasks.create_budget(work, "5")

        def enroll(job, cut, **opts):
            supervisor.enroll(
                admin,
                job.task["task_id"],
                manifest_ref=job.manifest_ref,
                knowledge_cut=cut,
                input_ref=job.agent_input["input_ref"],
                budget_id=budget,
                reserve_amount="1",
                token_reservation=100000,
                context_tokens=20000,
                **opts,
            )

        jobs = [
            runtime.prepare(
                admin,
                wr,
                role_id="character_actor",
                task_kind="character_action",
                story_time=at,
                subject_ids=[actor],
                predicates=["located_in"],
                actor_id=actor,
                assignment="依自己的目标行动。",
                adapter_id="test",
                queue_state="READY",
            )
            for actor in actors
        ]
        bundle = scheduler.prepare_interaction(
            admin,
            wr,
            at,
            process["scope_ids"],
            {actor: job.principal.grant_id for actor, job in zip(actors, jobs)},
            "banquet",
        )
        iid = bundle["interaction_id"]
        for actor, job in zip(actors, jobs):
            enroll(job, {"plane": "BELIEF", "actor_id": actor, "known_at": at}, seal_interaction_id=iid)
        control = create_control_task(
            kernel, admin, iid, subject_id=actors[0], destination_id=west, alternative_destination_ids=alternatives, **gift_options
        )
        assert (
            create_control_task(
                kernel,
                admin,
                iid,
                subject_id=actors[0],
                destination_id=west,
                alternative_destination_ids=alternatives, **gift_options,
            )
            == control
        )
        joint = runtime.prepare(
            admin,
            wr,
            role_id="adjudication_advisor",
            task_kind="joint_outcome",
            skill_name="adjudicate-actions",
            story_time=at,
            subject_ids=list(dict.fromkeys([actors[0], west, *alternatives,
                                            *([gift["item_id"], gift["from_id"], *actors[:2]] if item_case else [])])),
            predicates=["located_in", "legal_owner", "possessed_by"] if item_case else ["located_in"],
            input_artifacts=[bundle["interaction_ref"]],
            assignment="依规则裁决三个密封提案。",
            adapter_id="test",
            queue_state="BLOCKED",
        )
        location = dict(
            interaction_id=iid, subject_id=actors[0], destination_id=west, control_task_id=control["task_id"], **gift_options
        )
        if alternatives:
            location["alternative_destination_ids"] = alternatives
        with pytest.raises(DomainError):
            enroll(joint, {"plane": "WORLD"}, joint={**location, "destination_id": world["ids"]["north"]})
        enroll(joint, {"plane": "WORLD"}, joint=location)
        director = runtime.prepare(
            admin,
            wr,
            role_id="volume_director",
            task_kind="volume_review",
            story_time=at,
            subject_ids=[actors[0]],
            predicates=["located_in"],
            assignment="等正式世界回执。",
            adapter_id="test",
            queue_state="BLOCKED",
        )
        opts = dict(
            work_id=work,
            branch_id=branch,
            authorize_in_tx=lambda conn: kernel.authorize(
                KernelRepository(conn, tables), admin, work, "ADMIN", branch
            ),
        )
        supervisor.workflow.wait(
            joint.task["task_id"], 0, [job.task["task_id"] for job in jobs], mode="AND", **opts
        )
        supervisor.workflow.wait(director.task["task_id"], 0, [control["task_id"]], mode="AND", **opts)

        def candidate(ctx):
            packet = json.loads(requests[-1]["messages"][1]["content"])
            if ctx["role_id"] == "character_actor":
                assert "joint" not in packet
                return {
                    "schema_version": "2.0",
                    "proposal_id": str(uuid4()),
                    "task_id": ctx["task_id"],
                    "generation": ctx["generation"],
                    "context_bundle_id": ctx["context_bundle_id"],
                    "world_ref": wr,
                    "actor_id": ctx["actor_id"],
                    "intent": choices[ctx["actor_id"]],
                    "targets": [],
                    "evidence_refs": [],
                    "story_interval": {"start": at, "end": point(31), "bounds": "[)"},
                    "resource_requests": [],
                    "preconditions": [],
                    "dependency_fingerprint": ctx["dependency_fingerprint"],
                    "idempotency_key": "action",
                }
            data = packet["joint"]
            assert {
                p["proposal"]["actor_id"]: p["proposal"]["intent"] for p in data["action_proposals"]
            } == choices
            effect = {
                "kind": "RELATION_SET",
                "predicate": "located_in",
                "value_status": "VALUE",
                "effect_key": "stay",
                "subject_id": actors[0],
                "object_id": world["ids"]["north"] if case == "other-place" else west,
            }
            if case == "alternative":
                assert data["allowed_location"]["alternative_destination_ids"] == alternatives
                effect["object_id"] = alternatives[0]
            if case == "resource":
                effect = {
                    "kind": "RESOURCE_DELTA",
                    "resource_id": world["ids"]["grain"],
                    "quantity": "10",
                    "effect_key": "unapproved",
                    "unit": "bag",
                    "account_id": west,
                }
            if item_case:
                assert data["allowed_item_gift"] == gift
                effect.update(predicate="legal_owner", subject_id=gift["item_id"],
                              object_id=actors[2] if case == "item-unlisted-recipient" else actors[0])
            effects = [] if case == "no-movement" else [effect]
            if case == "item-two-winners":
                effects.append(dict(effect, object_id=actors[1], effect_key="second"))
            refs = [p["ref"] for p in data["action_proposals"]]
            return {
                "schema_version": "2.0",
                "proposal_id": str(uuid4()),
                "input_world_ref": wr,
                "interaction_id": iid,
                "action_proposal_refs": refs[:-1] if case == "bad-refs" else refs,
                "ruleset_ref": data["ruleset_ref"],
                "story_time": at,
                "effect_group_id": str(uuid4()),
                "representation_id": str(uuid4()),
                "effects": effects,
                "reasoning_summary": "角色自主留守，宴会照常。",
                "unresolved_questions": ["需要确认"] if case == "unresolved" else [],
                "dependency_fingerprint": ctx["dependency_fingerprint"],
            }

        provider.candidate = candidate
        for _ in range(3):
            assert supervisor.run_once(admin, work, branch)["status"] == "RESULT_STORED"
        assert len(requests) == 3
        if case == "revoked-before-send":
            messages = gateway._messages

            def revoke_after_projection(principal, binding):
                packet = messages(principal, binding)
                assert binding["task"]["task_id"] == joint.task["task_id"]
                kernel.revoke_grant(admin, jobs[0].principal.grant_id)
                return packet

            monkeypatch.setattr(gateway, "_messages", revoke_after_projection)
            refused = supervisor.run_once(admin, work, branch)
            assert (refused["status"], refused["reason"]) == ("BLOCKED", "VIEW_DENIED")
            assert len(requests) == 3
            with kernel.db.uow(write=False) as repo:
                assert not repo.one("call_attempts", {"task_id": joint.task["task_id"]})
                assert not repo.one("simulation_outcomes", {"interaction_id": iid})
                assert kernel.head(repo, work, branch) == wr
            assert kernel.tasks.get(director.task["task_id"])["state"] == "BLOCKED"
            return
        turn = supervisor.run_once(admin, work, branch)
        assert turn["status"] == "RESULT_STORED", turn
        assert kernel.tasks.get(director.task["task_id"])["state"] == "BLOCKED"
        assert len(requests) == 4
        if case == "revoke-actor":
            kernel.revoke_grant(admin, jobs[0].principal.grant_id)
        if case == "primary":

            def crash_after_accept(self, *args, **kwargs):
                original(self, *args, **kwargs)
                raise RuntimeError("lost before candidate delivery receipt")

            original = type(kernel).accept
            with monkeypatch.context() as patch:
                patch.setattr(type(kernel), "accept", crash_after_accept)
                with pytest.raises(RuntimeError, match="lost before"):
                    supervisor.run_once(admin, work, branch)
            with kernel.db.uow(write=False) as repo:
                assert kernel.head(repo, work, branch) == wr
                assert not repo.one("simulation_outcomes", {"interaction_id": iid})
            supervisor = RuntimeSupervisor(kernel, gateway)
        result = supervisor.run_once(admin, work, branch)
        with kernel.db.uow(write=False) as repo:
            selected = repo.one("simulation_outcomes", {"interaction_id": iid})
            if rejected:
                assert result["status"] == "WAITING_REVIEW", result
                assert not selected and kernel.head(repo, work, branch) == wr
                assert kernel.tasks.get(control["task_id"])["generation"] == 0
            else:
                assert result["status"] == "JOINT_ACCEPTED", result
                assert selected["payload"]["source_kind"] == "EXTERNAL_AGENT"
                assert selected["payload"]["source_ref"] == result["candidate_ref"]
                assert kernel.head(repo, work, branch) == result["world_ref"]
                snapshot = kernel.revision(repo, result["world_ref"])["snapshot"]
                actual = state_at(snapshot, actors[0], "located_in", at)
                if case == "item-winner":
                    assert len(selected["payload"]["commands"]) == 1
                    assert selected["payload"]["commands"][0]["template_id"] == "TRANSFER_ITEM"
                    assert len(result["accepted_event_refs"]) == 1
                    for predicate in ("legal_owner", "possessed_by"):
                        assert state_at(snapshot, gift["item_id"], predicate, at)["data"]["entity_id"] == actors[0]
                elif case == "no-movement":
                    assert result["accepted_event_refs"] == [] and selected["payload"]["commands"] == []
                    assert actual == state_at(kernel.revision(repo, wr)["snapshot"], actors[0], "located_in", at)
                else:
                    assert result["accepted_event_refs"]
                    assert actual["data"]["entity_id"] == (alternatives[0] if alternatives else west)
                assert kernel.tasks.get(control["task_id"])["state"] == "ACCEPTED"
        assert supervisor.workflow.resume_project(**opts)["awakened"] == (0 if rejected else 1)
        assert kernel.tasks.get(director.task["task_id"])["state"] == ("BLOCKED" if rejected else "READY")
        assert supervisor.run_once(admin, work, branch)["status"] == "IDLE" and len(requests) == 4
