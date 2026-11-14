"""Run a real Codex app-server -> MCP tools/list and resume call, without a model turn."""
import argparse
import asyncio
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from novel_factory.infrastructure.host_process import (
    ISOLATED_PLUGIN_OPTIONS,
    isolated_host_environment,
    resolve_host_binary,
    start_host,
    stop_host,
)


async def probe(codex, binding, *, status_only=False):
    tool = "nf_project_status" if status_only else "nf_resume_project"
    receipt = await probe_bound_tool(codex, binding, {"nf_project_status","nf_resume_project"}, tool, {})
    if "counts" not in receipt["result"]:
        raise RuntimeError("Bound project tool lacked its workflow receipt")
    return receipt


async def probe_bound_tool(codex, binding, expected_tools, tool, arguments):
    """Trusted Host conformance probe; the server still authorizes each request."""
    binary = resolve_host_binary(codex)
    if not binary:
        raise RuntimeError("Codex CLI executable unavailable")
    with tempfile.TemporaryDirectory(prefix="nf-host-probe-") as temp:
        home = Path(temp, "codex")
        home.mkdir(mode=0o700)
        server = {"command": sys.executable, "args": ["-m", "novel_factory.interfaces.mcp_stdio", "--binding", str(binding)]}
        options = ["-c", "project_doc_max_bytes=0", *ISOLATED_PLUGIN_OPTIONS]
        for key, value in server.items():
            options.extend(["-c", "mcp_servers.novelfactory." + key + "=" + json.dumps(value)])
        proc = await start_host(binary, "app-server", "--listen", "stdio://", *options,
            cwd=temp, env={**isolated_host_environment(temp), "CODEX_HOME": str(home),
                           "GIT_ALLOW_PROTOCOL": "file", "GIT_TERMINAL_PROMPT": "0"},
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            limit=4_000_000)

        async def rpc(method, params, identity=None):
            request = {"method": method, "params": params}
            if identity is not None:
                request["id"] = identity
            proc.stdin.write((json.dumps(request) + "\n").encode())
            await proc.stdin.drain()
            if identity is not None:
                while raw := await proc.stdout.readline():
                    packet = json.loads(raw)
                    if packet.get("id") == identity and "method" not in packet:
                        if "error" in packet:
                            raise RuntimeError(f"{method} rejected ({packet['error']['code']})")
                        return packet["result"]
                raise RuntimeError("Codex Host exited before its reply")

        try:
            async with asyncio.timeout(90):
                await rpc("initialize", {"clientInfo": {"name": "nf_mcp_probe", "version": "2.1"},
                                         "capabilities": {"experimentalApi": True}}, 1)
                await rpc("initialized", {})
                thread = await rpc("thread/start", {"cwd": temp, "ephemeral": True,
                                   "sandbox": "read-only", "approvalPolicy": "never"}, 2)
                thread_id = thread["thread"]["id"]
                inventory = await rpc("mcpServerStatus/list", {"threadId": thread_id, "serverName": "novelfactory"}, 3)
                servers = [s for s in inventory["data"] if s["name"] == "novelfactory"]
                if len(servers) != 1 or set(servers[0]["tools"]) != set(expected_tools) or tool not in expected_tools:
                    raise RuntimeError("Unexpected live bound tool inventory")
                request = {"threadId": thread_id, "server": "novelfactory", "tool": tool, "arguments": arguments}
                response = await rpc("mcpServer/tool/call", request, 4)
                if response.get("isError") or "structuredContent" not in response:
                    raise RuntimeError("Bound tool was denied or lacked its receipt")
                version = await asyncio.to_thread(subprocess.check_output, [binary, "--version"], text=True, timeout=5)
                return {"host": version.strip(),
                        "transport": "Codex app-server to local stdio MCP", "server": "NovelFactory",
                        "tools": sorted(servers[0]["tools"]), "thread_id": thread_id, "model_turns": 0,
                        "call": request["tool"], "result": response["structuredContent"]}
        finally:
            await stop_host(proc)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--binding", type=Path, required=True)
    parser.add_argument("--status-only", action="store_true",
                        help="Check Host, MCP discovery, grant expiry and project state without waking tasks")
    args = parser.parse_args()
    try:
        receipt = asyncio.run(probe(args.codex, args.binding.resolve(), status_only=args.status_only))
    except (RuntimeError, OSError, TimeoutError) as exc:
        # No db URLs, role input, access-file contents or Host stderr in diagnostics.
        reason = str(exc) if type(exc) is RuntimeError else type(exc).__name__
        raise SystemExit(json.dumps({"status": "FAILED", "reason": reason})) from None
    print(json.dumps(receipt, indent=2))
