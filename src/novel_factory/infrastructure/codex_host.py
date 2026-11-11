"""One fresh Codex app-server thread and process per grant-bound model turn.

The local Responses endpoint translates exactly one stateless text result into
a single assistant message. Codex's tool definitions, system prompt and file
context never cross the provider boundary. Tool-looking text stays JSON text.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import secrets
import shutil
import tempfile
from threading import Event, Thread
from uuid import uuid4

from novel_factory.domain.errors import DomainError
from novel_factory.infrastructure.model_gateway import ModelCallFailure, OpenAICompatibleAdapter


def _events(answer):
    item = {"id": "msg_" + uuid4().hex, "type": "message", "role": "assistant",
            "status": "completed", "content": [{"type": "output_text", "text": answer.text,
                                                "annotations": []}]}
    response = {"id": "resp_" + uuid4().hex, "object": "response", "model": "nf-text",
                "status": "in_progress", "output": []}
    events = [
        ("response.created", {"response": response}),
        ("response.output_item.added", {"output_index": 0, "item": {**item, "status": "in_progress", "content": []}}),
        ("response.output_text.delta", {"item_id": item["id"], "output_index": 0, "content_index": 0, "delta": answer.text}),
        ("response.output_item.done", {"output_index": 0, "item": item}),
        ("response.completed", {"response": {**response, "status": "completed", "output": [item]}}),
    ]
    for seq, (name, fields) in enumerate(events):
        yield (f"event: {name}\ndata: " + json.dumps({"type": name, "sequence_number": seq, **fields}) + "\n\n").encode()


class _TextBridge(HTTPServer):
    def __init__(self, backend, request, authorize_send):
        super().__init__(("127.0.0.1", 0), _TextEndpoint)
        self.route = "/" + secrets.token_hex(32)
        self.backend, self.request = backend, request
        self.authorize_send = authorize_send
        self.used, self.answer = False, None
        self.timeout = .1
        self.stopped = Event()

    def serve(self):
        while not self.stopped.is_set():
            self.handle_request()


class _TextEndpoint(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def do_POST(self):
        bridge = self.server
        if self.path != bridge.route + "/v1/responses":
            self.send_error(404)
            return
        # Single-threaded server: even a transport retry cannot spend twice.
        if bridge.used:
            self.send_error(409)
            return
        try:
            size = int(self.headers.get("Content-Length", "-1"))
            if not 0 <= size <= 4_000_000 or len(self.rfile.read(size)) != size:
                self.send_error(413)
                return
            bridge.used = True
            bridge.authorize_send(bridge.request.attempt_id)
            result = bridge.backend.generate(bridge.request)
            if result.request_sha256 != bridge.request.request_sha256 or not isinstance(result.text, str) or len(result.text.encode()) > 2_000_000:
                raise ValueError("Unbound or oversized response")
            bridge.answer = result
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for chunk in _events(result):
                self.wfile.write(chunk)
        except Exception:
            # Transport and provider errors can contain credentials and user data.
            self.send_error(502)


class CodexAppServerAdapter:
    def __init__(self, backend, journal, *, binary="codex", timeout_seconds=90):
        if not isinstance(backend, OpenAICompatibleAdapter) or not 1 <= timeout_seconds <= 600:
            raise DomainError("INVALID_MODEL_CONFIG", "A bounded stateless text backend is required")
        self.backend, self.journal = backend, journal
        self.binary, self.timeout_seconds = binary, timeout_seconds

    @property
    def configuration(self):
        return {**self.backend.configuration, "host_protocol": "codex-app-server:text-bridge-v1",
                "host_deadline_seconds": self.timeout_seconds,
                "isolation_evidence": "Fresh process and thread, one stateless request, only text can reach Codex"}

    def check_available(self):
        self.backend.check_available()
        if not shutil.which(self.binary):
            raise DomainError("MODEL_UNAVAILABLE", "Codex app-server executable is unavailable")

    def generate(self, request):
        self.check_available()
        if not request.attempt_id:
            raise ModelCallFailure("CALL_IDENTITY_REQUIRED")
        with tempfile.TemporaryDirectory(prefix="nf-host-") as tmp, _TextBridge(self.backend, request, self.journal.authorize_send) as bridge:
            server = Thread(target=bridge.serve, daemon=True)
            server.start()
            try:
                thread_id, turn_id, text = asyncio.run(self._turn(request, tmp, bridge))
                if bridge.answer is None or text != bridge.answer.text:
                    raise ModelCallFailure("HOST_BINDING_MISMATCH")
                self.journal.record(request.attempt_id, thread_id, turn_id, "COMPLETED")
                return replace(bridge.answer, raw_response={**bridge.answer.raw_response,
                    "codex_thread_id": thread_id, "codex_turn_id": turn_id})
            finally:
                bridge.stopped.set()
                # An already-sent upstream request may still be finishing.
                # Keep its ledger fence and release the host at its deadline.
                server.join(timeout=1)

    async def _turn(self, request, tmp, bridge):
        home, cwd = Path(tmp, "codex"), Path(tmp, "work")
        home.mkdir(mode=0o700)
        cwd.mkdir(mode=0o700)
        endpoint = f"http://127.0.0.1:{bridge.server_port}{bridge.route}/v1"
        settings = {"model_provider": "nf_text", "model": "nf-text", "project_doc_max_bytes": 0,
                    "model_providers.nf_text.name": "NovelFactory",
                    "model_providers.nf_text.base_url": endpoint,
                    "model_providers.nf_text.wire_api": "responses",
                    "model_providers.nf_text.requires_openai_auth": False,
                    "model_providers.nf_text.supports_websockets": False,
                    "model_providers.nf_text.stream_max_retries": 0, "model_providers.nf_text.request_max_retries": 0}
        args = [shutil.which(self.binary), "app-server", "--listen", "stdio://"]
        for key, value in settings.items():
            args.extend(["-c", key + "=" + json.dumps(value)])
        for feature in ("shell_tool", "multi_agent", "apps", "browser_use", "computer_use"):
            args.extend(["--disable", feature])
        env = {"HOME": tmp, "CODEX_HOME": str(home), "PATH": os.defpath, "NO_PROXY": "127.0.0.1"}
        process = await asyncio.create_subprocess_exec(*args, cwd=cwd, env=env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, limit=8_000_000)
        try:
            async with asyncio.timeout(self.timeout_seconds):
                return await self._exchange(process, cwd, request)
        except (TimeoutError, OSError, ValueError, KeyError, asyncio.IncompleteReadError):
            raise ModelCallFailure("CODEX_TRANSPORT_UNKNOWN") from None
        finally:
            if process.returncode is None:
                process.kill()
            await process.wait()

    async def _exchange(self, proc, cwd, request):
        async def send(method, params, rpc_id=None):
            packet = {"method": method, "params": params}
            if rpc_id is not None:
                packet["id"] = rpc_id
            proc.stdin.write((json.dumps(packet) + "\n").encode())
            await proc.stdin.drain()

        async def read():
            raw = await proc.stdout.readline()
            if not raw:
                raise ModelCallFailure("CODEX_TRANSPORT_UNKNOWN")
            value = json.loads(raw)
            if "id" in value and "method" in value:
                raise ModelCallFailure("UNEXPECTED_HOST_TOOL_REQUEST")
            return value

        async def response(rpc_id):
            while True:
                value = await read()
                if value.get("id") == rpc_id:
                    if "error" in value:
                        raise ModelCallFailure("CODEX_RPC_ERROR")
                    return value["result"]

        await send("initialize", {"clientInfo": {"name": "novel_factory", "title": "NovelFactory", "version": "2.1"}}, 1)
        await response(1)
        await send("initialized", {})
        await send("thread/start", {"cwd": str(cwd), "model": "nf-text", "ephemeral": True,
                                    "sandbox": "read-only", "approvalPolicy": "never"}, 2)
        thread_id = (await response(2))["thread"]["id"]
        self.journal.record(request.attempt_id, thread_id, None, "THREAD_STARTED")
        # Opaque reference only; the real model sees the authenticated request
        # retained by the bridge. No project credentials or context in Codex.
        await send("turn/start", {"threadId": thread_id,
            "input": [{"type": "text", "text": "Process authorized NovelFactory request " + request.request_sha256}]}, 3)
        turn_id = (await response(3))["turn"]["id"]
        self.journal.record(request.attempt_id, thread_id, turn_id, "TURN_STARTED")
        final_text = None
        while True:
            value = await read()
            data = value.get("params", {})
            if data.get("threadId") != thread_id:
                continue
            if value.get("method") == "item/completed" and data.get("turnId") == turn_id:
                if data["item"]["type"] == "agentMessage":
                    final_text = data["item"]["text"]
            if value.get("method") == "turn/completed" and data["turn"]["id"] == turn_id:
                if data["turn"]["status"] != "completed":
                    raise ModelCallFailure("CODEX_TURN_INCOMPLETE")
                return thread_id, turn_id, final_text


class CodexRoleContextAdapter(CodexAppServerAdapter):
    """For actor tasks, read the exact authorized context via an isolated MCP Host first."""

    def __init__(self, backend, journal, *, relay, context_reader, binary="codex", timeout_seconds=90):
        super().__init__(backend, journal, binary=binary, timeout_seconds=timeout_seconds)
        self.relay, self.context_reader = relay, context_reader

    @property
    def configuration(self):
        return {**super().configuration, "host_protocol": "codex-app-server:actor-mcp-read-text-v1",
                "isolation_evidence": "Isolated actor context read; one independently authorized text request"}

    def read_actor_context(self, principal, task):
        return self.context_reader(self.journal.kernel, principal, task, shutil.which(self.binary), self.relay)
