"""Read one frozen BELIEF context over a real character-bound MCP transport."""
import asyncio
import json
import os
import tempfile
from contextlib import ExitStack, suppress
from pathlib import Path

from mcp.server.stdio import stdio_server

from novel_factory.application.kernel import require
from novel_factory.application.mcp_runtime import McpRuntime, TaskBinding
from novel_factory.domain.errors import DomainError
from novel_factory.infrastructure.role_container import container_command
from novel_factory.interfaces.mcp_stdio import make_server


class ReplyPipe(asyncio.Protocol):
    """Async UTF-8 output for the SDK's newline-delimited stdio transport."""
    def __init__(self):
        self.ready = asyncio.Event()
        self.ready.set()
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def pause_writing(self):
        self.ready.clear()

    def resume_writing(self):
        self.ready.set()

    def connection_lost(self, exc):
        self.transport = None
        self.ready.set()

    async def write(self, value):
        await self.ready.wait()
        if self.transport is None:
            raise BrokenPipeError
        self.transport.write(value.encode("utf-8"))

    async def flush(self):
        await self.ready.wait()


async def _read_context(kernel, principal, binding, codex, relay, channel, tool, arguments):
    runtime = McpRuntime(kernel, principal, binding)
    server = make_server(runtime, ceiling={tool})
    loop = asyncio.get_running_loop()
    for name in ("requests", "replies"):
        os.mkfifo(channel / name, 0o600)
    # Keepers make startup nonblocking. asyncio transports must use distinct
    # read-only/write-only descriptors; an RDWR write transport can observe its
    # own buffered output as a peer-close notification.
    with ExitStack() as stack:
        for name in ("requests", "replies"):
            keeper = os.open(channel / name, os.O_RDWR | os.O_CLOEXEC)
            stack.callback(os.close, keeper)
        requests = stack.enter_context(os.fdopen(os.open(channel / "requests", os.O_RDONLY | os.O_NONBLOCK), "rb", buffering=0))
        replies = stack.enter_context(os.fdopen(os.open(channel / "replies", os.O_WRONLY | os.O_NONBLOCK), "wb", buffering=0))
        reader = asyncio.StreamReader(limit=1_048_576)
        input_transport, _ = await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), requests)
        try:
            output_transport, output = await loop.connect_write_pipe(ReplyPipe, replies)
        except BaseException:
            input_transport.close()
            raise
        try:
            async def lines():
                for _ in range(128):
                    data = await reader.readline()
                    if not data:
                        return
                    yield data.decode("utf-8")

            args = container_command(codex, relay, channel) + [
                "/bin/codex", "app-server", "--listen", "stdio://", "-c", "project_doc_max_bytes=0",
                "-c", "analytics.enabled=false", "-c", 'mcp_servers.novelfactory.command="/bin/role-mcp"',
                "-c", "mcp_servers.novelfactory.enabled_tools=" + json.dumps([tool]),
            ]
            host = await asyncio.create_subprocess_exec(*args, env={"PATH": os.defpath},
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, limit=6_000_000)
            try:
                async with stdio_server(stdin=lines(), stdout=output) as (rx, tx):
                    task = asyncio.create_task(server.run(rx, tx, server.create_initialization_options()))
                    try:
                        return await _call(host, tool, arguments)
                    finally:
                        task.cancel()
                        with suppress(asyncio.CancelledError):
                            await task
                        # Exit both stdio transport loops before joining their task group.
                        input_transport.close()
            finally:
                if host.returncode is None:
                    host.kill()
                await host.communicate()
        finally:
            input_transport.close()
            output_transport.close()


async def _call(host, tool, arguments):
    async def rpc(method, params, rpc_id=None):
        message = {"method": method, "params": params}
        if rpc_id is not None:
            message["id"] = rpc_id
        host.stdin.write((json.dumps(message) + "\n").encode())
        await host.stdin.drain()
        if rpc_id is None:
            return
        while data := await host.stdout.readline():
            packet = json.loads(data)
            if packet.get("id") == rpc_id and "method" not in packet:
                if "error" in packet:
                    break
                return packet["result"]
        raise DomainError("HOST_BINDING_MISMATCH", "Role Host did not return its bound result")

    await rpc("initialize", {"clientInfo": {"name": "nf_role_read", "version": "1"},
                             "capabilities": {"experimentalApi": True}}, 1)
    await rpc("initialized", {})
    thread = await rpc("thread/start", {"cwd": "/work", "ephemeral": True,
                       "sandbox": "read-only", "approvalPolicy": "never"}, 2)
    thread_id = thread["thread"]["id"]
    inventory = await rpc("mcpServerStatus/list", {"threadId": thread_id, "serverName": "novelfactory"}, 3)
    require(any(s.get("name") == "novelfactory" and set(s["tools"]) == {tool}
                for s in inventory["data"]), "VIEW_DENIED", "Role tool unavailable")
    response = await rpc("mcpServer/tool/call", {"threadId": thread_id, "server": "novelfactory",
                         "tool": tool, "arguments": arguments}, 4)
    require(not response.get("isError") and isinstance(response.get("structuredContent"), dict),
            "VIEW_DENIED", "Role context is unavailable")
    return response["structuredContent"], thread_id


def read_context(kernel, principal, task, codex, relay, *, tool="nf_context_get", arguments=None):
    """Called in a worker before provider send, with no DB transaction held."""
    require(tool in {"nf_context_get", "nf_query_batch"}, "VIEW_DENIED", "Unsupported role read")
    binding = TaskBinding(task["task_id"], task["generation"], task["lease_owner"])

    async def bounded(channel):
        async with asyncio.timeout(30):
            return await _read_context(kernel, principal, binding, codex, relay, channel, tool, arguments or {})

    try:
        with tempfile.TemporaryDirectory(prefix="nf-role-context-") as directory:
            return asyncio.run(bounded(Path(directory)))
    except (ExceptionGroup, DomainError, OSError, RuntimeError, ValueError, KeyError, TypeError, EOFError):
        raise DomainError("HOST_ISOLATION_UNVERIFIED", "Role context Host unavailable") from None
