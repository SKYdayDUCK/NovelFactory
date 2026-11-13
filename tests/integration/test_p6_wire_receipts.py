"""Loopback transport and PG17 operate solely against synthetic bytes and test grants."""

import base64
import hashlib
import json
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Event, Thread
from time import monotonic, sleep
from uuid import uuid4

import pytest
from test_p5_supervisor import _job, backend

from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.provider_evidence import inspect_wire_receipt
from novel_factory.application.supervisor import RuntimeSupervisor
from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import load_fixture
from novel_factory.infrastructure.wire_receipts import destination

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def test_lost_parent_keeps_private_raw_response(tmp_path):
    started, finish = Event(), Event()
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            calls.append(self.rfile.read(int(self.headers["Content-Length"])))
            started.set()
            assert finish.wait(10)
            self.send_response(200)
            self.send_header("x-request-id", "fixture-receipt")
            self.end_headers()
            self.wfile.write(b"delivered but malformed JSON")

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    dest = tmp_path / "receipt.json"
    attempt, request_hash = str(uuid4()), "a" * 64
    code = """
import sys
from agent_runtime.providers import post
post(url=sys.argv[1], payload={'model':'fixture'}, credential='standalone-test-key', request_hash=sys.argv[3],
     timeout=12, limit=2000000, receipt_target={'path':sys.argv[2], 'attempt_id':sys.argv[4]})
"""
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            code,
            f"http://127.0.0.1:{server.server_port}/",
            str(dest),
            request_hash,
            attempt,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert started.wait(10)
        proc.kill()
        proc.wait(5)
        finish.set()
        deadline = monotonic() + 10
        while not dest.exists() and monotonic() < deadline:
            sleep(0.02)
        blob = dest.read_bytes()
        receipt = json.loads(blob)
        assert len(calls) == 1 and b"standalone-test-key" not in blob
        assert (
            receipt["http_status"],
            receipt["receipt"],
            receipt["attempt_id"],
            receipt["request_sha256"],
        ) == (200, "fixture-receipt", attempt, request_hash)
        body = base64.b64decode(receipt["content"], validate=True)
        assert (
            body == b"delivered but malformed JSON"
            and hashlib.sha256(body).hexdigest() == receipt["body_sha256"]
        )
        if os.name != "nt":
            assert not dest.stat().st_mode & 0o077
    finally:
        finish.set()
        if proc.poll() is None:
            proc.kill()
            proc.wait(5)
        server.shutdown()
        server.server_close()
        thread.join(5)


def test_inspection_is_scoped_and_never_settles_or_dispatches(kernel_env):
    k = kernel_env["kernel"]
    world, other = load_fixture(k), load_fixture(k)
    wr, owner = world["world_ref"], world["principal"]
    scope = (k, owner, wr["work_id"], wr["branch_id"])
    budget = k.tasks.create_budget(wr["work_id"], "1")
    with backend() as (adapter, requests, _):
        gw = AgentGateway(k, {"test": adapter})
        sup = RuntimeSupervisor(k, gw)
        job = _job(k, gw, sup, world, budget, "READY")
        first = sup.run_once(owner, wr["work_id"], wr["branch_id"])
        attempt = first["attempt_id"]
        empty = inspect_wire_receipt(*scope, attempt)
        assert empty["wire_receipt"] is None and empty["artifacts"]["prompt"]
        with pytest.raises(DomainError):
            inspect_wire_receipt(
                k,
                other["principal"],
                other["world_ref"]["work_id"],
                other["world_ref"]["branch_id"],
                attempt,
            )
        raw = b'{"id":"private-fixture","usage":{"prompt_tokens":11,"completion_tokens":7}}'
        envelope = {
            "version": "nf.http-receipt.v1",
            "attempt_id": attempt,
            "request_sha256": empty["request_sha256"],
            "body_sha256": hashlib.sha256(raw).hexdigest(),
            "complete": True,
            "receipt": "private-fixture",
            "content": base64.b64encode(raw).decode(),
            "http_status": 200,
            "transport_window": {},
        }
        path = Path(destination(k.objects, attempt))
        path.write_text(json.dumps(envelope))
        path.chmod(0o600)
        detail = inspect_wire_receipt(*scope, attempt)
        assert detail["wire_receipt"]["reported_usage"] == {
            "prompt_tokens": 11,
            "completion_tokens": 7,
        }
        assert (
            "private-fixture" not in json.dumps(detail) and "content" not in detail["wire_receipt"]
        )
        assert (
            not detail["supplier_invoice_recorded"]
            and detail["ledger_state"] == empty["ledger_state"] == "UNKNOWN"
        )
        with k.db.uow(write=False) as repo:
            assert (
                len(repo.many("call_attempts", {"task_id": job.task["task_id"]}))
                == len(requests)
                == 1
            )
        path.write_text(json.dumps({**envelope, "request_sha256": "0" * 64}))
        with pytest.raises(DomainError) as wrong:
            inspect_wire_receipt(*scope, attempt)
        assert wrong.value.code == "INVALID_EVIDENCE"


def test_response_headers_survive_interrupted_body_without_success_receipt(tmp_path):
    from agent_runtime.providers import post
    from agent_runtime.protocol import ModelCallFailure
    class Interrupted(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.send_header("x-request-id", "fixture-request-id")
            self.send_header("Content-Length", "30")
            self.end_headers()
            self.wfile.write(b"partial")
            self.close_connection = True

    server = HTTPServer(("127.0.0.1", 0), Interrupted)
    thread = Thread(target=server.handle_request, daemon=True)
    thread.start()
    path = tmp_path / "attempt.json"
    try:
        with pytest.raises(ModelCallFailure):
            post(url=f"http://127.0.0.1:{server.server_port}/", payload={"model":"fixture"},
                 credential="test-key", request_hash="c"*64, timeout=5, limit=2000,
                 receipt_target={"path":str(path), "attempt_id":str(uuid4())})
        headers = json.loads(path.with_suffix(".headers.json").read_bytes())
        assert headers["receipt"] == "fixture-request-id" and headers["complete"] is False
        assert headers["content"] == "" and b"test-key" not in path.with_suffix(".headers.json").read_bytes()
        assert not path.exists() or json.loads(path.read_bytes())["complete"] is False
    finally:
        thread.join(timeout=5)
        server.server_close()
