"""Verify a real Codex thread with both service identities. Run as the role OS user."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess

from novel_factory.infrastructure.host_process import ISOLATED_PLUGIN_OPTIONS, start_host, stop_host


async def probe(binary, challenge):
    for binding in challenge["protected_paths"]:
        try:
            fd = os.open(binding, os.O_RDONLY)
        except PermissionError:
            continue
        os.close(fd)
        raise RuntimeError("Role can open a service credential")
    if any(os.environ.get(k) for k in ("AW_DATABASE_URL","NF_DATABASE_URL","OPENAI_API_KEY","ANTHROPIC_API_KEY")):
        raise RuntimeError("Role inherited service credentials")
    version = subprocess.check_output([binary, "--version"], text=True, timeout=10).strip()
    if version != "codex-cli 0.167.0-alpha.4":
        raise RuntimeError("Unexpected native Codex version")
    proc = await start_host(binary, "app-server", "--listen", "stdio://", "-c", "project_doc_max_bytes=0",
                            *ISOLATED_PLUGIN_OPTIONS, cwd=Path.cwd(), env=os.environ.copy(),
                            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.DEVNULL, limit=4_000_000)
    seen = []
    async def rpc(method, params, seq=None):
        msg = {"method": method, "params": params}
        if seq is not None:
            msg["id"] = seq
        seen.append(method)
        proc.stdin.write((json.dumps(msg) + "\n").encode())
        await proc.stdin.drain()
        if seq is not None:
            while raw := await proc.stdout.readline():
                packet = json.loads(raw)
                if packet.get("id") == seq and "method" not in packet:
                    if "error" in packet:
                        raise RuntimeError(f"Rejected {method} ({packet['error']['code']})")
                    return packet["result"]
            raise RuntimeError("Codex exited before replying")

    try:
        async with asyncio.timeout(100):
            await rpc("initialize", {"clientInfo":{"name":"dual-role-verifier","version":"1"},
                                     "capabilities":{"experimentalApi":True}}, 1)
            await rpc("initialized", {})
            created = await rpc("thread/start", {"cwd":str(Path.cwd()),"ephemeral":True,
                                                 "sandbox":"read-only","approvalPolicy":"never"}, 2)
            tid = created["thread"]["id"]
            listing = await rpc("mcpServerStatus/list", {"threadId":tid}, 3)
            servers = {item["name"]:sorted(item["tools"]) for item in listing["data"]}
            expected_nf = {"nf_task_get","nf_context_get","nf_query_batch","nf_artifact_put"}
            assert set(servers) == {"novelfactory","workshop"}, sorted(servers)
            assert set(servers["novelfactory"]) == expected_nf, servers
            assert "aw_roster_get" in servers["workshop"] and all(t.startswith("aw_") for t in servers["workshop"])
            async def read(server, tool, seq):
                reply = await rpc("mcpServer/tool/call", {"threadId":tid,"server":server,"tool":tool,"arguments":{}}, seq)
                if reply.get("isError") or "structuredContent" not in reply:
                    raise RuntimeError(f"Rejected authorized tool {server}.{tool}")
                return reply["structuredContent"]
            task = await read("novelfactory","nf_task_get",4)
            roster = await read("workshop","aw_roster_get",5)
            assert str(task["task_id"]) == challenge["task_id"] and task["generation"] == challenge["generation"]
            assert roster["org_id"] == challenge["org_id"]
            assert challenge["employee_id"] in {r["employee_id"] for r in roster["employees"]}
            assert "turn/start" not in seen
            return {"codex":version,"thread_id":tid,"servers":servers,"model_turns":seen.count("turn/start"),
                    "authenticated_reads":["nf_task_get","aw_roster_get"],"role_binding_reads_denied":2,
                    "task_identity_matched":True,"employee_roster_matched":True}
    finally:
        await stop_host(proc)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--codex", required=True)
    p.add_argument("--challenge", type=Path, required=True)
    args = p.parse_args()
    print(json.dumps(asyncio.run(probe(args.codex,json.loads(args.challenge.read_bytes()))), sort_keys=True))
