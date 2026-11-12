"""The dynamic registry is accessible only on an enabled, work-and-branch-bound NF controller."""
import asyncio
import json
from pathlib import Path
from uuid import uuid4

import pytest

from novel_factory.fixtures import load_fixture
from novel_factory.infrastructure.creative_config import CreativeHarnessConfig
from test_p5_mcp import config, error, exercise

pytestmark = [pytest.mark.postgres, pytest.mark.integration]

DYNAMIC_TOOLS = {
    "nf_harness_agent_register", "nf_harness_agent_select", "nf_harness_agent_history", "nf_harness_agent_retire",
}


def controller_port(path, kernel_env, world, flags, *, token=None):
    binding = {"mode": "creative_controller", **{k: world["world_ref"][k] for k in ("work_id", "branch_id")}}
    port = config(path, kernel_env, token or world["token"], binding)
    port.env = {"PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")}
    if flags is not None:
        port.env["NF_CREATIVE_HARNESS_CONFIG"] = json.dumps(flags.as_dict())
    return port


@pytest.mark.parametrize("flags", [None, CreativeHarnessConfig(enabled=True)])
def test_dynamic_controls_hidden_when_disabled(kernel_env, tmp_path, flags):
    world = load_fixture(kernel_env["kernel"])
    port = controller_port(tmp_path / "controller", kernel_env, world, flags)

    async def check(session):
        names = {t.name for t in (await session.list_tools()).tools}
        assert names.isdisjoint(DYNAMIC_TOOLS)
        assert ("nf_harness_plan_decide" in names) == bool(flags)
        for name in sorted(DYNAMIC_TOOLS):
            await error(session, name, {}, "VIEW_DENIED")

    asyncio.run(exercise(port, check))


def test_dynamic_admin_cannot_retarget_the_private_port(kernel_env, tmp_path):
    kernel = kernel_env["kernel"]
    world = load_fixture(kernel)
    wr = world["world_ref"]
    sibling = kernel.fork(world["principal"], wr, "CANDIDATE", "second-binding")
    foreign = load_fixture(kernel)["world_ref"]
    flags = CreativeHarnessConfig(enabled=True, dynamic_agents=True)
    port = controller_port(tmp_path / "author", kernel_env, world, flags)
    ref = {"artifact_id": str(uuid4()), "artifact_revision_id": str(uuid4()), "sha256": "0"*64}

    async def check(session):
        names = {t.name for t in (await session.list_tools()).tools}
        assert DYNAMIC_TOOLS <= names
        for other in (sibling, foreign):
            await error(session, "nf_harness_agent_history", {"world_ref": other, "instance_ref": ref}, "VIEW_DENIED")
            await error(session, "nf_harness_agent_retire", {"world_ref": other, "instance_ref": ref,
                        "action": "EXIT", "expected_epoch": 1, "idempotency_key": "other"}, "VIEW_DENIED")
            await error(session, "nf_harness_agent_select", {"world_ref": other, "instance_ref": ref, "revision_ref": ref,
                        "expected_epoch": 1, "idempotency_key": "other"}, "VIEW_DENIED")

    asyncio.run(exercise(port, check))
    role = kernel.issue_grant(world["principal"], wr["work_id"], role_id="writer", capabilities=["READ"],
                              branch_ids=[wr["branch_id"]])

    async def unprivileged(session):
        assert not (await session.list_tools()).tools
        await error(session, "nf_harness_agent_register", {}, "VIEW_DENIED")

    restricted = controller_port(tmp_path / "writer", kernel_env, world, flags, token=role["token"])
    asyncio.run(exercise(restricted, unprivileged))
