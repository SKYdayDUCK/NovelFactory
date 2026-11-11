"""One production handoff from a fixed Agent task to real narrative services.

Adapter output is an explicit fixture; this proves integration, not a real
model experiment. No service authorization or release gate is stubbed.
"""
from decimal import Decimal
import json

import pytest
from test_release import accept_draft, setup_batch

from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.narrative import NarrativeService
from novel_factory.application.planning import PlanningService
from novel_factory.application.release import ReleaseService
from novel_factory.application.runtime_tasks import RuntimeTaskService
from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import point
from novel_factory.infrastructure.model_gateway import ModelResponse

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


class ProseFixture:
    configuration = {"adapter_kind": "DETERMINISTIC_TEST", "model": "gateway-narrative-fixture-v1",
        "max_output_tokens": 512, "isolation": "STATELESS_TEXT_ONLY", "execution_kind": "TEST_FIXTURE"}

    def __init__(self):
        self.calls = []

    def check_available(self):
        pass

    def generate(self, request):
        self.calls.append(request)
        packet = json.loads(request.messages[1]["content"])
        context = packet["context"]
        card = json.loads(packet["authorized_materials"][0]["content"])
        assert "scene_contract" in card and "nodes" not in card
        assert card["scene_contract"]["input_world_ref"] == context["world_ref"]
        identity = {k: context[k] for k in ("task_id", "generation", "context_bundle_id", "world_ref", "role_id", "actor_id")}
        envelope = {"schema_version": "2.1", **identity, "status": "SUCCEEDED", "unresolved_questions": [],
            "payload": {"text": "屋外的风停了一瞬。她没有急着开口。", "event_refs": [],
                        "important_deltas": [], "evidence_refs": []}}
        return ModelResponse(json.dumps(envelope, ensure_ascii=False), provider_receipt="EXPLICIT_TEST_FIXTURE",
            input_tokens=0, output_tokens=0, actual_amount=Decimal("0"), currency="USD",
            request_sha256=request.request_sha256)


def test_runtime_material_scope_gateway_result_and_narrative_acceptance(kernel_env):
    kernel = kernel_env["kernel"]
    fixture = setup_batch(kernel)
    narrative, adapter = NarrativeService(kernel), ProseFixture()
    gateway = AgentGateway(kernel, {"fixture": adapter}, material_reader=narrative.read_agent_material)
    scene = fixture["scene_refs"][0]
    prepared = RuntimeTaskService(kernel, gateway).prepare(fixture["principal"], fixture["world_ref"],
        role_id="writer", task_kind="scene_draft", story_time=point(1, 1),
        subject_ids=[fixture["ids"]["sword"]], predicates=["owner"],
        assignment="Use the controller-bound writing card. This is a labelled engineering fixture.",
        adapter_id="fixture", input_artifacts=[fixture["plan_ref"], fixture["batch_ref"]], material_refs=[scene])
    tid, generation = prepared.task["task_id"], prepared.task["generation"]
    budget = kernel.tasks.create_budget(fixture["world_ref"]["work_id"], "1")
    request = {"schema_version": "2.1", "task_id": tid, "generation": generation,
        "worker_id": prepared.worker_id, "input_ref": prepared.agent_input["input_ref"], "budget_id": budget,
        "call_key": "writer-once", "reserve_amount": "0.5", "token_reservation": 100000}
    # A task input reference alone must not be enough to send scene text.
    with pytest.raises(DomainError):
        gateway.execute(prepared.principal, request)
    assert adapter.calls == []
    PlanningService(kernel).bind_task_for_controller(fixture["principal"], tid, generation, fixture["plan_ref"],
        fixture["scenes"][0]["plan_node_ids"], "controller-plan")
    ReleaseService(kernel).bind_writer_task(prepared.principal, tid, generation, scene, [], "writer-binding")
    result = gateway.execute(prepared.principal, request)
    assert result["validation_status"] == "VALIDATED_CANDIDATE" and result["execution_kind"] == "TEST_FIXTURE"
    assert kernel.get_task(prepared.principal, tid)["state"] == "RESULT_STORED"
    text = "屋外的风停了一瞬。她没有急着开口。"
    draft_ref = narrative.store_draft(prepared.principal, tid, generation, scene, text, "persist-candidate",
                                     agent_output_ref=result["output_ref"])
    with pytest.raises(DomainError) as error:
        narrative.store_draft(prepared.principal, tid, generation, scene, "篡改候选正文。", "forged-candidate",
                              agent_output_ref=result["output_ref"])
    assert error.value.code == "INVALID_EVIDENCE"
    accepted = accept_draft(kernel, fixture, {"draft_ref": draft_ref, "text": text,
                                            "writer": prepared.principal, "task": prepared.task})
    assert accepted["acceptance_ref"]
    assert len(adapter.calls) == 1
    with kernel.db.uow(write=False) as repo:
        assert kernel.head(repo, fixture["world_ref"]["work_id"]) == fixture["world_ref"]
        assert repo.many("events", {"work_id": fixture["world_ref"]["work_id"]}) == []
