"""Fixed-cut fact readers receive identity labels and explicit unknown state answers."""
import json

import pytest
from test_agent_gateway import FixtureAdapter
from test_harness_context import configured

from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.runtime_tasks import RuntimeTaskService
from novel_factory.fixtures import load_fixture, point, run_command

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def reading(kernel, world, *, at, role="evidence_maintainer", kind="assertion_extract", project=True):
    adapter = FixtureAdapter()
    gateway = AgentGateway(kernel, {"fixture": adapter})
    actor = world["ids"]["protagonist"] if role == "character_actor" else None
    task = RuntimeTaskService(kernel, gateway).prepare(world["principal"], world["world_ref"],
        role_id=role, task_kind=kind, story_time=at, subject_ids=[world["ids"]["sword"]],
        predicates=["lifecycle"], assignment="Read only the supplied fixed scope.", adapter_id="fixture",
        actor_id=actor, token_budget=100000)
    request = {"schema_version": "2.1", "task_id": task.task["task_id"], "generation": task.task["generation"],
               "worker_id": task.worker_id, "input_ref": task.agent_input["input_ref"],
               "budget_id": kernel.tasks.create_budget(world["world_ref"]["work_id"], "1"),
               "call_key": "preview-only", "reserve_amount": "0.1", "token_reservation": 100000}
    with kernel.db.uow(write=False) as repo:
        if not project:
            context = repo.one("contexts", {"context_bundle_id": task.context_bundle_id})
            compiled = kernel.artifact(repo, context["artifact_ref"], world["world_ref"]["work_id"],
                                       kind="COMPILED_CONTEXT")["payload"]
            return compiled
        binding = gateway._binding(repo, task.principal, request)
    messages = gateway._messages(task.principal, binding)
    assert not adapter.calls
    return json.loads(messages[1]["content"]), binding


def scopes(user):
    return [row["value"] for row in user.get("structured_evidence", [])
            if isinstance(row.get("value"), dict) and row["value"].get("kind") == "FACT_READING_SCOPE"]


def test_registered_identity_and_unknown_state_reach_creative_prompt_without_foreign_names(kernel_env, monkeypatch):
    configured(monkeypatch)
    kernel = kernel_env["kernel"]
    world = load_fixture(kernel)
    user, binding = reading(kernel, world, at=point(2))
    scope, = scopes(user)
    assert scope["entities"] == [{"entity_id": world["ids"]["sword"], "entity_kind": "ITEM", "name": "sword"}]
    assert scope["identity_semantics"] == "REGISTERED_LABELS_NOT_STATE_FACTS"
    assert scope["state_items"][0]["answer"]["knowledge_status"] == "UNKNOWN"
    assert scope["state_items"][0]["answer"]["evaluation_status"] == "PENDING"
    assert not user["creative_materials"].get("facts")
    assert world["ids"]["smith"] not in json.dumps(user)
    assert "smith" not in json.dumps(user)
    with kernel.db.uow(write=False) as repo:
        snapshot = kernel.artifact(repo, binding["creative_snapshot"][0], world["world_ref"]["work_id"])["payload"]
    assert any(s["evidence"].get("artifact_ref") == binding["compiled_ref"] for s in snapshot["packet"]["sources"])


def test_reading_scope_uses_old_world_ref_after_head_advances(kernel_env, monkeypatch):
    configured(monkeypatch)
    kernel = kernel_env["kernel"]
    world = load_fixture(kernel)
    updated = run_command(kernel, world["principal"], world["world_ref"], "DESTROY_ITEM",
                          {"item_id": world["ids"]["sword"], "at": point(10)})
    assert updated["world_ref"] != world["world_ref"]
    # A relevant HEAD change correctly prevents model dispatch until task
    # dependencies are revalidated. The compiler itself still reads the exact
    # explicitly requested historical revision, never the new HEAD snapshot.
    compiled = reading(kernel, world, at=point(1), project=False)
    scope, = [json.loads(section["content"]) for section in compiled["sections"]
              if section["section_kind"] == "EVIDENCE"]
    assert scope["world_ref"] == world["world_ref"]
    assert scope["state_items"][0]["answer"]["world_ref"] == world["world_ref"]
    assert scope["state_items"][0]["answer"]["data"] == {"kind": "STRING", "value": "ACTIVE"}


@pytest.mark.parametrize("role,kind", [("character_actor", "character_action"), ("writer", "scene_draft")])
def test_entity_registry_scope_does_not_enter_actor_or_other_role_context(kernel_env, monkeypatch, role, kind):
    configured(monkeypatch)
    world = load_fixture(kernel_env["kernel"])
    user, binding = reading(kernel_env["kernel"], world, at=point(2), role=role, kind=kind)
    assert scopes(user) == []
    assert all("FACT_READING_SCOPE" not in s["content"] for s in binding["compiled_context"]["sections"])
    if role == "character_actor":
        assert "REGISTERED_LABELS_NOT_STATE_FACTS" not in json.dumps(user)
        assert "sword" not in json.dumps(user)
