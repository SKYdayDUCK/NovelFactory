"""Run a real Codex app-server -> MCP tools/list and resume call, without a model turn."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


async def probe(codex, binding):
    binary = shutil.which(codex)
    if not binary:
        raise RuntimeError("Codex CLI executable unavailable")
    with tempfile.TemporaryDirectory(prefix="nf-host-probe-") as temp:
        home = Path(temp, "codex")
        home.mkdir(mode=0o700)
        server = {"command": sys.executable, "args": ["-m", "novel_factory.interfaces.mcp_stdio", "--binding", str(binding)]}
        options = ["-c", "project_doc_max_bytes=0"]
        for key, value in server.items():
            options.extend(["-c", "mcp_servers.novelfactory." + key + "=" + json.dumps(value)])
        proc = await asyncio.create_subprocess_exec(binary, "app-server", "--listen", "stdio://", *options,
            cwd=temp, env={"PATH": os.defpath, "CODEX_HOME": str(home), "HOME": temp},
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
                if len(servers) != 1 or set(servers[0]["tools"]) != {"nf_project_status", "nf_resume_project"}:
                    raise RuntimeError("Unexpected live operator tool inventory")
                request = {"threadId": thread_id, "server": "novelfactory", "tool": "nf_resume_project", "arguments": {}}
                response = await rpc("mcpServer/tool/call", request, 4)
                if response.get("isError") or "counts" not in response["structuredContent"]:
                    raise RuntimeError("Resume was denied or lacked its workflow receipt")
                return {"host": subprocess.check_output([binary, "--version"], text=True).strip(),
                        "transport": "Codex app-server to local stdio MCP", "server": "NovelFactory",
                        "tools": sorted(servers[0]["tools"]), "thread_id": thread_id, "model_turns": 0,
                        "call": request["tool"], "result": response["structuredContent"]}
        finally:
            if proc.returncode is None:
                proc.kill()
            await proc.wait()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--binding", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(probe(args.codex, args.binding.resolve())), indent=2))
