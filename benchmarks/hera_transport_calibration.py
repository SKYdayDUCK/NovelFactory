"""Transport-only diagnostic. Keep the frozen P3 offer intact, change only pool layout."""
from contextlib import AsyncExitStack

import httpx

from benchmarks import client_calibration as calibration

OriginalAsyncClient = httpx.AsyncClient


class StripedHTTPX:
    def __init__(self, *, limits, **kwargs):
        self.stack = AsyncExitStack()
        self.index = 0
        lanes = 16
        assert limits.max_connections == limits.max_keepalive_connections == 256
        self.clients = [OriginalAsyncClient(
            limits=httpx.Limits(max_connections=16, max_keepalive_connections=16),
            **kwargs) for _ in range(lanes)]

    async def __aenter__(self):
        try:
            for client in self.clients:
                await self.stack.enter_async_context(client)
        except BaseException:
            await self.stack.aclose()
            raise
        return self

    async def __aexit__(self, *exc):
        await self.stack.__aexit__(*exc)

    async def post(self, *args, **kwargs):
        client = self.clients[self.index]
        self.index = (self.index + 1) % len(self.clients)
        return await client.post(*args, **kwargs)

if __name__ == '__main__':
    # Monkeypatch only the generator process in this isolated experiment.
    httpx.AsyncClient = StripedHTTPX
    calibration.main()
