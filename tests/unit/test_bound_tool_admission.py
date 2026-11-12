import threading

import anyio
import pytest

from novel_factory.domain.errors import DomainError
from novel_factory.interfaces.mcp_stdio import BoundToolAdmission


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
