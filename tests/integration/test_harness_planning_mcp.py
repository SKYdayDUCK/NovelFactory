"""MCP discovery and end-to-end S2 operations on separately provisioned stdio ports."""
import asyncio
import os
from pathlib import Path

import pytest

from novel_factory.application.mcp_runtime import CreativeControllerBinding, McpRuntime
from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import load_fixture
from test_harness_planning_simulation import scene, scope
from test_p5_mcp import config, error, exercise

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def test_separate_planner_and_controller_stdio_ports(kernel_env, scene, tmp_path):
    planner = scene["planner"]
    binding = {**scope(planner), "worker_id": planner.worker_id}
    planner_port = config(tmp_path / "planner", kernel_env, planner.private_token, binding)
    control_scope = {k: scene["wr"][k] for k in ("work_id", "branch_id")}
    controller_port = config(tmp_path / "controller", kernel_env, scene["world"]["token"],
                             {**control_scope, "mode": "creative_controller"})
    source_path = os.pathsep.join(filter(None, [
        str(Path(__file__).resolve().parents[2] / "src"), os.environ.get("PYTHONPATH"),
    ]))
    controller_port.env = {"NF_CREATIVE_HARNESS_CONFIG": os.environ["NF_CREATIVE_HARNESS_CONFIG"],
                           "PYTHONPATH": source_path}
    planner_port.env = {"PYTHONPATH": source_path}

    async def propose(session):
        names = {t.name for t in (await session.list_tools()).tools}
        assert "nf_harness_plan_propose" in names
        assert "nf_harness_simulate" not in names and "nf_harness_plan_decide" not in names
        await error(session, "nf_harness_simulate", {}, "VIEW_DENIED")
        args = {"plan_ref": scene["anchor"], "node_ids": scene["nodes"], "trigger": "MOTIVE_CONFLICT",
                "quota": scene["quota"], "horizon_scenes": 3, "idempotency_key": "stdio-propose"}
        await error(session, "nf_harness_plan_propose", {**args, "generation": 2}, "INVALID_ARGUMENT")
        result = await session.call_tool("nf_harness_plan_propose", args)
        assert not result.isError, result.content
        return result.structuredContent["candidate_ref"]

    ref = asyncio.run(exercise(planner_port, propose))

    async def simulate_and_decline(session):
        names = {t.name for t in (await session.list_tools()).tools}
        assert {"nf_harness_simulate", "nf_harness_plan_decide"} <= names
        assert "nf_harness_plan_propose" not in names and "nf_resume_project" not in names
        frozen = {**scope(planner), "candidate_ref": ref}
        args = {**frozen, "actor_tasks": [scope(a) for a in scene["actors"]], "seed": 7,
                "max_branches": 2, "max_steps_per_branch": 3, "idempotency_key": "stdio-sim"}
        result = await session.call_tool("nf_harness_simulate", args)
        assert not result.isError, result.content
        report = result.structuredContent["report"]
        assert report["accepted_world_events"] == 0 and report["status"] == "HYPOTHESIS_ONLY"
        decision = await session.call_tool("nf_harness_plan_decide", {
            **frozen, "option_id": None, "rolling_review_ref": None, "idempotency_key": "stdio-decline"})
        assert not decision.isError, decision.content
        assert decision.structuredContent["status"] == "DECLINED"

    asyncio.run(exercise(controller_port, simulate_and_decline))
    with scene["kernel"].db.uow(write=False) as repo:
        assert scene["kernel"].head(repo, scene["wr"]["work_id"]) == scene["wr"]


def test_controller_binding_checks_branch_scope_and_operator_authority(scene, monkeypatch):
    kernel = scene["kernel"]
    binding = CreativeControllerBinding(scene["wr"]["work_id"], scene["wr"]["branch_id"])
    planner_on_controller_port = McpRuntime(kernel, scene["planner"].principal, binding)
    with pytest.raises(DomainError):
        planner_on_controller_port.available_tools()
    foreign = load_fixture(kernel)
    other = CreativeControllerBinding(foreign["world_ref"]["work_id"], foreign["world_ref"]["branch_id"])
    controller = McpRuntime(kernel, foreign["principal"], other)
    with kernel.db.uow(write=False) as repo, pytest.raises(DomainError):
        controller._creative_authority(repo, scene["planner"].task["task_id"])
    monkeypatch.delenv("NF_CREATIVE_HARNESS_CONFIG")
    assert controller.available_tools() == []


def test_raw_candidate_is_bound_to_branch_even_for_an_operator(scene):
    from test_harness_planning_simulation import propose

    candidate = propose(scene)["candidate_ref"]
    kernel, wr = scene["kernel"], scene["wr"]
    other = kernel.fork(scene["owner"], wr, "CANDIDATE", "independent-branch")
    scoped = kernel.issue_grant(scene["owner"], wr["work_id"], role_id="control_operator",
                                capabilities=["READ", "ADMIN"], branch_ids=[other["branch_id"]])
    with pytest.raises(DomainError):
        kernel.get_artifact(scoped["principal"], wr["work_id"], candidate)
    with pytest.raises(DomainError):
        kernel.get_artifact(scene["actors"][0].principal, wr["work_id"], candidate)
    assert kernel.get_artifact(scene["owner"], wr["work_id"], candidate)
