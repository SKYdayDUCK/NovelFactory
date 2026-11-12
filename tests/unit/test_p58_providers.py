"""AR-01/05 protocol fixtures. No model network or accounting authority."""
import json
import shutil
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Barrier, Thread

import pytest

from agent_runtime import codex, providers
from agent_runtime.protocol import ModelCallFailure, ModelRequest
from agent_runtime.providers import Profile, ProfileError, invoke, read_profiles

pytestmark = pytest.mark.unit
REQUEST = ModelRequest(({"role": "system", "content": "Return JSON"},
                        {"role": "user", "content": "Only the frozen character context"}), "a" * 64, 300, "attempt-1")


def plan(**updates):
    return Profile(**{"kind": "codex-cli", "model": "protocol-fixture", "secret_ref": "NF_TEST_CODEX",
                      "max_output_tokens": 300, "auth_mode": "api-key", "timeout_seconds": 12, **updates})


def deepseek(**updates):
    return Profile(**{"kind": "deepseek", "model": "ds-fixture", "secret_ref": "NF_TEST_DS",
                      "max_output_tokens": 300, **updates})


def chat_reply(text='{"candidate":"fiction"}', finish="stop"):
    return json.dumps({"id": "synthetic-chat", "choices": [{"finish_reason": finish, "message": {"content": text}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20, "prompt_cache_hit_tokens": 7}}).encode()


def codex_reply():
    item = {"type": "message", "id": "msg_fixture", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": '{"candidate":"fiction"}', "annotations": []}]}
    response = {"id": "resp_fixture", "model": "protocol-fixture", "object": "response", "output": [], "status": "in_progress"}
    events = [
        {"type": "response.created", "response": response},
        {"type": "response.output_item.added", "output_index": 0, "item": {**item, "status": "in_progress", "content": []}},
        {"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "item_id": item["id"],
         "delta": item["content"][0]["text"]},
        {"type": "response.output_item.done", "output_index": 0, "item": item},
        {"type": "response.completed", "response": {**response, "status": "completed", "output": [item],
                "usage": {"input_tokens": 17, "output_tokens": 11, "total_tokens": 28, "input_tokens_details": {"cached_tokens": 3}}}},
    ]
    return b"".join(("event: " + e["type"] + "\ndata: " + json.dumps({**e, "sequence_number": n}) + "\n\n").encode() for n, e in enumerate(events))


def enable_synthetic_credentials(monkeypatch):
    monkeypatch.setenv("NF_RUNTIME_LIVE_APPROVED", "1")
    monkeypatch.setenv("NF_TEST_DS", "synthetic-deepseek")
    monkeypatch.setenv("NF_TEST_CODEX", "synthetic-codex")


def test_catalog_preview_no_credentials_and_alias_quota_binding(tmp_path, monkeypatch):
    monkeypatch.delenv("NF_RUNTIME_LIVE_APPROVED", raising=False)
    p = tmp_path / "profiles.json"
    first = {"kind": "deepseek", "model": "fixture", "secret_ref": "NF_TEST_DS", "max_output_tokens": 300}
    p.write_text(json.dumps({"profiles": {"role-one": first, "role-two": {**first, "model": "other"}}}))
    one, two = read_profiles(p).values()
    assert one.account_key == two.account_key and one.provider_key == "https://api.deepseek.com"
    assert "NF_TEST_DS" not in json.dumps(one.describe())
    with pytest.raises(ProfileError, match="MODEL_CALL_NOT_AUTHORIZED"):
        invoke(one, REQUEST, authorize=lambda _: pytest.fail("unapproved send"), record=None)
    p.write_text('{"profiles":{"same":{}, "same":{}}}')
    with pytest.raises(ProfileError, match="INVALID_MODEL_CONFIG"):
        read_profiles(p)


def test_plan_requires_hard_model_bound_and_trusted_origin():
    with pytest.raises(ProfileError, match="MODEL_OUTPUT_CEILING_REQUIRED"):
        plan(auth_mode="chatgpt-plan")
    assert plan(auth_mode="chatgpt-plan", model_output_ceiling=300, oauth_client_id="oaiapp_test").model_output_ceiling == 300
    with pytest.raises(ProfileError):
        plan(endpoint="https://other.invalid")
    with pytest.raises(ProfileError):
        deepseek(endpoint="https://other.invalid/chat/completions")


@pytest.mark.parametrize("kind,token_field", [("deepseek", "max_tokens"), ("openai-compatible", "max_completion_tokens")])
def test_vendor_wire_fields_and_receipt(monkeypatch, kind, token_field):
    enable_synthetic_credentials(monkeypatch)
    observed = []
    def fixture(**kwargs):
        observed.append(kwargs)
        return chat_reply(), None, {}
    monkeypatch.setattr(providers, "post", fixture)
    value = invoke(deepseek(kind=kind, endpoint="https://api.deepseek.com/chat/completions"),
                   REQUEST, authorize=lambda attempt: attempt == REQUEST.attempt_id, record=None)
    sent, = observed
    assert sent["payload"] == {"model": "ds-fixture", "messages": list(REQUEST.messages), token_field: 300,
                               "response_format": {"type": "json_object"}, "stream": False}
    assert (value.input_tokens, value.output_tokens, value.actual_amount) == (100, 20, None)
    assert value.cached_input_tokens == (7 if kind == "deepseek" else None)


@pytest.mark.parametrize("body", [chat_reply("  "), chat_reply(finish="length"), b"{}"])
def test_incomplete_deepseek_never_retries(monkeypatch, body):
    enable_synthetic_credentials(monkeypatch)
    calls = []
    def response(**kwargs):
        calls.append(1)
        return body, "synthetic", {}
    monkeypatch.setattr(providers, "post", response)
    with pytest.raises(ModelCallFailure, match="MODEL_PROTOCOL_ERROR"):
        invoke(deepseek(), REQUEST, authorize=lambda _: True, record=None)
    assert len(calls) == 1


def test_http_rate_limit_and_redirect_never_retry_or_leak_body():
    paths = []
    class Throttled(BaseHTTPRequestHandler):
        def do_POST(self):
            paths.append(self.path)
            self.send_response(302 if self.path == "/redirect" else 429)
            self.send_header("Location", "/secret-destination")
            self.end_headers()
            self.wfile.write(b"private body")
        def log_message(self, *args):
            pass
    with ThreadingHTTPServer(("127.0.0.1", 0), Throttled) as server:
        worker = Thread(target=server.serve_forever)
        worker.start()
        try:
            for path, code in (("/limit", "MODEL_RATE_LIMITED"), ("/redirect", "MODEL_HTTP_ERROR")):
                with pytest.raises(ModelCallFailure) as failure:
                    providers.post(url=f"http://127.0.0.1:{server.server_port}{path}", credential="fixture",
                                   payload={}, request_hash="a" * 64, timeout=2, limit=1024)
                assert str(failure.value) == code
            assert paths == ["/limit", "/redirect"]
        finally:
            server.shutdown()
            worker.join()


@pytest.mark.skipif(not shutil.which("codex"), reason="Real app-server binary unavailable")
def test_real_cli_and_deepseek_wire_overlap_with_no_provider_egress(monkeypatch):
    enable_synthetic_credentials(monkeypatch)
    barrier, sends, identities = Barrier(2), [], []
    def fake_wire(**kwargs):
        payload = kwargs["payload"]
        is_native = kwargs["url"] == codex.OPENAI_RESPONSES
        assert kwargs["url"] in (codex.OPENAI_RESPONSES, "https://api.deepseek.com/chat/completions")
        assert "secret_ref" not in json.dumps(payload) and "synthetic-" not in json.dumps(payload)
        if is_native:
            assert payload["input"] == [{"role": "developer", "content": "Return JSON"}, REQUEST.messages[1]]
            assert payload["tools"] == [] and payload["max_output_tokens"] == 300
        else:
            assert payload["messages"] == list(REQUEST.messages)
            assert payload["max_tokens"] == 300 and "tools" not in payload
        sends.append((is_native, kwargs["request_hash"]))
        started_at = datetime.now(UTC).isoformat()
        barrier.wait(timeout=9)
        window = {"started_at": started_at, "received_at": datetime.now(UTC).isoformat()}
        return (codex_reply(), "synthetic-openai", window) if is_native else (chat_reply(), "synthetic-deepseek", window)
    monkeypatch.setattr(codex, "post", fake_wire)
    monkeypatch.setattr(providers, "post", fake_wire)
    def run(profile):
        return invoke(profile, REQUEST, authorize=lambda attempt: attempt == REQUEST.attempt_id,
                      record=lambda *parts: identities.append(parts))
    with ThreadPoolExecutor(2) as pool:
        c, d = pool.map(run, [plan(), deepseek()])
    assert sorted(sends) == [(False, REQUEST.request_sha256), (True, REQUEST.request_sha256)]
    assert c.text == d.text == '{"candidate":"fiction"}'
    assert c.actual_amount is None and d.actual_amount is None
    assert (c.input_tokens, c.output_tokens, c.cached_input_tokens) == (17, 11, 3)
    a, b = c.raw_response["transport_window"], d.raw_response["transport_window"]
    assert max(a["started_at"], b["started_at"]) < min(a["received_at"], b["received_at"])
    assert [i[-1] for i in identities] == ["THREAD_STARTED", "TURN_STARTED", "COMPLETED"]
    assert c.raw_response["codex_turn_id"] and c.raw_response["codex_thread_id"]


def test_native_crash_before_initialization(monkeypatch):
    enable_synthetic_credentials(monkeypatch)
    monkeypatch.setattr(shutil, "which", lambda _: "/bin/false")
    monkeypatch.setattr(codex, "post", lambda **kw: pytest.fail("Crashed host must not send"))
    with pytest.raises(ModelCallFailure) as error:
        invoke(plan(), REQUEST, authorize=lambda _: True, record=lambda *args: None)
    assert not error.value.sent


def test_completed_tool_item_is_never_delivered_to_cli():
    data = codex_reply().replace(b'"type": "message"', b'"type": "function_call"')
    with pytest.raises(ValueError):
        codex._text_stream(data, REQUEST.request_sha256, None)


def test_transport_deadline_kills_trickling_response_and_never_retries():
    from threading import Event
    from time import monotonic

    stop, paths = Event(), []

    class Trickles(BaseHTTPRequestHandler):
        def do_POST(self):
            paths.append(self.path)
            self.send_response(200)
            self.send_header("Content-Length", "999")
            self.end_headers()
            while not stop.wait(.025):
                try:
                    self.wfile.write(b"x")
                    self.wfile.flush()
                except OSError:
                    break

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Trickles) as server:
        worker = Thread(target=server.serve_forever)
        worker.start()
        try:
            started = monotonic()
            with pytest.raises(ModelCallFailure, match="MODEL_TRANSPORT_UNKNOWN") as caught:
                providers.post(url=f"http://127.0.0.1:{server.server_port}/fixture", credential="fixture",
                               payload={}, request_hash="b" * 64, timeout=1, limit=1024)
            assert monotonic() - started < 4 and caught.value.sent
            assert paths == ["/fixture"]
        finally:
            stop.set()
            server.shutdown()
            worker.join()


def test_subscription_source_requires_signed_access_scope_and_client(monkeypatch):
    from time import time
    from types import SimpleNamespace

    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa

    from agent_runtime import plan_auth

    profile = plan(auth_mode="chatgpt-plan", model_output_ceiling=300, oauth_client_id="oaiapp_fixture")
    enable_synthetic_credentials(monkeypatch)
    with pytest.raises(ProfileError, match="MODEL_PLAN_AUTH_UNVERIFIED"):
        profile.preflight()  # Arbitrary secret under a subscription profile.
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = SimpleNamespace(key=private.public_key(), algorithm_name="RS256")
    monkeypatch.setattr(plan_auth, "_issuer_keys", lambda: SimpleNamespace(get_signing_key_from_jwt=lambda _: public))
    now = int(time())
    claims = {"iss": plan_auth.ISSUER, "aud": plan_auth.AUDIENCE, "exp": now+3600, "nbf": now,
              "iat": now, "sub": "fixture", "client_id": "oaiapp_fixture", "scope": "resource.invoke chatgpt.tokens.use.direct"}
    def sign(fields, key=private):
        return jwt.encode(fields, key, algorithm="RS256", headers={"kid": "fixture"})

    good = sign(claims)
    monkeypatch.setenv("NF_TEST_CODEX", good)
    assert profile.preflight() == good
    impostor = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    for invalid in (sign({**claims, "aud": "other"}), sign({**claims, "iss": "other"}),
                    sign({**claims, "client_id": "other"}), sign({**claims, "scope": "resource.invoke"}),
                    sign({**claims, "exp": now-1}), sign(claims, impostor)):
        monkeypatch.setenv("NF_TEST_CODEX", invalid)
        with pytest.raises(ProfileError, match="MODEL_PLAN_AUTH_UNVERIFIED"):
            profile.preflight()


def test_stalled_loopback_headers_do_not_hold_shutdown_or_start_late_request():
    import socket
    from time import monotonic

    with codex._OneTurnServer(5) as relay:
        worker = Thread(target=relay.serve, daemon=True)
        worker.start()
        with socket.create_connection(relay.server_address, timeout=2) as client:
            client.sendall(b"POST /incomplete")
            started = monotonic()
            relay.stop()
            worker.join(timeout=1)
            assert not worker.is_alive() and monotonic()-started < 1 and not relay.forwarded
            with pytest.raises(ModelCallFailure, match="CODEX_SEND_FENCED"):
                relay.begin_forward()
