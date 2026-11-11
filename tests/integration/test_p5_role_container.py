"""Real Codex app-server with one MCP lease across a Linux container boundary."""
import asyncio
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from test_p5_mcp import config

from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.runtime_tasks import RuntimeTaskService
from novel_factory.fixtures import load_fixture, point
from novel_factory.infrastructure.model_gateway import OpenAICompatibleAdapter
from novel_factory.infrastructure.role_container import container_command

ROOT = Path(__file__).resolve().parents[2]
pytestmark = [pytest.mark.integration, pytest.mark.postgres]


def binaries(tmp_path):
    if not all(shutil.which(tool) for tool in ("bwrap", "unshare", "codex", "cc")):
        pytest.skip("Linux namespaces, static C toolchain and Codex are required")
    relay = tmp_path / "role-mcp"
    subprocess.run(["cc", "-Wall", "-Wextra", "-Werror", "-static", "-O2",
                    ROOT / "adapters/codex/role_relay.c", "-o", relay], check=True)
    codex = shutil.which("codex")
    with subprocess.Popen(container_command(codex, relay, tmp_path) + ["/bin/codex", "--version"],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
        stdout, stderr = process.communicate(timeout=10)
    if process.returncode:
        if b"Operation not permitted" in stderr or b"namespace" in stderr:
            pytest.skip("Linux container setup denied by this runner")
        raise AssertionError(f"unexpected container exit {process.returncode}")
    assert b"codex-cli" in stdout
    return codex, relay


def test_role_container_calls_actual_task_mcp(kernel_env, tmp_path):
    codex, relay = binaries(tmp_path)
    kernel = kernel_env["kernel"]
    world = load_fixture(kernel)
    adapter = OpenAICompatibleAdapter(endpoint="https://example.test/v1/chat/completions", model="test", api_key=None)
    task = RuntimeTaskService(kernel, AgentGateway(kernel, {"external": adapter})).prepare(
        world["principal"], world["world_ref"], role_id="character_actor", task_kind="character_action",
        actor_id=world["ids"]["protagonist"], story_time=point(1), subject_ids=[world["ids"]["sword"]],
        predicates=["owner"], assignment="Propose an action", adapter_id="external")
    binding = tmp_path / "actor.json"
    config(binding, kernel_env, task.private_token, {
        "task_id": task.task["task_id"], "generation": task.task["generation"], "worker_id": task.worker_id})

    async def check():
        host = await asyncio.create_subprocess_exec(
            sys.executable, ROOT / "scripts/run_role_host.py", "--binding", binding,
            "--codex", codex, "--relay", relay, "--lifetime", "50",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, limit=4_000_000)

        async def rpc(method, params, number=None):
            msg = {"method": method, "params": params}
            if number:
                msg["id"] = number
            host.stdin.write((json.dumps(msg) + "\n").encode())
            await host.stdin.drain()
            if number:
                while line := await host.stdout.readline():
                    response = json.loads(line)
                    if response.get("id") == number and "method" not in response:
                        assert "error" not in response, response.get("error", {}).get("code")
                        return response["result"]
                raise AssertionError("Isolated Codex Host exited before RPC reply")

        try:
            async with asyncio.timeout(45):
                await rpc("initialize", {"clientInfo": {"name": "nf-test", "version": "1"},
                                         "capabilities": {"experimentalApi": True}}, 1)
                await rpc("initialized", {})
                thread = await rpc("thread/start", {"cwd": "/work", "ephemeral": True,
                                   "sandbox": "read-only", "approvalPolicy": "never"}, 2)
                tid = thread["thread"]["id"]
                result = await rpc("mcpServerStatus/list", {"threadId": tid, "serverName": "novelfactory"}, 3)
                assert set(result["data"][0]["tools"]) == {"nf_task_get", "nf_context_get", "nf_query_batch", "nf_artifact_put"}
                base = {"threadId": tid, "server": "novelfactory"}
                response = await rpc("mcpServer/tool/call", {**base, "tool": "nf_context_get", "arguments": {}}, 4)
                assert not response["isError"]
                assert response["structuredContent"]["actor_id"] == world["ids"]["protagonist"]
                identity = await rpc("mcpServer/tool/call", {**base, "tool": "nf_task_get", "arguments": {}}, 10)
                assert not identity["isError"] and identity["structuredContent"]["task_id"] == task.task["task_id"]
                for number, subject in enumerate([world["ids"]["rival"], "00000000-0000-4000-b000-000000000001"], start=5):
                    response = await rpc("mcpServer/tool/call", {**base, "tool": "nf_query_batch",
                        "arguments": {"lookups": [{"subject_id": subject, "predicate": "owner"}]}}, number)
                    assert response["isError"] and response["structuredContent"] == {"error": {"code": "VIEW_DENIED"}}
                proposal = {**base, "tool": "nf_artifact_put", "arguments": {
                    "content": "I choose to wait.", "idempotency_key": "host-intent"}}
                first = await rpc("mcpServer/tool/call", proposal, 7)
                second = await rpc("mcpServer/tool/call", proposal, 8)
                assert not first["isError"] and first["structuredContent"] == second["structuredContent"]
                with kernel.db.uow(write=False) as repo:
                    assert kernel.head(repo, world["world_ref"]["work_id"]) == world["world_ref"]
                    assert len(repo.many("artifacts", {"kind": "MCP_TASK_CANDIDATE",
                                                      "work_id": world["world_ref"]["work_id"]})) == 1
                kernel.revoke_grant(world["principal"], task.principal.grant_id)
                response = await rpc("mcpServer/tool/call", {**base, "tool": "nf_context_get", "arguments": {}}, 9)
                assert response["isError"] and response["structuredContent"] == {"error": {"code": "VIEW_DENIED"}}
        finally:
            host.stdin.close()
            await asyncio.wait_for(host.wait(), 12)
    asyncio.run(check())


def test_namespace_hides_service_resources(kernel_env, tmp_path, monkeypatch):
    """Test capability boundaries against disposable, service-owned canaries."""
    from sqlalchemy.engine import make_url

    codex, _ = binaries(tmp_path)
    private = tmp_path / "service-binding"
    private.write_text("ISOLATION-TEST")
    monkeypatch.setenv("NF_ROLE_TEST_SECRET", "ISOLATION-TEST")
    code = tmp_path / "canary.c"
    code.write_text(r"""
#include <arpa/inet.h>
#include <fcntl.h>
#include <stdlib.h>
#include <unistd.h>
int main(int argc, char **argv) {
    if (argc != 3) return 1;
    if (access(argv[1], F_OK) == 0 || access("/bin/sh", X_OK) == 0) return 2;
    if (getenv("NF_ROLE_TEST_SECRET") || getenv("NF_DATABASE_URL")) return 3;
    int s = socket(AF_INET, SOCK_STREAM, 0);
    struct sockaddr_in addr = { .sin_family = AF_INET, .sin_port = htons(atoi(argv[2])) };
    inet_pton(AF_INET, "127.0.0.1", &addr.sin_addr);
    if (s >= 0 && connect(s, (struct sockaddr *)&addr, sizeof(addr)) == 0) return 4;
    return 0;
}
""")
    binary = tmp_path / "canary"
    subprocess.run(["cc", "-Wall", "-Wextra", "-Werror", "-O2", "-static", code, "-o", binary], check=True)
    channel = tmp_path / "empty-channel"
    channel.mkdir()
    # Replace only the trusted relay binary with an in-container diagnostic;
    # the mount set, environment, network, capabilities and userns are identical.
    url = make_url(kernel_env["url"])
    assert url.port and url.host in {"127.0.0.1", "localhost"}
    result = subprocess.run(container_command(codex, binary, channel)
                            + ["/bin/role-mcp", str(private), str(url.port)], timeout=10, check=False)
    assert result.returncode == 0
