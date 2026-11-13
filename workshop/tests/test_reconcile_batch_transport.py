"""A lost transport response retires its handle without replaying the operation."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

from agent_workshop import reconcile


def test_failed_batch_call_is_not_replayed_and_next_operation_can_reconnect(monkeypatch):
    launched = []
    closed = []
    calls = []

    @asynccontextmanager
    async def transport(_):
        number = len(launched)
        launched.append(number)
        try:
            yield (number,)
        finally:
            closed.append(number)

    class Session:
        def __init__(self, number):
            self.number = number

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def initialize(self):
            pass

        async def list_tools(self):
            return SimpleNamespace(tools=[SimpleNamespace(name="nf_delegation_submit")])

        async def call_tool(self, _, args):
            calls.append((self.number, args["operation_id"]))
            if self.number == 0:
                raise ConnectionError("injected lost response")
            return SimpleNamespace(isError=False, structuredContent={"received": args["operation_id"]})

    monkeypatch.setattr(reconcile, "stdio_client", transport)
    monkeypatch.setattr(reconcile, "ClientSession", Session)

    async def run():
        async with reconcile.batch_remote(None) as invoke:
            assert not launched
            try:
                await invoke("nf_delegation_submit", {"operation_id": "original"})
            except ConnectionError:
                pass
            else:
                raise AssertionError("Lost response was reported as confirmed")
            assert calls == [(0, "original")] and closed == [0]
            assert await invoke("nf_delegation_submit", {"operation_id": "later"}) == {"received": "later"}
            assert await invoke("nf_delegation_submit", {"operation_id": "last"}) == {"received": "last"}

    asyncio.run(run())
    assert launched == closed == [0, 1]
    assert calls == [(0, "original"), (1, "later"), (1, "last")]
