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
    ("deviation", "preset", "harness"), [(False, "medium_long", False), (True, "medium_long", False),
                                        (False, "short", False), (False, "medium_long", True)]
)
def test_reviewed_design_opens_sealed_interaction_before_any_writing(
    kernel_env, tmp_path, monkeypatch, deviation, preset, harness
):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    cli = importlib.import_module("p6_idea")
    if harness:
        from novel_factory.infrastructure.creative_config import CreativeHarnessConfig
        monkeypatch.setenv("NF_CREATIVE_HARNESS_CONFIG", json.dumps(CreativeHarnessConfig(
            enabled=True, dynamic_agents=True, adaptive_instructions=True).as_dict()))
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
    across = preset == "medium_long" and not deviation
    if across:
        idea["length"].update(min=3, max=3)
    allowance = {
        "adapter_id": "test",
        "reserve_amount": "0.1",
        "total_amount": "5",
        "currency": "USD",
        "total_tokens": 2000000,
        "token_reservation": 100000,
    }
    if across:
        allowance["production_mode"] = "quality"
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
        inherited_reads = []

        def answer(packet):
            if harness:
                assert "context" not in packet and "creative_materials" in packet
                system = _requests[-1]["messages"][0]["content"]
                ctx = json.loads(system.split("\nIdentity: ", 1)[1].splitlines()[0])
                # The fixture only translates bytes actually offered on the wire for the
                # shared responder. It does not consult a DB or reconstruct omitted fields.
                materials = packet["creative_materials"]
                refs = [item["value"] for item in packet.get("structured_evidence", [])]
                fixture_reads = []
                for scene in materials.get("scene", []):
                    event_sets = [v["required_event_refs"] for v in refs if "required_event_refs" in v]
                    fixture_reads.append({"scene_contract": scene, "required_event_refs": sum(event_sets, [])})
                for passage in materials.get("prose", []):
                    info = next((v for v in refs if "declared_important_deltas" in v), {})
                    fixture_reads.append({**info, "text": passage})
                if ctx["role_id"] == "reviewer" and materials.get("prose"):
                    sources = [v for v in refs if "prose_index" in v]
                    assert [v["prose_index"] for v in sources] == list(range(len(materials["prose"])))
                    assert all(v["draft_ref"] and v["span"] for v in sources)
                if materials.get("continuity"):
                    inherited_reads.extend(materials["continuity"])
                packet = {**packet, **packet.get("candidate_workflow", {}), "context": ctx,
                          "authorized_materials": [{"content": json.dumps(v)} for v in fixture_reads]}
            else:
                ctx = packet["context"]
            role = ctx["role_id"]
            if role == "world_designer":
                return design
            if role in {"writer", "evidence_maintainer"}:
                from test_p6_rolling_production import answer as write

                result = write(packet)
                if role == "writer":
                    card = next(json.loads(i["content"]) for i in packet["authorized_materials"]
                                if "scene_contract" in json.loads(i["content"]))
                    for material in packet["authorized_materials"]:
                        data = json.loads(material["content"])
                        if isinstance(data, dict) and "snapshot_ref" in data and "task_world_ref" in data:
                            inherited_reads.append(data)
                    result["event_refs"] = card["required_event_refs"]
                    if card["required_event_refs"]:
                        result["text"] += "渡船靠岸。乔动身了。"
                else:
                    read = json.loads(packet["authorized_materials"][0]["content"])
                    raw = json.loads(result["text"])
                    events = read.get("required_event_refs", [])
                    if events:
                        assert len(events) == 2
                        raw["assertions"][0]["quote"] = "北窗的雨声很轻。"
                        for event, passage in zip(events, ("渡船靠岸。", "乔动身了。"), strict=True):
                            raw["assertions"].append({**raw["assertions"][0], "quote": passage,
                                                      "classification": "EXISTING_EVENT_PRESENTATION", "event_ref": event})
                    result["text"] = json.dumps(raw)
                return result
            if role == "volume_director" and "editorial" not in packet:
                assert "simulation_scope" in json.loads(packet["assignment"])
                return {"volume_brief": "再会", "chapter_goals": ["重回码头"],
                        "scene_situation": "渡船重新靠近市场", "environment_destination": "INITIAL_PLACE"}
            if role == "reviewer" and "review_serial_continuity" in packet["assignment"]:
                reading = json.loads(packet["assignment"])
                assert reading["source_text"] and reading["new_source_entries"]
                return {"decision": "APPROVED", "reason": "Scoped regression reading.",
                        "deferred_questions": [], "evidence_refs": []}
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
                later = scene["starting_situation"] == "渡船重新靠近市场"
                assert scene["authorized_entities"][locations["乔"]["place_id"]] == ("旧桥" if later else "市场")
                assert scene["authorized_entities"][locations["林"]["place_id"]] == "市场"
                if preset == "medium_long":
                    assert scene["authorized_entities"][locations["末班渡船"]["place_id"]] == ("市场" if later else "旧桥")
                    assert locations["末班渡船"]["event_refs"]
                assert "欠薪" not in packet["assignment"] and "父亲嘱咐" not in packet["assignment"]
                options = protocol["authorized_effect_choices"]
                assert {e["subject_id"] for e in options} == {joint["allowed_location"]["subject_id"]}
                return {
                    **protocol["copy_into_payload"],
                    "effects": [options[1] if later else options[0]],
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

        if across:
            _check_subsequent_volume(kernel, gateway, owner, work, branch, turn, source, actors)
            assert len(inherited_reads) == 1
            memory = inherited_reads[0]
            if harness:
                assert memory and all(e["interpretation"] and "planned_payoff_at" not in e for e in memory)
            else:
                assert memory["world_ref"] == turn["world_ref"] and memory["task_world_ref"] != memory["world_ref"]
                assert memory["entries"] and all("planned_payoff_at" not in e for e in memory["entries"])


def _check_subsequent_volume(kernel, gateway, owner, work, branch, opening, source, actors):
    from editorial_helpers import approve_binding, binding_for
    from novel_factory.application.release import ReleaseService
    from novel_factory.application import rolling_production as rolling

    supervisor = RuntimeSupervisor(kernel, gateway)
    def until(status, limit=65):
        steps = []
        for _ in range(limit):
            result = supervisor.run_once(owner, work, branch)
            steps.append((result.get("status"), result.get("reason")))
            if result.get("status") == status:
                return result
        pytest.fail(str(steps))

    until("NEXT_PLAN_ACCEPTED")
    with kernel.db.uow(write=False) as repo:
        cursor = rolling._load(kernel, repo, work, branch)
        first = rolling._window(kernel, repo, cursor)["volume_id"]
        wr = kernel.head(repo, work, branch)
        scenes = rolling.frozen_volume_scenes(kernel, repo, cursor, first)
        accepted = [{**kernel.artifact(repo, s["acceptance_ref"], work)["payload"],
                     "acceptance_ref": s["acceptance_ref"]} for s in scenes]
        first_scene = kernel.artifact(repo, scenes[0]["scene_ref"], work)["payload"]
        assert wr == opening["world_ref"]
    for _ in range(2):
        supervisor.run_once(owner, work, branch)
    f = {"world_ref": wr, "principal": owner, "plan_ref": first_scene["plan_ref"],
         "narrative_revision_id": first_scene["scene_contract"]["narrative_revision_id"],
         "ids": {"sword": _uid(work, "place"), "protagonist": _uid(work, "protagonist")}}
    b = binding_for(f, accepted)
    b["arrangement_version"] = first_scene["scene_contract"]["arrangement_version"]
    approve_binding(kernel, f, b)
    released = until("ROLLING_VOLUME_RELEASED")
    release = ReleaseService(kernel).get_release(owner, work, released["release_ref"])
    with kernel.db.uow(write=False) as repo:
        assert kernel.head(repo, work, branch) == wr
    reviewed = until("CONTINUITY_REVIEW_APPROVED")
    assert reviewed["pages"] == len(release["manifest"]["ordered_artifacts"])
    with kernel.db.uow(write=False) as repo:
        decisions = repo.many("receipts", {"work_id": work, "command_type": "P6_SERIAL_PAGE_PASS"})
        assert len(decisions) == reviewed["pages"]
        assert all(d["payload"]["attempt_id"] for d in decisions)
    until("ROLLING_CONTINUITY_PUBLISHED")
    enrol = until("VOLUME_SIMULATION_ENROLLED")
    with kernel.db.uow(write=False) as repo:
        first_joint = kernel.artifact(repo, opening["joint_outcome_ref"], work)["payload"]
        assert enrol["interaction_id"] != first_joint["interaction_id"]
    accepted = until("VOLUME_SIMULATION_ACCEPTED")
    assert accepted["world_ref"] != wr
    assert all("上次" in b for b in actors.values())
    with kernel.db.uow(write=False) as repo:
        assert repo.ancestor(wr, accepted["world_ref"])
        saved = kernel.artifact(repo, accepted["handoff_ref"], work, kind="P6_VOLUME_SCENE_HANDOFF")["payload"]
        assert saved["joint_outcome_ref"] == accepted["joint_outcome_ref"]
        assert saved["accepted_event_refs"] == accepted["accepted_event_refs"]
        record = kernel.artifact(repo, accepted["scene_ref"], work)["payload"]
        assert record["release"] == released["release_ref"] and record["environment_events"]
    # The following writer must actually receive the published first-volume memory on its new cut.
    steps = []
    for _ in range(30):
        r = supervisor.run_once(owner, work, branch)
        steps.append((r.get("status"), r.get("reason"), r.get("validation_status")))
        cursor = rolling.rolling_status(kernel, owner, work, branch)
        if cursor["frozen_chapters"] == 3:
            break
    from novel_factory.application.production_pipeline import pipeline_status
    board = pipeline_status(kernel, owner, work, branch)
    assert cursor["frozen_chapters"] == 3, (steps, cursor["reason"], [(k, v["state"], v.get("reason")) for k, v in board["nodes"].items()])


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


def test_serial_reader_projects_only_earlier_approved_reader_evidence_on_exact_source_pages():
    from novel_factory.application.serial_continuity_review import _pages

    def entry(key, plane, source, ordinal):
        return {"entry_id": key, "plane": plane, "volume_ordinal": ordinal, "interpretation": key,
                "source_texts": [{"text_ref": source, "span": {"start": 0, "end": 1}}]}

    entries = [entry("read-before", "READER", "earlier", 1), entry("plan-secret", "LITERARY", "earlier", 1),
               entry("event", "WORLD_EVIDENCE", "earlier", 1), entry("disclosure", "READER", "later", 2)]
    pages = _pages({"candidate": {"input_sha256": "frozen", "request": {
                        "ordered_release_refs": ["old", "new"], "entries": entries}},
                    "original_texts": [{"volume_ordinal": 1, "text_ref": "earlier", "text": "a" * 8001},
                                       {"volume_ordinal": 2, "text_ref": "later", "text": "b"}]})
    assert "".join(p["source_text"] for p in pages) == "a" * 8001 + "b"
    assert [p["range"] for p in pages] == [[0, 8000], [8000, 8001], [0, 1]]
    assert all([e["entry_id"] for e in p["previous_reader_evidence"]] == ["read-before"] for p in pages)
    assert all(not p["new_source_entries"] for p in pages[:-1])
    assert [e["entry_id"] for e in pages[-1]["new_source_entries"]] == ["disclosure"]
    assert "plan-secret" not in json.dumps(pages)
