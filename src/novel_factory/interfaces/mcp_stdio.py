"""NovelFactory local MCP server. Start with a private, server-provisioned binding file."""
import argparse
import asyncio
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
    def __init__(self, slots):
        self.slots = anyio.CapacityLimiter(slots)

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


def make_server(runtime, *, ceiling=None):
    server = Server("NovelFactory", version="2.1.0")
    admission = BoundToolAdmission(min(2, runtime.kernel.db.engine.pool.size()))

    @server.list_tools()
    async def list_tools():
        try:
            names = await admission.run(runtime.available_tools)
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


async def serve(config, *, character_only=False):
    runtime = runtime_from_config(config)
    try:
        if character_only:
            with runtime.kernel.db.uow(write=False) as repo:
                _, grant, _ = runtime._task(repo)
                if grant["role_id"] != "character_actor":
                    raise ValueError("Character binding required")
        server = make_server(runtime)
        async with stdio_server(stdin=character_input() if character_only else None) as (reader, writer):
            await server.run(reader, writer, server.create_initialization_options())
    finally:
        runtime.kernel.db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binding", required=True)
    parser.add_argument("--character-only", action="store_true", help="Require a live character task binding")
    args = parser.parse_args()
    try:
        asyncio.run(serve(read_private_config(args.binding), character_only=args.character_only))
    except KeyboardInterrupt:
        return
    except Exception:
        print("NovelFactory MCP startup failed; check private configuration, grant and database.", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
