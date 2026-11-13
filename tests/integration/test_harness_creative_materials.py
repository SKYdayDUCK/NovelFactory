"""Exercise literary material projection through the production NF readers and model gateway."""
from copy import deepcopy
import json
from uuid import uuid4

import pytest

from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.editorial import ReviewService
from novel_factory.application.narrative import NarrativeService
from novel_factory.application.planning import PlanningService
from novel_factory.application.release import ReleaseService
from novel_factory.application.runtime_tasks import RuntimeTaskService
from novel_factory.domain.reconciliation import span_for
from novel_factory.fixtures import point
from test_harness_context import EnvelopeFixture, configured
from test_release import setup_batch, write_scene


pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def test_writer_receives_assigned_style_voice_and_adjacent_prose(kernel_env, monkeypatch):
    configured(monkeypatch)
    kernel = kernel_env["kernel"]
    work = setup_batch(kernel)
    controller, world = work["principal"], work["world_ref"]
    prior = "她把手里的桨往后一拖。\n船贴上了石阶。"
    previous = write_scene(kernel, work, 0, prior)
    narrative, release = NarrativeService(kernel), ReleaseService(kernel)
    anchor = kernel.put_artifact(controller, world["work_id"], prior.encode(), "TEXT")
    guide = {
        "schema_version": "2.1", "style_id": str(uuid4()), "style_revision_id": str(uuid4()),
        "world_ref": world, "principles": ["通过动作写出停顿。"], "method_refs": [],
        "voices": [{"actor_id": work["ids"]["protagonist"], "name": "摆渡人",
                    "register": "句子短，遇到追问会停一下。", "allowed_variations": ["遇险时喊话。"],
                    "anchors": [{"kind": "ARTIFACT", "artifact_ref": anchor, "span": span_for(prior)}]}],
    }
    style_ref = ReviewService(kernel).save_style(controller, guide, "chapter-style")
    batch = release.create_writing_batch(controller, {**work["batch_request"], "style_refs": [style_ref]},
                                         "chapter-batch")
    scene = deepcopy(work["scenes"][1])
    scene["scene_contract"]["style_refs"] = [style_ref]
    scene.update(batch_ref=batch, prior_scene_ref=work["scene_refs"][1])
    scene_ref = narrative.save_scene(controller, scene, "chapter-scene")

    adapter = EnvelopeFixture()
    gateway = AgentGateway(kernel, {"fixture": adapter}, material_reader=narrative.read_agent_material)
    prepared = RuntimeTaskService(kernel, gateway).prepare(
        controller, world, role_id="writer", task_kind="scene_draft", story_time=point(1, 1),
        subject_ids=[work["ids"]["sword"]], predicates=["owner"], assignment="续写靠岸后的动作。",
        adapter_id="fixture", input_artifacts=[work["plan_ref"], batch],
        material_refs=[scene_ref, previous["draft_ref"]],
    )
    task, gen = prepared.task["task_id"], prepared.task["generation"]
    PlanningService(kernel).bind_task_for_controller(controller, task, gen, work["plan_ref"],
                                                      scene["plan_node_ids"], "chapter-plan")
    release.bind_writer_task(prepared.principal, task, gen, scene_ref, [], "chapter-writer")
    narrative.bind_reader_task(controller, task, gen, [previous["draft_ref"]], "chapter-reader")
    request = {
        "schema_version": "2.1", "task_id": task, "generation": gen, "worker_id": prepared.worker_id,
        "input_ref": prepared.agent_input["input_ref"], "budget_id": kernel.tasks.create_budget(world["work_id"], "1"),
        "call_key": "chapter-write", "reserve_amount": "0.2", "token_reservation": 100000,
    }
    receipt = gateway.execute(prepared.principal, request)
    assert receipt["validation_status"] == "VALIDATED_CANDIDATE"
    assert len(adapter.calls) == 1
    offered = json.loads(adapter.calls[0].messages[1]["content"])
    material = offered["creative_materials"]
    assert material["prose"] == [prior]
    style = material["scene"][0]["styles"][0]
    assert style["principles"] == guide["principles"]
    assert style["voices"][0]["register"] == guide["voices"][0]["register"]
    for control in ("dependency_fingerprint", "delivery_ref", "source_ref", "scene_contract", "world_ref", "grant_id"):
        assert control not in json.dumps(material)
    with kernel.db.uow(write=False) as repo:
        snapshot, = repo.many("artifacts", {"work_id": world["work_id"], "kind": "CREATIVE_CONTEXT_SNAPSHOT"})
        sources = [source["evidence"] for source in snapshot["payload"]["packet"]["sources"]]
        assert {"kind": "ARTIFACT", "artifact_ref": style_ref} in sources
        assert any(e.get("span") == span_for(prior) for e in sources)


def test_fact_extractor_receives_only_its_bound_draft_obligations(kernel_env, monkeypatch):
    configured(monkeypatch)
    kernel = kernel_env["kernel"]
    work = setup_batch(kernel, count=1)
    world, owner = work["world_ref"], work["principal"]
    prior = write_scene(kernel, work, 0, "门帘被风撩起。")
    narrative = NarrativeService(kernel)
    adapter = EnvelopeFixture()
    gateway = AgentGateway(kernel, {"fixture": adapter}, material_reader=narrative.read_agent_material)
    prepared = RuntimeTaskService(kernel, gateway).prepare(
        owner, world, role_id="evidence_maintainer", task_kind="assertion_extract",
        story_time=point(1, 1), subject_ids=[work["ids"]["sword"]], predicates=["owner"],
        assignment="核对正文。", adapter_id="fixture", input_artifacts=[work["scene_refs"][0]],
        material_refs=[prior["draft_ref"]],
    )
    task, gen = prepared.task["task_id"], prepared.task["generation"]
    narrative.bind_reader_task(owner, task, gen, [prior["draft_ref"]], "fact-reader")
    receipt = gateway.execute(prepared.principal, {
        "schema_version": "2.1", "task_id": task, "generation": gen, "worker_id": prepared.worker_id,
        "input_ref": prepared.agent_input["input_ref"], "budget_id": kernel.tasks.create_budget(world["work_id"], "1"),
        "call_key": "fact-read", "reserve_amount": "0.2", "token_reservation": 100000,
    })
    assert receipt["validation_status"] == "VALIDATED_CANDIDATE"
    packet = json.loads(adapter.calls[0].messages[1]["content"])
    assert packet["creative_materials"]["prose"] == [prior["text"]]
    assert packet["structured_evidence"] == [
        {"ref": prior["draft_ref"], "value": {"draft_ref": prior["draft_ref"], "required_event_refs": [],
                                              "declared_important_deltas": []}}
    ]
