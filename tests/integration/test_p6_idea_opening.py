"""Actual native kernel acceptance with a local fixture at the HTTP boundary."""

import importlib
import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest
from test_p5_supervisor import backend

from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.idea_intake import _uid
from novel_factory.application.production_pipeline import production_material_reader
from novel_factory.application.simulation import WorldSchedulerService
from novel_factory.application.supervisor import RuntimeSupervisor


@pytest.mark.postgres
@pytest.mark.integration
@pytest.mark.parametrize(
    ("deviation", "preset"), [(False, "medium_long"), (True, "medium_long"), (False, "short")]
)
def test_reviewed_design_opens_sealed_interaction_before_any_writing(
    kernel_env, tmp_path, monkeypatch, deviation, preset
):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    cli = importlib.import_module("p6_idea")
    kernel = kernel_env["kernel"]
    idea = {
        "idea": "两人在市集丢了信。",
        "genre": "现实",
        "length": {"preset": preset, "unit": "chapters", "min": 2, "max": 2},
    }
    design = {
        "title": "旧桥",
        "protagonist": "乔",
        "place": "市场",
        "storyline": "寻找物主",
        "world_rules": ["来信不得代领"],
        "chapter_goals": ["寻找目击者", "对证"],
        "opening": {
            "counterpart": "林",
            "other_place": "旧桥",
            "protagonist_knowledge": "自己的委托人欠薪",
            "counterpart_knowledge": "父亲嘱咐我留下",
            "situation": "市场即将闭市",
            "environment": {"moving_entity": "末班渡船"},
        },
    }
    allowance = {
        "adapter_id": "test",
        "reserve_amount": "0.1",
        "total_amount": "5",
        "currency": "USD",
        "total_tokens": 2000000,
        "token_reservation": 100000,
    }
    with backend() as (adapter, _requests, server):
        generate = adapter.generate
        monkeypatch.setattr(
            adapter,
            "generate",
            lambda request: replace(generate(request), actual_amount=Decimal(0), currency="USD"),
        )
        gateway = AgentGateway(
            kernel, {"test": adapter}, material_reader=production_material_reader(kernel)
        )
        actors = {}

        def answer(packet):
            ctx = packet["context"]
            role = ctx["role_id"]
            if role == "world_designer":
                return design
            if role == "reviewer":
                return {
                    "summary": "fixture inspected sources",
                    "issues": [],
                    "counterinterpretations": [],
                    "evidence_refs": [],
                }
            if role == "character_actor":
                actor = ctx["actor_id"]
                actors[actor] = packet["assignment"]
                return {
                    **packet["proposal_protocol"]["copy_into_payload"],
                    "intent": "先寻找证据",
                    "targets": [],
                    "resource_requests": [],
                    "preconditions": [],
                    "evidence_refs": [],
                }
            if role == "adjudication_advisor":
                joint = packet["joint"]
                protocol = packet["proposal_protocol"]
                scene = json.loads(packet["assignment"].split("\n", 1)[1])
                assert scene["setting_rules"] == ["来信不得代领"]
                assert set(scene["authorized_entities"].values()) == (
                    {"乔", "林", "市场", "旧桥"} | ({"末班渡船"} if preset != "short" else set())
                )
                locations = {scene["authorized_entities"][r["subject_id"]]: r for r in scene["last_recorded_locations"]}
                assert scene["authorized_entities"][locations["乔"]["place_id"]] == "市场"
                assert scene["authorized_entities"][locations["林"]["place_id"]] == "市场"
                if preset == "medium_long":
                    assert scene["authorized_entities"][locations["末班渡船"]["place_id"]] == "旧桥"
                    assert locations["末班渡船"]["event_refs"]
                assert "欠薪" not in packet["assignment"] and "父亲嘱咐" not in packet["assignment"]
                options = protocol["authorized_effect_choices"]
                assert {e["subject_id"] for e in options} == {joint["allowed_location"]["subject_id"]}
                return {
                    **protocol["copy_into_payload"],
                    "effects": [options[0]],
                    "reasoning_summary": "乔前往旧桥",
                    "unresolved_questions": [],
                }
            editorial = packet["editorial"]
            if editorial["operation"] == "request_change":
                return {
                    "text": "改去桥头等信" if deviation else "原计划仍可实行",
                    "important_deltas": ["会面地点改变"] if deviation else [],
                    "event_refs": editorial["accepted_event_refs"],
                    "evidence_refs": [],
                }
            if editorial["operation"] == "propose_window":
                work = ctx["world_ref"]["work_id"]
                return {
                    **packet["proposal_protocol"]["copy_into_payload"],
                    "scope_ids": [_uid(work, "place")],
                    "focus_actor_ids": [_uid(work, "protagonist")],
                    "process_ids": [],
                    "resolution_strategy": "MICRO_INTERACTION",
                    "candidate_goals": ["改去桥头等信", "核实地址"],
                    "next_decision_reason": "等来信",
                    "assumptions": [],
                    "expected_outputs": ["SCENE_CONTRACT"],
                }
            assert editorial["operation"] == "review_and_adopt"
            return {
                "decision": "APPROVED",
                "reason": "核实变更与裁决记录相符",
                "deferred_questions": [],
                "evidence_refs": [],
            }

        server.packet_candidate = answer
        private = tmp_path / "idea"
        cli.run_intake("enroll", kernel, gateway, private, idea, allowance)
        for _ in range(12):
            source = cli.run_intake("advance", kernel, gateway, private)
            if source.get("status") == "INITIAL_PLAN_MATERIALIZED":
                break
        assert source["opening_version"] == 1
        auth = json.loads((private / "supervisor.json").read_text())
        owner = kernel.authenticate(auth["token"])
        work, branch = auth["work_id"], auth["branch_id"]
        result = cli.run_intake("register-window", kernel, gateway, private, idea, allowance)
        assert result == cli.run_intake(
            "register-window", kernel, gateway, private, idea, allowance
        )
        assert result["writing_registration"]["status"] == "OPENING_ENROLLED"
        with kernel.db.uow(write=False) as repo:
            assert not repo.many("tasks", {"work_id": work, "role_id": "writer"})
            wr = kernel.head(repo, work, branch)
        processes = WorldSchedulerService(kernel).processes(owner, wr)
        assert sorted(p["world_stage"] for p in processes) == (
            ["ACTIVE", "COMPLETED"] if preset == "medium_long" else ["ACTIVE"]
        )
        assert bool(result["writing_registration"]["director_task_id"]) == (preset != "short")
        supervisor = RuntimeSupervisor(kernel, gateway)
        statuses = []
        for _ in range(25):
            turn = supervisor.run_once(owner, work, branch)
            statuses.append((turn.get("status"), turn.get("reason")))
            if turn.get("status") == "OPENING_ACCEPTED":
                break
        assert turn.get("status") == "OPENING_ACCEPTED", statuses
        assert len(actors) == 2
        assert all("市场" in brief and "末班渡船" not in brief for brief in actors.values())
        assert "父亲嘱咐" not in actors[_uid(work, "protagonist")]
        assert "欠薪" not in actors[_uid(work, "counterpart")]
        assert all(
            p["world_stage"] == "COMPLETED"
            for p in WorldSchedulerService(kernel).processes(owner, turn["world_ref"])
        )
        with kernel.db.uow(write=False) as repo:
            opening = kernel.artifact(repo, turn["opening_ref"], work)["payload"]
            assert bool(opening["environment_events"]) == (preset == "medium_long")
            assert len(turn["accepted_event_refs"]) > len(opening["environment_events"])
            cards = repo.many("artifacts", {"work_id": work, "kind": "SCENE_PLAN"})
            card = next(c["payload"] for c in cards if c["payload"]["required_event_refs"])
            assert card["required_event_refs"] == turn["accepted_event_refs"]
            assert card["scene_contract"]["input_world_ref"] == turn["world_ref"]
            assert "旧桥" in card["literary_purpose"] and (
                "末班渡船" in card["literary_purpose"]
            ) == (preset == "medium_long")
            assert card["literary_purpose"].startswith(
                "改去桥头等信" if deviation else "寻找目击者"
            )
            assert (turn["reviewed_plan_ref"] != source["plan_ref"]) == deviation
            from novel_factory.application.editorial_effects import scoped_accepted_effects

            projection = scoped_accepted_effects(
                kernel, repo, turn["world_ref"], {"tick": 1, "phase": 0}, source, card["required_event_refs"]
            )
            assert projection["accepted_event_refs"] == turn["accepted_event_refs"]
            assert any(
                e.get("subject_id") == _uid(work, "protagonist")
                for event in projection["accepted_effects"] for e in event["effects"]
            )


def test_split_model_design_contract_requires_profile_specific_opening():
    from novel_factory.contracts.registry import validate_contract
    from novel_factory.domain.errors import DomainError

    design = dict(title="风", protagonist="陆", place="桥", storyline="寻人", world_rules=["一日两班"], chapter_goals=["等候"])
    validate_contract("FirstWorldDesign", design, "production_profile")
    with pytest.raises(DomainError):
        validate_contract("OpeningWorldDesign", design, "production_profile")
    opening = dict(counterpart="周", other_place="埠", protagonist_knowledge="已领工钱", counterpart_knowledge="记得信号", situation="班船晚点")
    validate_contract("OpeningWorldDesign", {**design, "opening": opening}, "production_profile")
    with pytest.raises(DomainError):
        validate_contract("EnvironmentalOpeningWorldDesign", {**design, "opening": opening}, "production_profile")
    validate_contract("EnvironmentalOpeningWorldDesign", {**design, "opening": {**opening, "environment": {"moving_entity": "渡船"}}}, "production_profile")
    with pytest.raises(DomainError):
        validate_contract("OpeningWorldDesign", {**design, "protagonist": "陆，年龄二十。", "opening": opening}, "production_profile")
