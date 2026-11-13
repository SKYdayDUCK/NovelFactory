import asyncio
import threading
from unittest.mock import Mock, patch

import anyio
import pytest
from mcp import types

from novel_factory.domain.errors import DomainError
from novel_factory.interfaces.mcp_stdio import BoundToolAdmission, make_server


def test_cancelled_host_call_holds_slot_until_the_worker_finishes():
    entered = threading.Event()
    release = threading.Event()
    completed = threading.Event()

    def work():
        entered.set()
        assert release.wait(5)
        completed.set()

    async def scenario():
        admission = BoundToolAdmission(1)
        with anyio.fail_after(5):
            async with anyio.create_task_group() as group:
                async def first(*, task_status=anyio.TASK_STATUS_IGNORED):
                    with anyio.CancelScope() as caller:
                        task_status.started(caller)
                        await admission.run(work)

                caller = await group.start(first)
                assert await anyio.to_thread.run_sync(entered.wait, 3)
                caller.cancel()
                await anyio.sleep(0)
                with pytest.raises(DomainError) as full:
                    await admission.run(lambda: True)
                assert full.value.code == "BACKPRESSURE"
                release.set()
        assert completed.is_set()
        assert await admission.run(lambda: True)

    anyio.run(scenario)


def test_idle_timer_waits_for_active_worker_and_keeps_one_handle():
    async def scenario():
        entered = threading.Event()
        release = threading.Event()
        engine = Mock()
        engine.pool.checkedout.return_value = 0
        engine.pool.checkedin.return_value = 1
        admission = BoundToolAdmission(1, owned_engine=engine, idle_seconds=30)
        timer = admission._timer
        await admission.run(lambda: True)
        assert timer is admission._timer

        def work():
            entered.set()
            assert release.wait(5)

        async with anyio.create_task_group() as group:
            group.start_soon(admission.run, work)
            assert await anyio.to_thread.run_sync(entered.wait, 3)
            # Fire the scheduled callback as if the initial deadline arrived.
            timer.cancel()
            admission._retire_if_idle()
            engine.dispose.assert_not_called()
            release.set()
        assert admission._timer is not None
        admission._timer.cancel()
        admission._deadline = 0.0
        engine.pool.checkedout.return_value = 1
        admission._retire_if_idle()
        engine.dispose.assert_not_called()
        engine.pool.checkedout.return_value = 0
        admission._retire_if_idle()
        engine.dispose.assert_called_once()
        admission.close()
        assert admission._timer is None

    anyio.run(scenario)


def test_embedded_server_leaves_its_shared_kernel_pool_with_its_owner():
    async def scenario():
        runtime = Mock()
        runtime.kernel.db.engine.pool.size.return_value = 2
        runtime.available_tools.return_value = ["nf_project_status"]
        # Only owned pools require a clock or a disposal callback.
        with patch("novel_factory.interfaces.mcp_stdio.asyncio", wraps=asyncio) as scheduling:
            server = make_server(runtime)
            result = await server.request_handlers[types.ListToolsRequest](types.ListToolsRequest())
            scheduling.get_running_loop.assert_not_called()
        assert [tool.name for tool in result.root.tools] == ["nf_project_status"]
        runtime.kernel.db.engine.dispose.assert_not_called()

    anyio.run(scenario)
