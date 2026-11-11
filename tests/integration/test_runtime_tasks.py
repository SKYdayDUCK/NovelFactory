"""Runtime provisioning does not confer controller powers or implicit reads."""
import pytest

from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.runtime_tasks import RuntimeTaskService
from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import load_fixture, point
from novel_factory.infrastructure.model_gateway import OpenAICompatibleAdapter

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def test_provisioned_writer_has_fixed_context_but_no_controller_power(kernel_env):
    kernel = kernel_env["kernel"]
    world = load_fixture(kernel)
    adapter = OpenAICompatibleAdapter(endpoint="https://example.test/v1/chat/completions",
                                      model="explicit-test-route", api_key=None)
    gateway = AgentGateway(kernel, {"external": adapter})
    private = kernel.put_artifact(world["principal"], world["world_ref"]["work_id"],
                                  {"private": "listing is not permission"}, "TEXT")
    prepared = RuntimeTaskService(kernel, gateway).prepare(world["principal"], world["world_ref"],
        role_id="writer", task_kind="scene_draft", story_time=point(1),
        subject_ids=[world["ids"]["sword"]], predicates=["owner"], assignment="Only use the authorized cut.",
        adapter_id="external", input_artifacts=[private])
    assert kernel.authenticate(prepared.private_token) == prepared.principal
    assert prepared.private_token not in repr(prepared)
    with kernel.db.uow(write=False) as repo:
        grant = repo.one("grants", {"grant_id": prepared.principal.grant_id})
        assert not {"ADMIN", "APPROVE", "ACCEPT", "PROMOTE", "PLAN_READ"}.intersection(grant["capabilities"])
        context = repo.one("contexts", {"context_bundle_id": prepared.context_bundle_id})
        assert context["payload"]["world_ref"] == world["world_ref"]
    with pytest.raises(DomainError) as error:
        kernel.get_artifact(prepared.principal, world["world_ref"]["work_id"], private,
            task_id=prepared.task["task_id"], generation=prepared.task["generation"],
            context_bundle_id=prepared.context_bundle_id, world_ref=world["world_ref"],
            story_time=point(1), knowledge_cut={"plane": "WORLD"})
    assert error.value.code == "VIEW_DENIED"
    budget = kernel.tasks.create_budget(world["world_ref"]["work_id"], "1")
    result = gateway.execute(prepared.principal, {
        "schema_version": "2.1", "task_id": prepared.task["task_id"], "generation": prepared.task["generation"],
        "worker_id": prepared.worker_id, "input_ref": prepared.agent_input["input_ref"], "budget_id": budget,
        "call_key": "live-attempt", "reserve_amount": "0.5", "token_reservation": 100_000})
    assert result["reason"] == "MODEL_UNAVAILABLE" and result["cost_status"] == "NOT_SENT"


def test_controller_cannot_accidentally_provision_character_with_world_view(kernel_env):
    kernel = kernel_env["kernel"]
    world = load_fixture(kernel)
    with pytest.raises(DomainError) as error:
        RuntimeTaskService(kernel, AgentGateway(kernel, {})).prepare(world["principal"], world["world_ref"],
            role_id="character_actor", task_kind="character_action", actor_id=world["ids"]["protagonist"],
            story_time=point(1), knowledge_cut={"plane": "WORLD"}, subject_ids=[], predicates=[],
            assignment="This must fail before runtime input creation.", adapter_id="none")
    assert error.value.code == "VIEW_DENIED"
    with kernel.db.uow(write=False) as repo:
        assert repo.many("tasks", {"work_id": world["world_ref"]["work_id"]}) == []
