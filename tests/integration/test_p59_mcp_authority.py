"""Candidate read/discovery fences with real PostgreSQL grants and the pinned SDK."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from itertools import count
from threading import Event
from time import monotonic

import pytest
from sqlalchemy import text

from novel_factory.application import mcp_runtime
from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.mcp_runtime import McpRuntime, TaskBinding
from novel_factory.application.runtime_tasks import RuntimeTaskService
from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import load_fixture, point
from novel_factory.infrastructure.model_gateway import OpenAICompatibleAdapter
from test_p5_mcp import config, error, exercise

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


@pytest.fixture
def actor_read(kernel_env):
    kernel = kernel_env["kernel"]
    world = load_fixture(kernel)
    adapter = OpenAICompatibleAdapter(
        endpoint="https://example.test/v1/chat/completions", model="fixture", api_key=None
    )
    role = RuntimeTaskService(kernel, AgentGateway(kernel, {"fixture": adapter})).prepare(
        world["principal"], world["world_ref"], role_id="character_actor",
        task_kind="character_action", actor_id=world["ids"]["protagonist"], story_time=point(1),
        subject_ids=[world["ids"]["sword"]], predicates=["owner"],
        assignment="Observe from your own knowledge cut.", adapter_id="fixture",
    )
    binding = TaskBinding(role.task["task_id"], role.task["generation"], role.worker_id)
    return kernel, world, role, McpRuntime(kernel, role.principal, binding, compact_role_reads=True)


def test_group_started_behind_revocation_cannot_disclose(actor_read):
    kernel, _, role, runtime = actor_read
    # Hold an uncommitted revoke. Observe this exact lock owner blocking the reader
    # before committing, so outcomes do not depend on thread timing.
    with ThreadPoolExecutor(1) as pool:
        with kernel.db.uow() as repo:
            grant = repo.one("grants", {"grant_id": role.principal.grant_id}, lock="update")
            repo.change("grants", {"grant_id": role.principal.grant_id},
                        {"active": False, "epoch": grant["epoch"] + 1})
            blocker = repo.conn.scalar(text("SELECT pg_backend_pid()"))
            group = pool.submit(runtime.discover_group, 3)
            deadline = monotonic() + 5
            while True:
                with kernel.db.uow(write=False) as observer:
                    blocked = observer.conn.scalar(text(
                        "SELECT EXISTS (SELECT FROM pg_stat_activity a WHERE a.datname=current_database()"
                        " AND :ours = ANY(pg_blocking_pids(a.pid)))"), {"ours": blocker})
                if blocked:
                    break
                assert monotonic() < deadline, "reader never attempted the grant lock"
            assert not group.done()
        with pytest.raises(DomainError) as denied:
            group.result(timeout=3)
        assert denied.value.code == "VIEW_DENIED"


def test_dynamic_refresh_is_outside_uow_and_rechecks_local_grant(actor_read, monkeypatch):
    kernel, world, role, runtime = actor_read
    entered = Event()
    release = Event()
    # Inject a dynamic reference solely to stop at the Workshop I/O boundary.
    # The test exercises the *local* revalidation on return; no remote provider is called.
    monkeypatch.setattr(mcp_runtime, "assignment_from_inputs", lambda inputs: {"test": "assignment"})

    def wait_for_witness(*_):
        entered.set()
        assert release.wait(5)
        return {"fixture": "witness"}

    monkeypatch.setattr(mcp_runtime, "refresh", wait_for_witness)
    with ThreadPoolExecutor(2) as pool:
        get = pool.submit(runtime.context_get)
        try:
            assert entered.wait(5)
            pool.submit(kernel.revoke_grant, world["principal"], role.principal.grant_id).result(3)
        finally:
            release.set()
        with pytest.raises(DomainError) as denied:
            get.result(3)
    assert denied.value.code == "VIEW_DENIED"


def test_dynamic_group_has_independent_outcomes(actor_read, monkeypatch):
    _, _, _, runtime = actor_read
    monkeypatch.setattr(mcp_runtime, "assignment_from_inputs", lambda inputs: {"test": "assignment"})
    calls = count(0)

    def discover():
        if next(calls) == 0:
            return ["nf_context_get"]
        raise DomainError("VIEW_DENIED", "No longer authorized")

    monkeypatch.setattr(runtime, "available_tools", discover)
    assert runtime.discover_group(3) == [["nf_context_get"], [], []]
    assert next(calls) == 3


def test_initialized_stdio_sdk_cache_does_not_authorize_after_revoke(actor_read, kernel_env, tmp_path):
    kernel, world, role, runtime = actor_read
    params = config(tmp_path / "bound", kernel_env, role.private_token, vars(runtime.binding))
    params.args.extend(["--compact-role-reads", "--batch-discovery"])

    async def probe(session):
        allowed = [item.name for item in (await session.list_tools()).tools]
        assert "nf_context_get" in allowed
        before = await session.call_tool("nf_context_get", {})
        assert not before.isError and before.structuredContent["actor_id"] == world["ids"]["protagonist"]
        kernel.revoke_grant(world["principal"], role.principal.grant_id)
        # Call directly while the SDK still has a populated definition cache.
        await error(session, "nf_context_get", {}, "VIEW_DENIED")
        replies = await asyncio.gather(*(session.list_tools() for _ in range(3)))
        assert all(not reply.tools for reply in replies)

    asyncio.run(exercise(params, probe))
