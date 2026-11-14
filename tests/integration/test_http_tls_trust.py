"""Actual loopback TLS on both HTTP routes; no model or external origin is used."""
import json
import os
import ssl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest
from test_agent_gateway import setup_gateway
from test_p5_process_resume import _https

from agent_runtime import providers
from agent_runtime.protocol import ModelCallFailure, ModelRequest
from novel_factory.application.supervisor import HostJournal
from novel_factory.domain.errors import DomainError
from novel_factory.infrastructure import model_gateway
from novel_factory.infrastructure.model_gateway import OpenAICompatibleAdapter
from novel_factory.infrastructure.runtime_providers import RuntimeModelAdapter

pytestmark = pytest.mark.integration
REQUEST = ModelRequest(({"role": "user", "content": "Synthetic TLS fixture"},), "a" * 64, 100, "tls-fixture")


@pytest.fixture
def tls_backend(tmp_path, monkeypatch):
    calls = []

    class Backend(BaseHTTPRequestHandler):
        def do_POST(self):
            calls.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            body = json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": "synthetic reply"}}],
                               "usage": {"prompt_tokens": 12, "completion_tokens": 3}}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    correct, incorrect = tmp_path / "correct", tmp_path / "incorrect"
    correct.mkdir()
    incorrect.mkdir()
    empty_ca_directory = tmp_path / "empty-trust-directory"
    empty_ca_directory.mkdir()
    monkeypatch.setenv("SSL_CERT_DIR", str(empty_ca_directory))
    with ThreadingHTTPServer(("127.0.0.1", 0), Backend) as server:
        certificate = _https(server, correct)
        with ThreadingHTTPServer(("127.0.0.1", 0), Backend) as other:
            wrong_certificate = _https(other, incorrect)
        worker = Thread(target=server.serve_forever)
        worker.start()
        monkeypatch.setenv("NO_PROXY", "127.0.0.1")
        monkeypatch.setenv("SSL_CERT_FILE", str(certificate))
        try:
            yield f"https://127.0.0.1:{server.server_port}/fixture", calls, certificate, wrong_certificate
        finally:
            server.shutdown()
            worker.join(timeout=5)


def call(route, endpoint):
    if route == "generic":
        return OpenAICompatibleAdapter(endpoint=endpoint, model="tls-fixture", api_key="synthetic", timeout_seconds=3).generate(REQUEST)
    return providers.post(url=endpoint, payload={"synthetic": True}, credential="synthetic",
                          request_hash=REQUEST.request_sha256, timeout=3, limit=4096)


@pytest.mark.parametrize("route", ["generic", "private_worker"])
@pytest.mark.parametrize("trusted", [True, False])
def test_explicit_ca_is_used_and_other_ca_is_rejected(tls_backend, monkeypatch, route, trusted):
    endpoint, requests, certificate, other = tls_backend
    monkeypatch.setenv("SSL_CERT_FILE", str(certificate if trusted else other))
    if trusted:
        assert call(route, endpoint)
        assert len(requests) == 1
    else:
        with pytest.raises(ModelCallFailure) as failure:
            call(route, endpoint)
        assert failure.value.code == "MODEL_TRANSPORT_UNKNOWN" and failure.value.sent
        assert requests == []
    context = model_gateway._tls_context()
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname


@pytest.mark.parametrize("route", ["generic", "private_worker"])
@pytest.mark.parametrize("invalid", ["missing", "malformed", "missing_directory", "directory_is_file"])
def test_invalid_ca_configuration_cannot_reach_send_boundary(tmp_path, monkeypatch, route, invalid):
    certificate = tmp_path / "unusable.pem"
    if invalid in {"malformed", "directory_is_file"}:
        certificate.write_text("Synthetic invalid CA configuration", encoding="ascii")
    if invalid in {"missing_directory", "directory_is_file"}:
        monkeypatch.delenv("SSL_CERT_FILE", raising=False)
        monkeypatch.setenv("SSL_CERT_DIR", str(certificate))
    else:
        monkeypatch.setenv("SSL_CERT_FILE", str(certificate))
    forbidden = lambda *args, **kwargs: pytest.fail("Invalid TLS configuration reached the sending boundary")
    monkeypatch.setattr(model_gateway, "build_opener", forbidden)
    monkeypatch.setattr(providers.subprocess, "run", forbidden)
    with pytest.raises(ModelCallFailure) as failure:
        call(route, "https://127.0.0.1:1/fixture")
    assert failure.value.code == "MODEL_TLS_CONFIGURATION_INVALID" and not failure.value.sent
    if route == "generic":
        with pytest.raises(DomainError) as unavailable:
            OpenAICompatibleAdapter(endpoint="https://127.0.0.1:1/fixture", model="fixture", api_key="synthetic").check_available()
        assert unavailable.value.code == "MODEL_TLS_CONFIGURATION_INVALID"
    else:
        monkeypatch.setenv("NF_RUNTIME_LIVE_APPROVED", "1")
        monkeypatch.setenv("NF_RUNTIME_ALLOWED_MODELS", json.dumps({"openai-compatible": ["fixture"]}))
        monkeypatch.setenv("NF_TEST_TLS", "synthetic")
        profile = providers.Profile("openai-compatible", "fixture", "NF_TEST_TLS", 100, endpoint="https://127.0.0.1:1/fixture")
        with pytest.raises(providers.ProfileError, match="MODEL_TLS_CONFIGURATION_INVALID"):
            providers.invoke(profile, REQUEST, authorize=forbidden, record=forbidden)


@pytest.mark.parametrize("changed", ["file", "directory"])
def test_worker_rechecks_ca_before_open_and_preserves_definite_unsent(tls_backend, monkeypatch, changed):
    endpoint, requests, certificate, _ = tls_backend
    original = providers._check_http_tls

    def changed_after_parent_preflight():
        original()
        if changed == "file":
            certificate.unlink()
        else:
            Path(os.environ["SSL_CERT_DIR"]).rmdir()

    monkeypatch.setattr(providers, "_check_http_tls", changed_after_parent_preflight)
    with pytest.raises(ModelCallFailure) as failure:
        call("private_worker", endpoint)
    assert failure.value.code == "MODEL_TLS_CONFIGURATION_INVALID" and not failure.value.sent
    assert requests == []


@pytest.mark.postgres
@pytest.mark.parametrize("route", ["generic", "private_worker"])
def test_invalid_tls_preflight_never_reserves_a_paid_attempt(kernel_env, tmp_path, monkeypatch, route):
    kernel = kernel_env["kernel"]
    endpoint = "https://127.0.0.1:1/fixture"
    adapter = OpenAICompatibleAdapter(endpoint=endpoint, model="fixture", api_key="synthetic")
    if route == "private_worker":
        monkeypatch.setenv("NF_RUNTIME_LIVE_APPROVED", "1")
        monkeypatch.setenv("NF_RUNTIME_ALLOWED_MODELS", json.dumps({"openai-compatible": ["fixture"]}))
        monkeypatch.setenv("NF_TEST_TLS", "synthetic")
        adapter = RuntimeModelAdapter(providers.Profile("openai-compatible", "fixture", "NF_TEST_TLS", 1024,
                                                       endpoint=endpoint), HostJournal(kernel))
    state = setup_gateway(kernel_env, adapter)
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "missing-ca.pem"))
    forbidden = lambda *args, **kwargs: pytest.fail("Invalid TLS configuration escaped preflight")
    monkeypatch.setattr(model_gateway, "build_opener", forbidden)
    monkeypatch.setattr(providers, "post", forbidden)
    with kernel.db.uow(write=False) as repo:
        budget = repo.one("budgets", {"budget_id": state["request"]["budget_id"]})
    result = state["gateway"].execute(state["principal"], state["request"])
    assert result["status"] == "BLOCKED" and result["cost_status"] == "NOT_SENT"
    assert result["reason"] == "MODEL_TLS_CONFIGURATION_INVALID"
    with kernel.db.uow(write=False) as repo:
        assert repo.many("call_attempts", {"task_id": state["task"]["task_id"]}) == []
        assert repo.one("budgets", {"budget_id": budget["budget_id"]}) == budget
