"""A native app-server turn with a single first-party, text-only Responses request.

The private loopback transport removes CLI tools, injected filesystem context and
retry authority before contacting OpenAI. It relays actual provider SSE events.
Only the parent process holds the operator's credential.
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import tempfile
from dataclasses import replace
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread

from .host_process import ISOLATED_PLUGIN_OPTIONS, start_host, stop_host
from .protocol import ModelCallFailure, ModelResponse
from .providers import ProfileError, _authorized_credential, post, usage_value

OPENAI_RESPONSES = "https://api.openai.com/v1/responses"


def _text_stream(content, request_hash, receipt):
    """Only complete, provider-originated assistant messages enter the CLI."""
    kept = []
    completed = None
    final_event_text = None
    for line in content.splitlines():
        if not line.startswith(b"data:"):
            continue
        data = line[5:].strip()
        if data == b"[DONE]":
            continue
        event = json.loads(data)
        kind = event["type"]
        if kind in ("error", "response.failed", "response.incomplete"):
            raise ValueError
        if kind in ("response.output_item.added", "response.output_item.done"):
            item = event["item"]
            if item["type"] == "reasoning":
                continue
            if item["type"] != "message" or item["role"] != "assistant":
                raise ValueError
            if kind == "response.output_item.done":
                if final_event_text is not None:
                    raise ValueError
                final_event_text = _message(item)
        elif kind == "response.completed":
            if completed is not None:
                raise ValueError
            completed = event["response"]
            if completed["status"] != "completed" or any(i["type"] not in ("message", "reasoning")
                                                         for i in completed["output"]):
                raise ValueError
        elif kind == "response.created":
            if event["response"].get("output"):
                raise ValueError
        elif kind not in ("response.output_text.delta", "response.output_text.done"):
            continue
        kept.append(b"event: " + kind.encode("ascii") + b"\ndata: " + data + b"\n\n")
    if not completed:
        raise ValueError
    messages = [_message(i) for i in completed["output"] if i["type"] == "message"]
    if len(messages) != 1 or messages[0] != final_event_text:
        raise ValueError
    usage = completed.get("usage") or {}
    response = ModelResponse(text=messages[0], provider_receipt=receipt or completed["id"],
        input_tokens=usage_value(usage.get("input_tokens")), output_tokens=usage_value(usage.get("output_tokens")),
        cached_input_tokens=usage_value((usage.get("input_tokens_details") or {}).get("cached_tokens")),
        raw_response={"response_id": completed["id"], "model": completed.get("model")},
        request_sha256=request_hash)
    return b"".join(kept), response


def _message(item):
    if item["role"] != "assistant" or any(x["type"] != "output_text" for x in item["content"]):
        raise ValueError
    value = "".join(x["text"] for x in item["content"])
    if not value.strip():
        raise ValueError
    return value


class _Transport(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        owner = self.server
        if self.path != owner.route + "/v1/responses":
            self.send_error(404)
            return
        if owner.used:
            self.send_error(409)
            return
        self.connection.settimeout(5)
        try:
            size = int(self.headers["Content-Length"])
            if not 0 < size <= 4_000_000:
                raise ValueError
            native = json.loads(self.rfile.read(size))
            # The CLI may also include implicit environment and developer messages.
            # Select the exact frozen user turn produced by this CLI instance.
            matching = [part["text"] for item in native["input"] if item.get("role") == "user"
                        for part in item.get("content", []) if isinstance(part, dict)
                        and part.get("text") == owner.turn_input]
            if native["model"] != owner.profile.model or len(matching) != 1:
                raise ValueError
            authorized_messages = json.loads(matching[0])["messages"]
            owner.used = True
            profile, request = owner.profile, owner.request
            key = _authorized_credential(profile, request, owner.authorize)
            payload = {"model": profile.model, "input": [
                {"role": "developer" if m["role"] == "system" else m["role"], "content": m["content"]}
                for m in authorized_messages], "stream": True, "store": False, "tools": []}
            if profile.auth_mode == "api-key":
                payload["max_output_tokens"] = request.max_output_tokens
            started_at = datetime.now(UTC).isoformat()
            content, receipt = post(url=OPENAI_RESPONSES, payload=payload, credential=key,
                request_hash=request.request_sha256, timeout=profile.timeout_seconds, limit=8_000_000,
                headers={"User-Agent": self.headers.get("User-Agent", "novel_factory"),
                         "originator": "novel_factory", "Accept": "text/event-stream"})
            received_at = datetime.now(UTC).isoformat()
            stream, answer = _text_stream(content, request.request_sha256, receipt)
            owner.answer = replace(answer, raw_response={**answer.raw_response,
                "transport_window": {"started_at": started_at, "received_at": received_at}})
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(stream)))
            self.end_headers()
            self.wfile.write(stream)
        except ModelCallFailure as exc:
            owner.failure = exc
            self.send_error(502)
        except Exception:  # noqa: BLE001 - a journal may raise a secret-bearing exception
            # No provider text, secrets or user content in host diagnostics.
            owner.failure = ModelCallFailure("CODEX_PROTOCOL_ERROR")
            self.send_error(502)


def generate(profile, request, *, authorize, record):
    profile.preflight()
    if profile.auth_mode == "chatgpt-plan" and request.max_output_tokens < profile.model_output_ceiling:
        raise ProfileError("MODEL_OUTPUT_CEILING_REQUIRED")
    with tempfile.TemporaryDirectory(prefix="nf-native-") as tmp, HTTPServer(("127.0.0.1", 0), _Transport) as relay:
        relay.profile, relay.request, relay.authorize = profile, request, authorize
        relay.route = "/" + secrets.token_hex(32)
        relay.turn_input = json.dumps({"request_sha256": request.request_sha256, "messages": list(request.messages)},
                                      ensure_ascii=False, separators=(",", ":"))
        relay.used, relay.answer, relay.failure = False, None, None
        server = Thread(target=relay.serve_forever, kwargs={"poll_interval": .05}, daemon=True)
        server.start()
        try:
            try:
                thread_id, turn_id, answer = asyncio.run(_turn(tmp, relay, record))
            except ModelCallFailure:
                if relay.failure is not None:
                    raise relay.failure
                raise
            if relay.answer is None or answer != relay.answer.text:
                raise ModelCallFailure("HOST_BINDING_MISMATCH")
            return replace(relay.answer, raw_response={**relay.answer.raw_response,
                    "codex_thread_id": thread_id, "codex_turn_id": turn_id, "auth_mode": profile.auth_mode})
        finally:
            relay.shutdown()
            server.join()


async def _turn(tmp, relay, record):
    home, cwd = Path(tmp, "codex"), Path(tmp, "work")
    home.mkdir(mode=0o700)
    cwd.mkdir(mode=0o700)
    origin = f"http://127.0.0.1:{relay.server_port}{relay.route}/v1"
    settings = {"model_provider": "nf_openai", "model": relay.profile.model, "project_doc_max_bytes": 0,
                "web_search": "disabled", "model_providers.nf_openai.name": "OpenAI guarded Responses",
                "model_providers.nf_openai.base_url": origin, "model_providers.nf_openai.wire_api": "responses",
                "model_providers.nf_openai.requires_openai_auth": False, "model_providers.nf_openai.supports_websockets": False,
                "model_providers.nf_openai.request_max_retries": 0, "model_providers.nf_openai.stream_max_retries": 0}
    args = [shutil.which("codex"), "app-server", "--listen", "stdio://", *ISOLATED_PLUGIN_OPTIONS]
    for key, value in settings.items():
        args.extend(["-c", key + "=" + json.dumps(value)])
    for feature in ("shell_tool", "multi_agent", "apps", "browser_use", "computer_use", "view_image", "hooks", "image_generation"):
        args.extend(["--disable", feature])
    env = {"HOME": tmp, "CODEX_HOME": str(home), "PATH": os.defpath, "NO_PROXY": "127.0.0.1"}
    try:
        proc = await start_host(*args, cwd=cwd, env=env, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, limit=4_000_000)
    except OSError:
        raise ModelCallFailure("CODEX_HOST_START_FAILED", sent=False) from None
    try:
        async with asyncio.timeout(relay.profile.timeout_seconds):
            return await _exchange(proc, cwd, relay, record)
    except (TimeoutError, OSError, ValueError, KeyError, TypeError):
        raise ModelCallFailure("CODEX_TRANSPORT_UNKNOWN", sent=relay.used) from None
    finally:
        await stop_host(proc)


async def _exchange(proc, cwd, relay, record):
    async def read():
        raw = await proc.stdout.readline()
        if not raw:
            raise ModelCallFailure("CODEX_HOST_EXITED", sent=relay.used)
        event = json.loads(raw)
        if "id" in event and "method" in event:
            raise ModelCallFailure("UNEXPECTED_HOST_TOOL_REQUEST", sent=relay.used)
        return event

    async def send(method, params, identifier=None):
        data = {"method": method, "params": params}
        if identifier is not None:
            data["id"] = identifier
        proc.stdin.write((json.dumps(data) + "\n").encode())
        await proc.stdin.drain()
        if identifier is not None:
            while True:
                event = await read()
                if event.get("id") == identifier:
                    if "error" in event:
                        raise ModelCallFailure("CODEX_RPC_ERROR", sent=relay.used)
                    return event["result"]

    await send("initialize", {"clientInfo": {"name": "novel_factory", "title": "NovelFactory", "version": "2.1"}}, 1)
    await send("initialized", {})
    data = await send("thread/start", {"cwd": str(cwd), "model": relay.profile.model, "ephemeral": True,
                                      "sandbox": "read-only", "approvalPolicy": "never"}, 2)
    thread_id = data["thread"]["id"]
    attempt = relay.request.attempt_id
    record(attempt, thread_id, None, "THREAD_STARTED")
    data = await send("turn/start", {"threadId": thread_id, "input": [{"type": "text", "text": relay.turn_input}]}, 3)
    turn_id = data["turn"]["id"]
    record(attempt, thread_id, turn_id, "TURN_STARTED")
    final_text = None
    while True:
        event = await read()
        params = event.get("params", {})
        if params.get("threadId") != thread_id:
            continue
        if (event.get("method") == "item/completed" and params.get("turnId") == turn_id
                and params["item"]["type"] == "agentMessage"):
            final_text = params["item"]["text"]
        if event.get("method") == "turn/completed" and params["turn"]["id"] == turn_id:
            if params["turn"]["status"] != "completed":
                raise ModelCallFailure("CODEX_TURN_INCOMPLETE", sent=relay.used)
            record(attempt, thread_id, turn_id, "COMPLETED")
            return thread_id, turn_id, final_text
