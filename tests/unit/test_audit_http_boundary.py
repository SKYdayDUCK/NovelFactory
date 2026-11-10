"""ASGI slow-body tests run without a database or a network listener."""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from novel_factory.interfaces.admission import AdmissionController
from novel_factory.interfaces.http import HttpSettings, RequestBoundaryMiddleware


async def run_case(receive, settings):
    called = []
    messages = []

    async def app(scope, rx, tx):
        called.append(True)
        message = await rx()
        assert message["type"] == "http.request"
        await tx({"type": "http.response.start", "status": 200, "headers": []})
        await tx({"type": "http.response.body", "body": b"ok"})

    ingress = AdmissionController(settings.max_inflight_requests,
                                  per_principal=settings.max_inflight_requests, max_waiters=0, timeout=1.0)
    middleware = RequestBoundaryMiddleware(app, settings, ingress)
    scope = {"type": "http", "path": "/slow", "method": "POST",
             "headers": [(b"content-type", b"application/json")], "state": {}}

    async def send(message):
        messages.append(message)

    started = time.monotonic()
    await middleware(scope, receive, send)
    return messages, called, ingress.snapshot(), time.monotonic() - started


def test_slow_incomplete_upload_times_out_and_releases_ingress_slot():
    async def scenario():
        async def receive():
            await asyncio.Event().wait()
        return await run_case(receive, HttpSettings(max_body_bytes=1024,
                                                   body_read_timeout_seconds=0.06))
    messages, called, snapshot, elapsed = asyncio.run(scenario())
    assert messages[0]["status"] == 408
    assert json.loads(messages[1]["body"])["code"] == "REQUEST_TIMEOUT"
    assert not called
    assert snapshot["active"] == 0
    assert elapsed < 1.0


def test_trickle_upload_cannot_extend_absolute_deadline():
    async def scenario():
        async def receive():
            await asyncio.sleep(0.025)
            return {"type": "http.request", "body": b"x", "more_body": True}
        return await run_case(receive, HttpSettings(max_body_bytes=8192,
                                                   body_read_timeout_seconds=0.065))
    messages, called, snapshot, elapsed = asyncio.run(scenario())
    assert messages[0]["status"] == 408
    assert not called and snapshot["active"] == 0 and elapsed < 0.5


def test_body_chunk_flood_and_size_limit_remain_bounded():
    async def scenario():
        count = 0
        async def receive():
            nonlocal count
            count += 1
            return {"type": "http.request", "body": b"x", "more_body": count < 1000}
        return await run_case(receive, HttpSettings(max_body_bytes=1024,
                                                   max_body_chunks=8, body_read_timeout_seconds=1.0))
    messages, called, snapshot, elapsed = asyncio.run(scenario())
    assert messages[0]["status"] == 422
    assert not called and snapshot["active"] == 0
