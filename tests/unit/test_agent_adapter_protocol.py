"""P2-03: runtime isolation, transport shape and bounded dispatch (no paid calls)."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from novel_factory.application.agent_gateway import BoundedAgentWorker, _inline_schema, _unique_pairs
from novel_factory.contracts.registry import validate_contract
from novel_factory.domain.errors import DomainError
from novel_factory.infrastructure.model_gateway import (
    ModelCallFailure, ModelRequest, ModelResponse, OpenAICompatibleAdapter, TrustedHostAdapter,
)
from novel_factory.infrastructure.runtime_catalog import RuntimeCatalog

pytestmark = pytest.mark.unit


def test_runtime_loads_only_one_allowed_skill_and_no_tools():
    selected = RuntimeCatalog().snapshot("character_actor", "character_action")
    value = selected["snapshot"]
    assert value["skill_name"] == "simulate-character"
    assert value["effective_model_tools"] == []
    assert len(selected["sha256"]) == 64
    with pytest.raises(DomainError, match="outside this runtime role"):
        RuntimeCatalog().snapshot("character_actor", "character_action", "edit-volume")
    with pytest.raises(DomainError) as error:
        RuntimeCatalog().snapshot("character_actor", "release_review")
    assert error.value.code == "ROLE_MODE_DENIED"


def test_runtime_resource_cannot_escape_catalog_root(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("must not be loaded")
    root = tmp_path / "runtime"
    root.mkdir()
    with pytest.raises(DomainError) as error:
        RuntimeCatalog(root)._read("../outside.txt")
    assert error.value.code == "RUNTIME_UNAVAILABLE"


def test_output_schema_is_exact_and_dispatch_rejects_extra_authority():
    schema = _inline_schema("ActionProposal", "2.0")
    assert "actor_id" in schema["required"]
    assert "$ref" not in json.dumps(schema)
    with pytest.raises(DomainError):
        validate_contract("AgentTextOutput", {"text": "x", "event_refs": [],
            "important_deltas": [], "evidence_refs": [], "accept_world": True}, "agent")
    with pytest.raises(ValueError):
        json.loads('{"actor_id":"one","actor_id":"two"}', object_pairs_hook=_unique_pairs)


def test_missing_model_configuration_never_sends_or_invents_credentials(monkeypatch):
    monkeypatch.delenv("NF_MODEL_ENDPOINT", raising=False)
    monkeypatch.delenv("NF_MODEL_NAME", raising=False)
    with pytest.raises(DomainError) as error:
        OpenAICompatibleAdapter.from_environment()
    assert error.value.code == "MODEL_UNAVAILABLE"
    adapter = OpenAICompatibleAdapter(endpoint="https://example.test/v1/chat/completions",
        model="explicit-test-model", api_key=None)
    with pytest.raises(DomainError) as error:
        adapter.check_available()
    assert error.value.code == "MODEL_UNAVAILABLE"
    assert "api_key" not in json.dumps(adapter.configuration)


def test_unsafe_endpoint_and_implicit_credential_urls_rejected():
    for endpoint in ("http://example.test/v1/chat/completions", "https://secret@example.test/api", "https://example.test/api?key=secret"):
        with pytest.raises(DomainError) as error:
            OpenAICompatibleAdapter(endpoint=endpoint, model="m", api_key="fixture")
        assert error.value.code == "INVALID_MODEL_CONFIG"


def test_real_http_transport_shape_is_stateless_text_only():
    # A loopback protocol fixture, explicitly not a real model evaluation.
    observed = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            observed.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            body = json.dumps({"id": "fixture-receipt", "choices": [{"finish_reason": "stop", "message": {"content": '{"ok":true}'}}],
                               "usage": {"prompt_tokens": 12, "completion_tokens": 3}}).encode()
            self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = Thread(target=server.serve_forever, daemon=True); worker.start()
    try:
        adapter = OpenAICompatibleAdapter(endpoint=f"http://127.0.0.1:{server.server_port}/v1/chat/completions",
            model="protocol-fixture", api_key="not-a-real-secret", allow_loopback_http=True)
        result = adapter.generate(ModelRequest(({"role": "user", "content": "fixture"},), "a" * 64, 20))
        assert result.text == '{"ok":true}' and result.input_tokens == 12 and result.output_tokens == 3
        assert result.actual_amount is None and result.request_sha256 == "a" * 64
        assert len(observed) == 1
        assert set(observed[0]) == {"model", "messages", "max_completion_tokens", "response_format", "stream"}
        assert "tools" not in observed[0] and "conversation_id" not in observed[0]
    finally:
        server.shutdown(); server.server_close(); worker.join()


def test_host_default_is_not_claimed_isolated_and_reply_binding_is_required():
    host = TrustedHostAdapter(lambda request: ModelResponse(text="{}", request_sha256="b" * 64),
                              host="test-host", model="explicit-test-model")
    assert host.configuration["isolation"] == "UNVERIFIED_HOST"
    with pytest.raises(ModelCallFailure) as error:
        host.generate(ModelRequest((), "a" * 64, 20))
    assert error.value.code == "HOST_BINDING_MISMATCH"


def test_worker_rejects_overflow_before_dispatching_any_task():
    class Gateway:
        def execute(self, principal, request):
            raise AssertionError("Overflow must be detected before submitting")
    worker = BoundedAgentWorker(Gateway(), max_workers=1, max_pending=2)
    with pytest.raises(DomainError) as error:
        worker.run([(None, {})] * 3)
    assert error.value.code == "CAPACITY_LIMIT"
