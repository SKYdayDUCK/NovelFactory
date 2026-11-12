"""Read the actual S1 adapter input with a real world, two actors and two knowledge cuts."""
import json
from uuid import uuid4

import pytest

from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.narrative import NarrativeService
from novel_factory.domain.errors import DomainError
from novel_factory.domain.propositions import encode_proposition
from novel_factory.fixtures import entity_value, fixture_definition, load_fixture, point
from novel_factory.infrastructure.model_gateway import ModelCallFailure
from test_agent_gateway import FixtureAdapter, setup_gateway
from test_harness_context import EnvelopeFixture
from test_release import setup_batch, write_scene

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


@pytest.fixture
def harness_enabled(monkeypatch):
    monkeypatch.setenv("NF_CREATIVE_HARNESS_CONFIG", json.dumps({
        "kind": "CONFIGURATION", "schema_version": "1.0",
        "creative_harness": {
            "enabled": True, "dynamic_agents": {"enabled": False},
            "adaptive_instructions": {"enabled": False, "auto_promote": False},
        },
    }))


def test_actors_receive_only_their_own_belief_in_production_dispatch(kernel_env, harness_enabled):
    kernel = kernel_env["kernel"]
    world = fixture_definition("r1_sword")
    ids = world["ids"]
    # The same sword has one objective owner (smith). Each actor believes a
    # different person owns it. Both records are valid at the same world cut.
    holders = {"protagonist": "master", "friend": "rival"}
    for actor, holder in holders.items():
        value = entity_value(ids[holder])
        world["genesis"]["cognitions"].append({
            "record_id": str(uuid4()), "actor_id": ids[actor], "record_kind": "BELIEF",
            "subject_id": ids["sword"], "predicate": "legal_owner", "object_value": value,
            "statement": encode_proposition(ids["sword"], "legal_owner", value),
            "acquired_at": point(1), "evidence_refs": [],
        })
    world.update(kernel.bootstrap_work("TEST_ONLY:two_beliefs", world["genesis"],
                                       frontiers=world["frontiers"]))

    def stop_after_capture(_request):
        # Capture the real transport boundary without paying for a model or
        # constructing an artificial successful response to the actor task.
        raise ModelCallFailure("MODEL_TRANSPORT_UNKNOWN")

    for actor, holder in holders.items():
        adapter = FixtureAdapter(hook=stop_after_capture)
        item = setup_gateway(kernel_env, adapter, role="character_actor", world=world, actor_key=actor)
        result = item["gateway"].execute(item["principal"], item["request"])
        assert result["status"] == "BLOCKED" and result["reason"] == "MODEL_TRANSPORT_UNKNOWN"
        assert len(adapter.calls) == 1
        user = json.loads(adapter.calls[0].messages[1]["content"])
        beliefs = json.dumps(user["creative_materials"]["beliefs"], ensure_ascii=False)
        assert ids[holder] in beliefs
        assert "facts" not in user["creative_materials"]
        other_holder = next(other for other in holders.values() if other != holder)
        all_messages = json.dumps(adapter.calls[0].messages, ensure_ascii=False)
        assert ids[other_holder] not in all_messages
        assert ids["smith"] not in all_messages


@pytest.mark.parametrize("scope", ["work", "branch"])
def test_foreign_draft_never_reaches_model(kernel_env, harness_enabled, scope):
    kernel = kernel_env["kernel"]
    source = setup_batch(kernel, count=1)
    draft = write_scene(kernel, source, 0, "渡船靠岸时，船底轻轻碰了一下石阶。")
    adapter = FixtureAdapter()
    if scope == "work":
        other = load_fixture(kernel)
        # The controller cannot even freeze a foreign-work draft into an input.
        with pytest.raises(DomainError) as err:
            setup_gateway(kernel_env, adapter, world=other, materials=[draft["draft_ref"]])
        assert err.value.code == "VIEW_DENIED"
    else:
        branch = kernel.fork(source["principal"], source["world_ref"], "CANDIDATE", str(uuid4()))
        other = {**source, "world_ref": branch}
        item = setup_gateway(kernel_env, adapter, world=other, story_time=point(1, 1),
                             materials=[draft["draft_ref"]], task_inputs=[draft["draft_ref"]])
        narrative = NarrativeService(kernel)
        request = item["request"]
        with pytest.raises(DomainError) as cannot_assign:
            narrative.bind_reader_task(other["principal"], request["task_id"], request["generation"],
                                       [draft["draft_ref"]], "foreign-branch-reader")
        assert cannot_assign.value.code == "VIEW_DENIED"
        gateway = AgentGateway(kernel, {"fixture": adapter}, material_reader=narrative.read_agent_material)
        with pytest.raises(DomainError) as err:
            gateway.execute(item["principal"], request)
        assert err.value.code == "VIEW_DENIED"
        with kernel.db.uow(write=False) as repo:
            assert not repo.many("call_attempts", {"task_id": item["task"]["task_id"]})
    assert adapter.calls == []


def test_superseded_manuscript_blocks_delivery_and_replay(kernel_env, harness_enabled):
    kernel = kernel_env["kernel"]
    narrative = NarrativeService(kernel)
    source = setup_batch(kernel, count=1)
    first = write_scene(kernel, source, 0, "人到了门外，却没有敲门。")
    ref = first["draft_ref"]
    adapter = EnvelopeFixture()
    item = setup_gateway(kernel_env, adapter, world=source, story_time=point(1, 1),
                         materials=[ref], task_inputs=[ref])
    request = item["request"]
    narrative.bind_reader_task(source["principal"], request["task_id"], request["generation"],
                               [ref], "read-original-draft")
    gateway = AgentGateway(kernel, {"fixture": adapter}, material_reader=narrative.read_agent_material)

    def supersede_during_generation(_request):
        write_scene(kernel, source, 0, "他屈起指节，在门上敲了两下。", prior_draft_ref=ref)

    adapter.hook = supersede_during_generation
    for _ in range(2):
        with pytest.raises(DomainError) as error:
            gateway.execute(item["principal"], request)
        assert error.value.code == "TASK_REBIND_REQUIRED"
    assert len(adapter.calls) == 1
    user = json.loads(adapter.calls[0].messages[1]["content"])
    assert first["text"] in json.dumps(user["creative_materials"], ensure_ascii=False)
    with kernel.db.uow(write=False) as repo:
        assert repo.many("task_results", {"task_id": request["task_id"]}) == []
        assert len(repo.many("call_attempts", {"task_id": request["task_id"]})) == 1
