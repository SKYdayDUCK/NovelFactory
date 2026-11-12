"""Read branch metrics through the same authenticated stdio port available to operators."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from test_harness_context import EnvelopeFixture
from test_harness_dynamic_mcp import controller_port
from test_p5_mcp import config, error, exercise

from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.runtime_tasks import RuntimeTaskService
from novel_factory.fixtures import load_fixture, point
from novel_factory.infrastructure.creative_config import CreativeHarnessConfig

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def test_scoped_metrics_are_measured_through_stdio_and_hidden_from_roles(
    kernel_env, tmp_path, monkeypatch
):
    kernel = kernel_env["kernel"]
    world = load_fixture(kernel)
    owner, wr = world["principal"], world["world_ref"]
    flags = CreativeHarnessConfig(enabled=True)
    monkeypatch.setenv("NF_CREATIVE_HARNESS_CONFIG", json.dumps(flags.as_dict()))
    port = controller_port(tmp_path / "operator", kernel_env, world, flags)
    other = kernel.fork(owner, wr, "CANDIDATE", "other-branch-metrics")
    gateway = AgentGateway(kernel, {"fixture": EnvelopeFixture()}, creative_harness=flags)
    text = "她用蓝色的绣线在内衬缝了一封信。"
    job = RuntimeTaskService(kernel, gateway).prepare(
        owner,
        wr,
        role_id="writer",
        task_kind="scene_draft",
        subject_ids=[world["ids"]["sword"]],
        predicates=["owner"],
        assignment=text,
        adapter_id="fixture",
        story_time=point(1),
    )
    request = {
        "task_id": job.task["task_id"],
        "generation": job.task["generation"],
        "schema_version": "2.1",
        "worker_id": job.worker_id,
        "input_ref": job.agent_input["input_ref"],
        "call_key": "metrics-offline",
        "token_reservation": 100000,
        "reserve_amount": "0.1",
        "budget_id": kernel.tasks.create_budget(wr["work_id"], "1"),
    }
    assert gateway.execute(job.principal, request)["validation_status"] == "VALIDATED_CANDIDATE"
    end = datetime.now(UTC)
    args = {
        "world_ref": wr,
        "since": (end - timedelta(hours=1)).isoformat(),
        "until": end.isoformat(),
        "limit": 256,
    }

    async def operator(session):
        assert "nf_harness_metrics" in {t.name for t in (await session.list_tools()).tools}
        await error(session, "nf_harness_metrics", {**args, "world_ref": other}, "VIEW_DENIED")
        result = await session.call_tool("nf_harness_metrics", args)
        assert not result.isError, result.content
        report = result.structuredContent
        assert report["world_ref"] == wr
        assert {
            "context",
            "planning",
            "review",
            "dynamic",
            "instructions",
            "scenes",
            "latency",
            "recovery",
        } <= report.keys()
        wire = json.dumps(report, ensure_ascii=False)
        assert text not in wire and "Payload JSON Schema" not in wire
        assert "dependency_fingerprint" not in wire and job.private_token not in wire

    asyncio.run(exercise(port, operator))
    active = RuntimeTaskService(kernel, gateway).prepare(
        owner,
        wr,
        role_id="writer",
        task_kind="scene_draft",
        subject_ids=[world["ids"]["sword"]],
        predicates=["owner"],
        assignment="检查端口。",
        adapter_id="fixture",
        story_time=point(1),
    )
    role_port = config(
        tmp_path / "role",
        kernel_env,
        active.private_token,
        {
            "task_id": active.task["task_id"],
            "generation": active.task["generation"],
            "worker_id": active.worker_id,
        },
    )
    role_port.env = dict(port.env)

    async def role(session):
        names = {t.name for t in (await session.list_tools()).tools}
        assert "nf_task_get" in names and "nf_harness_metrics" not in names
        await error(session, "nf_harness_metrics", args, "VIEW_DENIED")

    asyncio.run(exercise(role_port, role))
