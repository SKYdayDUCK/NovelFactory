"""The response receipt survives gateway process exit."""
import base64
import hashlib
import json
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Event, Thread
from time import monotonic, sleep
from uuid import uuid4


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
