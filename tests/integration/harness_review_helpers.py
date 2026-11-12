"""Provision native writer and reviewer assignments for real S3 integration tests."""
import json

import pytest

from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.narrative import NarrativeService
from novel_factory.application.runtime_tasks import RuntimeTaskService
from novel_factory.fixtures import point
from novel_factory.infrastructure.creative_config import CreativeHarnessConfig
from editorial_helpers import binding_for
from test_agent_gateway import FixtureAdapter
from test_release import accept_draft, setup_batch, write_scene


@pytest.fixture
def review_assignment(kernel_env, monkeypatch):
    kernel = kernel_env["kernel"]
    monkeypatch.setenv("NF_CREATIVE_HARNESS_CONFIG", json.dumps(CreativeHarnessConfig(enabled=True).as_dict()))
    world = setup_batch(kernel, count=1)
    first = write_scene(kernel, world, 0, "她把铜印按在封泥上，等最后一条船靠岸。")
    accepted = accept_draft(kernel, world, first)
    editorial = binding_for(world, [accepted])
    wr, admin = world["world_ref"], world["principal"]
    adapter = FixtureAdapter()
    reviewer = RuntimeTaskService(kernel, AgentGateway(kernel, {"fixture": adapter})).prepare(
        admin, wr, role_id="reviewer", task_kind="literary", story_time=point(1, 1),
        subject_ids=[world["ids"]["sword"]], predicates=["owner"], assignment="核对这段文字的叙事功能。",
        adapter_id="fixture", input_artifacts=editorial["ordered_draft_refs"])
    scope = {name: reviewer.task[name] for name in ("task_id", "generation")}
    NarrativeService(kernel).bind_reader_task(admin, scope["task_id"], scope["generation"],
                                              [first["draft_ref"]], "reviewer-reading-assignment")
    return world, first, editorial, reviewer, scope
