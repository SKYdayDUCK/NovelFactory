"""Observe the real standalone MCP process's own PostgreSQL sessions through its full idle lifecycle."""
import asyncio
from datetime import UTC, datetime, timedelta
import json
from time import perf_counter
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url

from novel_factory.fixtures import load_fixture
from test_p5_mcp import config, exercise

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def test_stdio_drops_idle_sessions_and_revalidates_after_reconnect(kernel_env, tmp_path, record_property):
    kernel = kernel_env["kernel"]
    world = load_fixture(kernel)
    work, branch = (world["world_ref"][key] for key in ("work_id", "branch_id"))
    scoped = kernel.issue_grant(world["principal"], work, role_id="resume_operator",
        capabilities=["PROJECT_RESUME"], branch_ids=[branch], expires_at=datetime.now(UTC) + timedelta(hours=1))
    app = "nf_idle_test_" + uuid4().hex
    db_url = make_url(kernel_env["url"]).update_query_dict({"application_name": app})
    params = config(tmp_path / "role.json", {**kernel_env, "url": db_url.render_as_string(hide_password=False)},
                    scoped["token"], {"work_id": work, "branch_id": branch})
    params.args.extend(["--idle-db-seconds", "1.0"])
    durations = {"hot_ms": [], "reconnect_ms": []}

    def pids():
        with kernel.db.uow(write=False) as repo:
            return set(repo.conn.scalars(text("SELECT pid FROM pg_stat_activity WHERE application_name=:app"),
                                         {"app": app}))

    async def detached():
        async with asyncio.timeout(8):
            while pids():
                await asyncio.sleep(.05)

    async def inspect(session):
        assert pids()  # The child authenticated even before a tools/list or tools/call.
        for _ in range(3):
            await detached()
            start = perf_counter()
            cold = await session.call_tool("nf_project_status", {})
            durations["reconnect_ms"].append(round((perf_counter() - start) * 1000, 3))
            assert not cold.isError and cold.structuredContent["counts"] == {"READY": 0, "RUNNING": 0, "WAITING": 0}
            hot_pids = pids()
            assert len(hot_pids) == 1
            for _ in range(4):
                start = perf_counter()
                hot = await session.call_tool("nf_project_status", {})
                durations["hot_ms"].append(round((perf_counter() - start) * 1000, 3))
                assert not hot.isError and hot.structuredContent == cold.structuredContent
                assert pids() == hot_pids
        await detached()
        kernel.revoke_grant(world["principal"], scoped["principal"].grant_id)
        denied = await session.call_tool("nf_project_status", {})
        assert denied.isError and denied.structuredContent == {"error": {"code": "VIEW_DENIED"}}

    asyncio.run(exercise(params, inspect))
    assert not pids()
    record_property("binding_latency", json.dumps(durations))
