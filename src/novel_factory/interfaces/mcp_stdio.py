"""NovelFactory local MCP server. Start with a private, server-provisioned binding file."""
import argparse
import asyncio
import math
import sys
from functools import partial

import anyio
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from novel_factory.application.bound_tools import invoke_bound_tool
from novel_factory.application.tool_catalog import _TOOLS, _VALIDATORS as _VALIDATORS
from novel_factory.domain.errors import DomainError
from .bindings import read_private_config, runtime_from_config


def tool_error(code):
    return types.CallToolResult(isError=True, content=[types.TextContent(type="text", text=code)],
                                structuredContent={"error": {"code": code}})


class BoundToolAdmission:
    """One stdio binding has a finite share of the host's thread and database budgets."""
    def __init__(self, slots, *, owned_engine=None, idle_seconds=30.0):
        self.slots = anyio.CapacityLimiter(slots)
        self._owned_engine = owned_engine
        self._idle_seconds = idle_seconds
        self._timer = None
        self._deadline = 0.0
        self._note_idle()  # Authentication may already have returned a connection to this pool.

    def _note_idle(self):
        if self._owned_engine is None or self.slots.borrowed_tokens:
            return
        loop = asyncio.get_running_loop()
        self._deadline = loop.time() + self._idle_seconds
        if self._timer is None:
            self._timer = loop.call_at(self._deadline, self._retire_if_idle)

    def _retire_if_idle(self):
        self._timer = None
        if self.slots.borrowed_tokens:
            return  # The final active call will establish a fresh idle deadline.
        loop = asyncio.get_running_loop()
        if loop.time() < self._deadline:
            self._timer = loop.call_at(self._deadline, self._retire_if_idle)
            return
        # Only the dedicated stdio server passes its engine. Its calls acquire
        # a slot on this loop before entering any worker thread. No await occurs
        # between the last check and disposal. The next use connects on demand.
        pool = self._owned_engine.pool
        if pool.checkedout() == 0 and pool.checkedin() > 0:
            self._owned_engine.dispose()

    def close(self):
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        self._owned_engine = None

    async def run(self, method, *args, **kwargs):
        try:
            self.slots.acquire_nowait()
        except anyio.WouldBlock:
            raise DomainError("BACKPRESSURE", "Bound tool capacity is full", True, "WAIT") from None
        try:
            # The SDK cancels calls through an AnyIO scope. Keep the slot until
            # the worker actually returns, including after a transport cancellation.
            with anyio.CancelScope(shield=True):
                return await anyio.to_thread.run_sync(partial(method, *args, **kwargs))
        finally:
            self.slots.release()
            self._note_idle()


class DiscoveryGroup:
    """Collect at most four requests before their shared static-role authority check."""

    def __init__(self, runtime, admission):
        self.runtime = runtime
        self.admission = admission
        self.waiters = []
        self.pending = set()

    async def list_tools(self):
        if len(self.waiters) >= 4:
            return []
        if not self.waiters:
            group = self.waiters
            job = asyncio.create_task(self._dispatch(group))
            self.pending.add(job)
            job.add_done_callback(self.pending.discard)
        waiter = asyncio.get_running_loop().create_future()
        self.waiters.append(waiter)
        return await waiter

    async def _dispatch(self, group):
        await asyncio.sleep(0.001)
        self.waiters = []
        active = [waiter for waiter in group if not waiter.cancelled()]
        if not active:
            return
        try:
            answers = await self.admission.run(self.runtime.discover_group, len(active))
        except Exception:
            answers = [[] for _ in active]
        for waiter, names in zip(active, answers, strict=True):
            if not waiter.done():
                waiter.set_result(names)

    async def drain(self):
        if self.pending:
            await asyncio.gather(*tuple(self.pending))


def make_server(runtime, *, ceiling=None, admission=None, discovery_group=None):
    server = Server("NovelFactory", version="2.1.0")
    if admission is None:
        admission = BoundToolAdmission(min(2, runtime.kernel.db.engine.pool.size()))

    @server.list_tools()
    async def list_tools():
        try:
            names = (await discovery_group.list_tools() if discovery_group is not None
                     else await admission.run(runtime.available_tools))
        except DomainError:
            return []
        except Exception:
            return []
        return [types.Tool(name=name, description=_TOOLS[name][0], inputSchema=_TOOLS[name][1])
                for name in names if ceiling is None or name in ceiling]

    @server.call_tool(validate_input=False)
    async def call_tool(name, arguments):
        try:
            return await admission.run(
                invoke_bound_tool, runtime, name, arguments, ceiling=ceiling,
                request_id=server.request_context.request_id, transport="mcp",
            )
        except DomainError as error:
            return tool_error(error.code)

    return server


async def character_input():
    """Bound untrusted Host traffic before the SDK decodes JSON or queues work."""
    for _ in range(128):
        line = await asyncio.to_thread(sys.stdin.buffer.readline, 1_048_577)
        if not line or len(line) > 1_048_576 or not line.endswith(b"\n"):
            return
        try:
            yield line.decode("utf-8")
        except UnicodeError:
            return


async def serve(config, *, character_only=False, idle_db_seconds=30.0, pool_slots=2,
                compact_role_reads=False, batch_discovery=False):
    if not math.isfinite(idle_db_seconds) or idle_db_seconds < 0:
        raise ValueError("Idle database timeout must be finite and nonnegative")
    runtime = runtime_from_config(config, pool_slots=pool_slots, compact_role_reads=compact_role_reads)
    engine = runtime.kernel.db.engine
    admission = BoundToolAdmission(min(2, engine.pool.size()), owned_engine=engine, idle_seconds=idle_db_seconds)
    discovery = DiscoveryGroup(runtime, admission) if batch_discovery else None
    try:
        if character_only:
            with runtime.kernel.db.uow(write=False) as repo:
                _, grant, _ = runtime._task(repo)
                if grant["role_id"] != "character_actor":
                    raise ValueError("Character binding required")
        server = make_server(runtime, admission=admission, discovery_group=discovery)
        async with stdio_server(stdin=character_input() if character_only else None) as (reader, writer):
            await server.run(reader, writer, server.create_initialization_options())
    finally:
        if discovery is not None:
            await discovery.drain()
        admission.close()
        runtime.kernel.db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binding", required=True)
    parser.add_argument("--character-only", action="store_true", help="Require a live character task binding")
    parser.add_argument("--idle-db-seconds", type=float, default=30.0,
                        help="Release this stdio process's unused database connections after this idle period")
    parser.add_argument("--pool-slots", type=int, choices=(1, 2), default=2,
                        help="Local connection and worker limit for this process")
    parser.add_argument("--compact-role-reads", action="store_true",
                        help="Resolve static role proof and read in one transaction")
    parser.add_argument("--batch-discovery", action="store_true",
                        help="Group already waiting tools/list requests on this binding")
    args = parser.parse_args()
    try:
        asyncio.run(serve(read_private_config(args.binding), character_only=args.character_only,
                          idle_db_seconds=args.idle_db_seconds, pool_slots=args.pool_slots,
                          compact_role_reads=args.compact_role_reads, batch_discovery=args.batch_discovery))
    except KeyboardInterrupt:
        return
    except Exception:
        print("NovelFactory MCP startup failed; check private configuration, grant and database.", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
