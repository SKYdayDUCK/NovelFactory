"""R1-C/G exact PostgreSQL counterexamples; all story content is TEST_FIXTURE.

Health is an explicitly declared conserved health-point resource in this finite
fixture. A loss sink records the three consumed points; this is not a general
health simulator or a new kernel predicate.
"""
import json
import os
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest
from test_narrative import arrange, assertion
from test_release import runtime_grant, task_for
from test_simulation import process_definition, register, revision, rules

from novel_factory.application.narrative import NarrativeService
from novel_factory.application.planning import PlanningService
from novel_factory.application.queries import WorldQueryService
from novel_factory.application.simulation import WorldSchedulerService, outcome_command
from novel_factory.domain.canonical import fingerprint
from novel_factory.domain.errors import DomainError
from novel_factory.domain.propositions import encode_proposition
from novel_factory.domain.world import resource_at, state_at
from novel_factory.fixtures import (
    create_bound_task,
    entity_value,
    fixture_definition,
    load_fixture,
    point,
    prepare_command,
    run_command,
)

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def evidence(name, payload):
    directory = os.environ.get("NF_EVIDENCE_DIR")
    if directory:
        path = Path(directory) / "p2_case_extensions" / (name + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"execution_kind": "TEST_FIXTURE_REAL_POSTGRESQL", **payload},
                                   ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


@pytest.mark.parametrize("known_lower_bound", [True, False])
def test_r1_c_unproven_inbound_blocks_related_scope_but_not_remote(kernel_env, known_lower_bound):
    k = kernel_env["kernel"]
    initial = load_fixture(k, "white_bridge")
    ids = initial["ids"]
    channel = {"channel_id": "incomplete-west-east", "from_scope_id": ids["south"],
               "to_scope_id": ids["north"], "min_delay_ticks": 2 if known_lower_bound else None,
               "earliest_possible_impact": point(45) if known_lower_bound else None,
               "completeness": "UNDECLARED", "evidence_refs": []}
    process = process_definition(k, initial, name="unproven-channel", scopes=[ids["south"]],
                                 start=35, due=45, channels=[channel])
    process["process_kind"] = "MESSAGE"
    f, _ = register(k, initial, process)
    with pytest.raises(DomainError) as blocked:
        run_command(k, f["principal"], f["world_ref"], "ADVANCE_SCOPE",
                    {"scope_id": ids["north"], "target": point(40)})
    assert blocked.value.code == "ADVANCE_BLOCKED"
    with pytest.raises(DomainError) as interaction:
        WorldSchedulerService(k).prepare_interaction(f["principal"], f["world_ref"], point(42),
                                                     [ids["north"]], {}, "unproved-interaction")
    assert interaction.value.code == "ADVANCE_BLOCKED"
    independent = run_command(k, f["principal"], f["world_ref"], "ADVANCE_SCOPE",
                              {"scope_id": ids["reserve"], "target": point(40)})
    rev = revision(k, independent["world_ref"])
    assert rev["frontiers"][ids["north"]] == point(1, 1)
    assert rev["frontiers"][ids["reserve"]] == point(40)
    evidence("C02-" + str(known_lower_bound), {"world_ref": independent["world_ref"],
             "channel": channel, "related_errors": [blocked.value.code, interaction.value.code],
             "frontiers": rev["frontiers"], "receipt": independent["receipt"]})


def _chosen_command(k, f, chosen):
    with k.db.uow(write=False) as repo:
        interaction = repo.one("simulation_interactions", {"interaction_id": chosen["interaction_id"]})
        outcome = k.artifact(repo, chosen["outcome_ref"], f["world_ref"]["work_id"])["payload"]
    return outcome_command(interaction["artifact_ref"], interaction["payload"], chosen["outcome_ref"], outcome)


def test_r1_c_delayed_message_observation_and_duplicate_delivery_preserve_knowledge_time(kernel_env):
    k = kernel_env["kernel"]
    initial = load_fixture(k, "white_bridge")
    ids = initial["ids"]
    source = k.put_artifact(initial["principal"], initial["world_ref"]["work_id"],
                            "TEST_FIXTURE：南岸信使报告铁匠仍保管剑。".encode(), "EVIDENCE")
    process = process_definition(k, initial, name="delayed-observation", scopes=[ids["south"], ids["protagonist"]],
        start=29, due=31, channels=[{"channel_id": "two-day-message", "from_scope_id": ids["south"],
        "to_scope_id": ids["north"], "min_delay_ticks": 2, "earliest_possible_impact": point(31),
        "completeness": "COMPLETE", "evidence_refs": rules(k, initial["world_ref"])}])
    process.update(process_kind="MESSAGE", evaluator_kind="DETERMINISTIC", required_actor_ids=[],
        participant_ids=[ids["protagonist"]], deterministic_commands=[{"template_id": "RECORD_COGNITION", "args": {
            "actor_id": ids["protagonist"], "record_kind": "OBSERVATION", "epistemic_mode": "HEARD",
            "statement": encode_proposition(ids["sword"], "possessed_by", entity_value(ids["smith"])),
            "evidence_refs": [{"kind": "ARTIFACT", "artifact_ref": source}], "at": point(31)}}])
    f, _ = register(k, initial, process)
    scheduler = WorldSchedulerService(k)
    with pytest.raises(DomainError):
        scheduler.evaluate_due(f["principal"], f["world_ref"], point(30), process["scope_ids"], "too-early")
    assert not revision(k, f["world_ref"])["snapshot"]["cognitions"]
    chosen = scheduler.evaluate_due(f["principal"], f["world_ref"], point(31), process["scope_ids"], "delivery")
    assert not revision(k, f["world_ref"])["snapshot"]["cognitions"]  # prepared is not delivered
    prepared = prepare_command(k, f["principal"], f["world_ref"], "JOINT_INTERACTION", _chosen_command(k, f, chosen), key="message-accept")
    receipt = k.accept(f["principal"], prepared["request"])
    assert k.accept(f["principal"], prepared["request"]) == receipt
    current = revision(k, receipt["world_ref"])
    assert len(current["snapshot"]["cognitions"]) == 1
    assert len(current["selection"]) == 1
    assert current["snapshot"]["cognitions"][0]["acquired_at"] == point(31)
    assert current["snapshot"]["cognitions"][0]["record_kind"] == "OBSERVATION"
    actor = k.issue_grant(f["principal"], f["world_ref"]["work_id"], role_id="character_actor",
        actor_id=ids["protagonist"], capabilities=["READ", "COMPILE", "TASK_CREATE", "TASK_CLAIM", "ARTIFACT_WRITE"],
        planes=["BELIEF"], max_story_time=point(40))["principal"]
    answers, observations = [], []
    for day in (30, 31):
        task = create_bound_task(k, actor, receipt["world_ref"], point(day), [ids["sword"]],
                                 ["possessed_by"], plane="BELIEF", actor_id=ids["protagonist"])
        request = {"schema_version": "2.1", "task_id": task["task_id"],
            "generation": task["generation"], "read_contract": {"revision": {"mode": "FIXED", "world_ref": receipt["world_ref"]},
            "process_policy": "ALLOW_PARTIAL", "index_policy": "AUTHORITATIVE", "max_wait_ms": 0},
            "knowledge_cut": {"plane": "BELIEF", "actor_id": ids["protagonist"], "known_at": point(day)},
            "subject_time": {"kind": "POINT", "at": point(day)},
            "query": {"kind": "STATE", "subject_ids": [ids["sword"]], "predicates": ["possessed_by"]},
            "page_size": 10, "cursor": None}
        result = WorldQueryService(k).query(actor, request)
        answers.append(result["payload"]["state_items"][0]["answer"])
        request["query"] = {"kind": "TIMELINE", "subject_ids": [ids["sword"]],
            "entry_kinds": ["OBSERVATION"], "time_axis": "SOURCE", "include_inactive": True}
        observations.append(WorldQueryService(k).query(actor, request)["payload"]["entries"])
    assert answers[0]["knowledge_status"] == "UNKNOWN" and answers[0]["evidence_refs"] == []
    assert answers[1]["knowledge_status"] == "UNKNOWN" and answers[1]["evidence_refs"] == []
    assert observations[0] == []
    assert len(observations[1]) == 1
    assert observations[1][0]["source_story_time"]["at"] == point(31)
    # Receipt of a message is an observation. Adoption as a belief requires a
    # distinct formal event, even when its proposition happens to be true.
    adopted = run_command(k, f["principal"], receipt["world_ref"], "RECORD_COGNITION", {
        "actor_id": ids["protagonist"], "record_kind": "BELIEF", "epistemic_mode": "HEARD",
        "statement": encode_proposition(ids["sword"], "possessed_by", entity_value(ids["smith"])),
        "evidence_refs": [{"kind": "EVENT", "world_ref": receipt["world_ref"],
                           "event_ref": receipt["accepted_event_refs"][0]}], "at": point(31, 1)}, key="adopt-message-belief")
    adopted_task = create_bound_task(k, actor, adopted["world_ref"], point(31, 1), [ids["sword"]],
                                    ["possessed_by"], plane="BELIEF", actor_id=ids["protagonist"])
    request.update(task_id=adopted_task["task_id"], generation=adopted_task["generation"])
    request["read_contract"]["revision"]["world_ref"] = adopted["world_ref"]
    request["knowledge_cut"]["known_at"] = point(31, 1)
    request["subject_time"] = {"kind": "POINT", "at": point(31, 1)}
    request["query"] = {"kind": "STATE", "subject_ids": [ids["sword"]], "predicates": ["possessed_by"]}
    belief = WorldQueryService(k).query(actor, request)["payload"]["state_items"][0]["answer"]
    assert belief["knowledge_status"] == "KNOWN" and belief["data"] == entity_value(ids["smith"])
    # A delayed consumer replays the original receipt. It cannot create a new
    # event by treating the completed process as another delivery.
    with pytest.raises(DomainError):
        scheduler.evaluate_due(f["principal"], adopted["world_ref"], point(31), process["scope_ids"], "late-redelivery")
    assert k.accept(f["principal"], prepared["request"]) == receipt
    assert len(revision(k, adopted["world_ref"])["snapshot"]["cognitions"]) == 2
    assert len(revision(k, adopted["world_ref"])["selection"]) == 2
    assert revision(k, receipt["world_ref"])["state_hash"] == current["state_hash"]
    evidence("C04", {"receipt": receipt, "answers_before_adoption": answers, "observation_timelines": observations,
                       "adoption_receipt": adopted["receipt"], "belief_after_adoption": belief,
                       "cognitions": current["snapshot"]["cognitions"],
                       "delayed_duplicate_world_effects": 0})


def _macro_world(k):
    definition = fixture_definition("white_bridge")
    ids, genesis = definition["ids"], deepcopy(definition["genesis"])
    health = str(uuid4())
    ids["health_points"] = health
    genesis["entities"][health] = {"entity_id": health, "entity_kind": "RESOURCE", "name": "健康点", "created_at": point(1)}
    genesis["resources"].append({"account_id": ids["traveler"], "resource_id": health, "quantity": "100", "unit": "point"})
    for predicate in ("legal_owner", "possessed_by"):
        genesis["relations"].append({"subject_id": ids["box"], "predicate": predicate, "object_value": entity_value(ids["traveler"]),
                                     "valid_time": {"start": point(1), "end": None, "bounds": "[)"}})
    created = k.bootstrap_work("TEST_FIXTURE:R1-G-100-88-100-97", genesis, frontiers={eid: point(1, 1) for eid in genesis["entities"]})
    return {**definition, **created, "ids": ids}


def test_r1_g_macro_transport_and_three_presentations_do_not_repeat_any_effect(kernel_env):
    k = kernel_env["kernel"]
    initial = _macro_world(k)
    ids = initial["ids"]
    process = process_definition(k, initial, name="macro-transport", start=50, due=53,
                                 scopes=[ids[key] for key in ("warehouse", "relief", "traveler", "reserve", "box", "recipient")])
    process.update(process_kind="TRAVEL", evaluator_kind="DETERMINISTIC", required_actor_ids=[], participant_ids=[],
        deterministic_commands=[
            {"template_id": "RESOURCE_TRANSFER", "args": {"from_account": ids["warehouse"], "to_account": ids["relief"],
             "resource_id": ids["grain"], "quantity": "12", "unit": "bag", "at": point(53)}},
            {"template_id": "RESOURCE_TRANSFER", "args": {"from_account": ids["traveler"], "to_account": ids["reserve"],
             "resource_id": ids["health_points"], "quantity": "3", "unit": "point", "at": point(53)}},
            {"template_id": "TRANSFER_ITEM", "args": {"item_id": ids["box"], "from_id": ids["traveler"],
             "to_id": ids["recipient"], "transfer_kind": "LOAN", "at": point(53)}},
        ])
    f, _ = register(k, initial, process)
    scheduler = WorldSchedulerService(k)
    chosen = scheduler.evaluate_due(f["principal"], f["world_ref"], point(53), process["scope_ids"], "macro-due")
    accepted = run_command(k, f["principal"], f["world_ref"], "JOINT_INTERACTION", _chosen_command(k, f, chosen), key="macro")
    f = arrange(k, {**f, "world_ref": accepted["world_ref"]})
    before = revision(k, f["world_ref"])
    assert resource_at(before["snapshot"], ids["warehouse"], ids["grain"], "bag", point(52))["quantity"] == "100"
    assert resource_at(before["snapshot"], ids["warehouse"], ids["grain"], "bag", point(53))["quantity"] == "88"
    assert resource_at(before["snapshot"], ids["traveler"], ids["health_points"], "point", point(52))["quantity"] == "100"
    assert resource_at(before["snapshot"], ids["traveler"], ids["health_points"], "point", point(53))["quantity"] == "97"
    assert state_at(before["snapshot"], ids["box"], "active_holder", point(53))["data"] == entity_value(ids["recipient"])
    event_ref = accepted["receipt"]["accepted_event_refs"][0]
    narrative = NarrativeService(k)
    acceptances = []
    for ordinal, prose in enumerate((
        "第五十三日，粮账剩下八十八袋，旅人健康点九十七，箱子交到了接收人手中。",
        "后来回想那段运输，他记得十二袋的损耗、三点健康的消耗，以及交出的箱子。",
        "接收人再述同一次抵达：粮账八十八袋，旅人九十七点健康，箱子已由自己保管。",
    )):
        scene = {"schema_version": "2.2", "scene_contract": {"schema_version": "2.0", "scene_id": str(uuid4()),
            "input_world_ref": f["world_ref"], "story_interval": {"start": point(50), "end": point(54), "bounds": "[)"},
            "event_refs": [event_ref], "entry_commitments": [], "exit_commitments": [], "pov_actor_id": ids["protagonist"],
            "narrator_mode": "omniscient", "narrative_revision_id": f["narrative_id"], "arrangement_version": "macro-g-1",
            "reader_cut_ordinal": ordinal, "allowed_invention": ["仅重述已接受结果"], "style_refs": [],
            "dependency_fingerprint": fingerprint({"event": event_ref, "world": f["world_ref"]})},
            "plan_ref": f["plan_ref"], "plan_node_ids": [f["plan"]["nodes"][0]["node_id"]], "batch_ref": None,
            "prior_scene_ref": None, "required_event_refs": [event_ref], "required_predecessor_scene_ids": [],
            "entry_checks": [], "exit_checks": [], "allowed_disclosures": [], "literary_purpose": "TEST_FIXTURE：宏微效果去重"}
        scene_ref = narrative.save_scene(f["principal"], scene, f"macro-scene:{ordinal}")
        writer = runtime_grant(k, f, "writer")
        task = task_for(k, f, writer, [scene_ref, f["plan_ref"]], story_time=point(54))
        PlanningService(k).bind_task_for_controller(f["principal"], task["task_id"], task["generation"], f["plan_ref"],
                                                   scene["plan_node_ids"], f"macro-bind:{ordinal}")
        draft = narrative.store_draft(writer, task["task_id"], task["generation"], scene_ref, prose, f"macro-draft:{ordinal}")
        reader = runtime_grant(k, f, "reviewer")
        read_task = task_for(k, f, reader, [draft], "extract", story_time=point(54))
        narrative.bind_reader_task(f["principal"], read_task["task_id"], read_task["generation"], [draft], f"macro-reader:{ordinal}")
        read = narrative.read_text(reader, read_task["task_id"], read_task["generation"], draft)
        extraction = narrative.submit_extraction(reader, read_task["task_id"], read_task["generation"], draft,
            read["delivery_ref"], [assertion(prose, classification="EXISTING_EVENT_PRESENTATION", event_ref=event_ref)], f"macro-extract:{ordinal}")
        reconciled = narrative.reconcile(f["principal"], draft, extraction, f["world_ref"], f"macro-reconcile:{ordinal}")
        assert reconciled["status"] == "CLEAR"
        acceptance = narrative.accept_narrative(f["principal"], draft, reconciled["reconciliation_ref"], f["world_ref"], f"macro-narrative:{ordinal}")
        acceptances.append(acceptance)
        after = revision(k, f["world_ref"])
        assert after["snapshot"] == before["snapshot"]
        assert after["selection"] == before["selection"]
        assert after["frontiers"] == before["frontiers"]
        with k.db.uow(write=False) as repo:
            assert k.head(repo, f["world_ref"]["work_id"]) == f["world_ref"]
            assert k.artifact(repo, acceptance, f["world_ref"]["work_id"])["payload"]["world_effects_applied"] == 0
    evidence("G01", {"world_receipt": accepted["receipt"], "state_hash": before["state_hash"],
        "selected_occurrences": before["selection"], "narrative_acceptances": acceptances,
        "grain_before": "100", "grain_after": "88", "health_before": "100", "health_after": "97",
        "health_representation": "DECLARED_RESOURCE_WITH_LOSS_SINK", "new_world_effects_from_presentations": 0})
