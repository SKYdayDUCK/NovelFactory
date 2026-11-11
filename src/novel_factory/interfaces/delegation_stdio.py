"""Dedicated stdio MCP profile for a private Workshop installation binding."""

import argparse
import asyncio
import sys
from pathlib import Path

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from novel_factory.application.delegation import SCHEMAS, Delegations
from novel_factory.application.kernel import Kernel
from novel_factory.domain.errors import DomainError
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.objects import ObjectStore
from novel_factory.interfaces.mcp_stdio import read_private_config, tool_error

_DESCRIPTIONS = {
    "nf_delegation_submit": "Consume an NF-authorized task slot with a stable operation ID.",
    "nf_delegation_get": "Read the original task binding and authoritative accounting for this installation.",
}


async def bounded_input():
    while True:
        line = await asyncio.to_thread(sys.stdin.buffer.readline, 16385)
        if not line or len(line) > 16384 or not line.endswith(b"\n"):
            return
        try:
            yield line.decode("utf-8")
        except UnicodeError:
            return


async def serve(config):
    if (
        set(config) != {"database_url", "object_root", "token", "installation_id"}
        or not Path(config["object_root"]).is_absolute()
    ):
        raise ValueError("Invalid installation config")
    kernel = Kernel(
        Database(config["database_url"], pool_size=2), ObjectStore(config["object_root"])
    )
    runtime = Delegations(kernel, config["installation_id"], config["token"])
    server = Server("NovelFactory.Delegation", version="1.0")

    @server.list_tools()
    async def discover():
        def names():
            with kernel.db.uow(write=False) as repo:
                runtime.identity(repo)
            return [
                types.Tool(name=k, description=_DESCRIPTIONS[k], inputSchema=v)
                for k, v in SCHEMAS.items()
            ]

        try:
            return await asyncio.to_thread(names)
        except DomainError:
            return []

    @server.call_tool(validate_input=False)
    async def call(name, arguments):
        try:
            return await asyncio.to_thread(runtime.call, name, arguments)
        except DomainError as e:
            return tool_error(e.code)
        except Exception:  # noqa: BLE001 - keep credential-bearing driver errors off stdio
            return tool_error("SERVICE_UNAVAILABLE")

    try:
        async with stdio_server(stdin=bounded_input()) as (reader, writer):
            await server.run(reader, writer, server.create_initialization_options())
    finally:
        kernel.db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binding", required=True)
    try:
        asyncio.run(serve(read_private_config(parser.parse_args().binding)))
    except KeyboardInterrupt:
        return
    except Exception:  # noqa: BLE001 - keep credential-bearing driver errors off stdio
        print(
            "NF delegation startup failed; check private binding and installation.", file=sys.stderr
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
