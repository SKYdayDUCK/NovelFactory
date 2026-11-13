"""Immutable administrator profile snapshots and bounded, stateless HTTP invocations."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .host_process import resolve_host_binary
from .protocol import ModelCallFailure, ModelResponse


class ProfileError(ValueError):
    """Stable error codes only. File contents, endpoints and secrets are not diagnostics."""


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProfileError("INVALID_MODEL_CONFIG")
        result[key] = value
    return result


@dataclass(frozen=True)
class Profile:
    kind: str
    model: str
    secret_ref: str
    max_output_tokens: int
    timeout_seconds: int = 60
    endpoint: str | None = None
    auth_mode: str | None = None
    model_output_ceiling: int | None = None
    oauth_client_id: str | None = None

    def __post_init__(self):
        if (self.kind not in ("codex-cli", "deepseek", "openai-compatible")
                or not isinstance(self.model, str) or not re.fullmatch(r"[a-zA-Z0-9][\w./:@-]{0,191}", self.model)
                or not isinstance(self.secret_ref, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", self.secret_ref)
                or type(self.max_output_tokens) is not int or not 1 <= self.max_output_tokens <= 1_000_000
                or type(self.timeout_seconds) is not int or not 1 <= self.timeout_seconds <= 600):
            raise ProfileError("INVALID_MODEL_CONFIG")
        if self.kind == "codex-cli":
            if self.endpoint is not None or self.auth_mode not in ("chatgpt-plan", "api-key"):
                raise ProfileError("INVALID_MODEL_CONFIG")
            # Sign in with ChatGPT rejects max_output_tokens. This mode therefore
            # requires reserving the whole documented ceiling of the selected model.
            if self.auth_mode == "chatgpt-plan":
                if (type(self.model_output_ceiling) is not int
                        or self.model_output_ceiling != self.max_output_tokens):
                    raise ProfileError("MODEL_OUTPUT_CEILING_REQUIRED")
                if (not isinstance(self.oauth_client_id, str)
                        or not re.fullmatch(r"oaiapp_[A-Za-z0-9_-]{1,120}", self.oauth_client_id)):
                    raise ProfileError("MODEL_PLAN_CLIENT_REQUIRED")
            elif self.model_output_ceiling is not None or self.oauth_client_id is not None:
                raise ProfileError("INVALID_MODEL_CONFIG")
        else:
            if self.auth_mode is not None or self.model_output_ceiling is not None or self.oauth_client_id is not None:
                raise ProfileError("INVALID_MODEL_CONFIG")
            if self.endpoint is None and self.kind == "deepseek":
                object.__setattr__(self, "endpoint", "https://api.deepseek.com/chat/completions")
            if not isinstance(self.endpoint, str):
                raise ProfileError("INVALID_MODEL_CONFIG")
            try:
                url = urlsplit(self.endpoint)
                port = url.port
            except ValueError:
                raise ProfileError("INVALID_MODEL_CONFIG") from None
            if (url.scheme != "https" or not url.hostname or url.username is not None or url.password is not None
                    or url.query or url.fragment or not url.path.strip("/") or any(c.isspace() for c in self.endpoint)):
                raise ProfileError("INVALID_MODEL_CONFIG")
            if self.kind == "deepseek" and (url.hostname != "api.deepseek.com" or port not in (None, 443)
                    or url.path not in ("/chat/completions", "/v1/chat/completions")):
                raise ProfileError("INVALID_MODEL_CONFIG")

    @property
    def provider_key(self):
        if self.kind == "codex-cli":
            return "codex-cli"
        url = urlsplit(self.endpoint)
        host = f"[{url.hostname}]" if ":" in url.hostname else url.hostname
        return f"https://{host}" + (f":{url.port}" if url.port not in (None, 443) else "")

    @property
    def account_key(self):
        return hashlib.sha256((self.provider_key + "\0" + self.secret_ref).encode()).hexdigest()

    def preflight(self):
        if os.environ.get("NF_RUNTIME_LIVE_APPROVED") != "1":
            raise ProfileError("MODEL_CALL_NOT_AUTHORIZED")
        from .credentials import CredentialError, resolve
        try:
            credential = resolve(self.secret_ref)
        except (CredentialError, OSError, UnicodeError):
            raise ProfileError("CREDENTIAL_STORE_UNAVAILABLE") from None
        if not credential or (self.kind == "codex-cli" and not resolve_host_binary()):
            raise ProfileError("MODEL_UNAVAILABLE")

        if self.auth_mode == "chatgpt-plan":
            from .plan_auth import verify
            verify(credential, self.oauth_client_id)
        return credential

    def describe(self):
        return {"adapter_kind": self.kind, "provider": self.provider_key, "model": self.model,
                    "endpoint": self.endpoint, "account_key": self.account_key, "auth_mode": self.auth_mode,
                    "max_output_tokens": self.max_output_tokens, "timeout_seconds": self.timeout_seconds,
                    **({"oauth_client_id": self.oauth_client_id} if self.oauth_client_id is not None else {}),
                    "isolation": "STATELESS_TEXT_ONLY", "isolation_evidence": "Frozen context; one text-only request per attempt",
                    "execution_kind": "REAL_HOST" if self.kind == "codex-cli" else "REAL_EXTERNAL"}


def read_profiles(path):
    """Reading or previewing a catalog does not inspect secrets or use a model."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=_unique)
        if (type(data) is not dict or set(data) != {"profiles"} or type(data["profiles"]) is not dict
                or not 1 <= len(data["profiles"]) <= 256):
            raise ProfileError("INVALID_MODEL_CONFIG")
        profiles = {}
        for key, fields in data["profiles"].items():
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", key) or not isinstance(fields, dict):
                raise ProfileError("INVALID_MODEL_CONFIG")
            profiles[key] = Profile(**fields)
        return profiles
    except ProfileError:
        raise
    except (OSError, TypeError, ValueError, UnicodeError):
        raise ProfileError("INVALID_MODEL_CONFIG") from None


def post(*, url, payload, credential, request_hash, timeout, limit, headers=None, receipt_target=None):
    """Bound DNS, connection, headers and body to one wall-clock deadline.

    A socket inactivity timeout alone lets a trickling response hold the executor
    after its capacity lease expires. Only private stdio carries this request and
    its credential. subprocess.run kills and waits for the helper on timeout.
    """
    packet = json.dumps({"url": url, "payload": payload, "credential": credential,
        "request_hash": request_hash, "timeout": timeout, "limit": limit, "headers": headers,
        "receipt_target": receipt_target},
        ensure_ascii=False, allow_nan=False).encode()
    allowed = ("PATH", "SYSTEMROOT", "SSL_CERT_FILE", "SSL_CERT_DIR", "https_proxy", "HTTPS_PROXY",
               "http_proxy", "HTTP_PROXY", "no_proxy", "NO_PROXY", "ALL_PROXY", "all_proxy")
    env = {key: os.environ[key] for key in allowed if key in os.environ}
    try:
        proc = subprocess.run([sys.executable, "-I", str(Path(__file__).with_name("_http_worker.py"))],
            input=packet, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=timeout, env=env,
            check=False)
        if proc.returncode:
            raise ModelCallFailure("MODEL_TRANSPORT_UNKNOWN")
        parsed = json.loads(proc.stdout)
        if "failure" in parsed:
            raise ModelCallFailure(parsed["failure"], http_status=parsed.get("status"))
        return base64.b64decode(parsed["content"], validate=True), parsed["receipt"], parsed["transport_window"]
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
        raise ModelCallFailure("MODEL_TRANSPORT_UNKNOWN") from None


def usage_value(value):
    if value is not None and (type(value) is not int or value < 0):
        raise ValueError("invalid usage")
    return value


def invoke(profile, request, *, authorize, record, receipt_target=None):
    """The host rechecks a durable attempt immediately before upstream I/O."""
    if (not request.attempt_id or type(request.max_output_tokens) is not int
            or not 1 <= request.max_output_tokens <= profile.max_output_tokens or not request.messages
            or any(set(m) != {"role", "content"} or m["role"] not in ("system", "user", "assistant", "developer")
                   or not isinstance(m["content"], str) for m in request.messages)):
        raise ModelCallFailure("INVALID_MODEL_REQUEST", sent=False)
    if profile.kind == "codex-cli":
        from .codex import generate
        return generate(profile, request, authorize=authorize, record=record)
    credential = _authorized_credential(profile, request, authorize)
    deepseek = profile.kind == "deepseek"
    limit_field = "max_tokens" if deepseek else "max_completion_tokens"
    content, receipt, window = post(url=profile.endpoint, credential=credential,
            payload={"model": profile.model, "messages": list(request.messages), limit_field: request.max_output_tokens,
                     "response_format": {"type": "json_object"}, "stream": False},
            request_hash=request.request_sha256, timeout=profile.timeout_seconds, limit=2_000_000,
            receipt_target=receipt_target)
    try:
        raw = json.loads(content)
        choice, usage = raw["choices"][0], raw.get("usage") or {}
        text = choice["message"]["content"]
        if (choice.get("finish_reason") != "stop" or choice["message"].get("tool_calls")
                or not isinstance(text, str) or not text.strip()):
            raise ValueError
        cached = usage.get("prompt_cache_hit_tokens") if deepseek else (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
        return ModelResponse(text=text, provider_receipt=receipt or raw.get("id"),
            input_tokens=usage_value(usage.get("prompt_tokens")), output_tokens=usage_value(usage.get("completion_tokens")),
            cached_input_tokens=usage_value(cached), request_sha256=request.request_sha256,
            raw_response={"id": raw.get("id"), "model": raw.get("model"), "finish_reason": "stop",
                          "transport_window": window})
    except (TypeError, ValueError, KeyError, IndexError, AttributeError):
        raise ModelCallFailure("MODEL_PROTOCOL_ERROR") from None


def _authorized_credential(profile, request, authorize):
    credential = profile.preflight()
    if authorize(request.attempt_id) is False:
        raise ModelCallFailure("MODEL_CALL_NOT_AUTHORIZED", sent=False)
    return credential
