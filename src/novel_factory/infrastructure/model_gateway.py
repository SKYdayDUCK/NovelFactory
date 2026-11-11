"""Bounded, stateless external model adapters. No world or task mutations.

The HTTP route uses a documented Chat Completions-shaped JSON interface. No
SDK retry, conversation identifier, tool, shell or file access is provided to
the model. Credentials are kept only in this adapter and never in provenance.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
import json
import os
from typing import Callable, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from novel_factory.domain.canonical import canonical_bytes
from novel_factory.domain.errors import DomainError


@dataclass(frozen=True)
class ModelRequest:
    messages: tuple[dict, ...]
    request_sha256: str
    max_output_tokens: int
    attempt_id: str | None = None


@dataclass(frozen=True)
class ModelResponse:
    text: str
    provider_receipt: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    actual_amount: Decimal | None = None
    currency: str | None = None
    raw_response: dict = field(default_factory=dict)
    request_sha256: str | None = None
    provider_reported_usd: Decimal | None = None


def _openrouter_charge(endpoint, content, generation_id):
    if (endpoint != "https://openrouter.ai/api/v1/chat/completions"
            or not isinstance(generation_id, str) or not generation_id.startswith("gen-")):
        return None
    # Preserve the provider's JSON number exactly rather than rounding a float
    # into the six-place monetary ledger. Only the explicitly enrolled lookup
    # adapter uses this per-request receipt for automatic settlement.
    exact = (json.loads(content, parse_float=Decimal).get("usage") or {}).get("cost")
    if type(exact) not in (int, Decimal):
        return None
    amount = Decimal(exact)
    if (amount.is_finite() and 0 <= amount < Decimal("1e14")
            and amount == amount.quantize(Decimal("0.000001"))):
        return amount
    return None


class ModelCallFailure(Exception):
    """Only stable diagnostics are exposed; provider error text can be secret."""
    def __init__(self, code: str, *, sent: bool = True, http_status: int | None = None):
        super().__init__(code)
        self.code, self.sent, self.http_status = code, sent, http_status


class ModelAdapter(Protocol):
    @property
    def configuration(self) -> dict: ...
    def check_available(self) -> None: ...
    def generate(self, request: ModelRequest) -> ModelResponse: ...


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward credentials or task content to a redirected origin.
        return None


class OpenAICompatibleAdapter:
    """An explicitly configured endpoint, not a client-supplied arbitrary URL.

    A missing API key is BLOCKED before reserving/sending a provider request.
    The server configuration must supply the model and max output; no model or
    price is guessed. HTTP usage does not constitute a billing receipt, so
    actual_amount remains unknown until an independent reconciliation.
    """
    def __init__(self, *, endpoint: str, model: str, api_key: str | None,
                 max_output_tokens: int = 2048, timeout_seconds: float = 60,
                 allow_loopback_http: bool = False, max_response_bytes: int = 2_000_000):
        url = urlsplit(endpoint)
        loopback = url.hostname in {"127.0.0.1", "localhost", "::1"}
        if (url.scheme != "https" and not (allow_loopback_http and url.scheme == "http" and loopback)) or url.username or url.password or url.query or url.fragment:
            raise DomainError("INVALID_MODEL_CONFIG", "Model endpoint must use an explicit secure origin")
        if not url.hostname or not model or not 1 <= max_output_tokens <= 100_000 or not 0 < timeout_seconds <= 600 or not 1024 <= max_response_bytes <= 10_000_000:
            raise DomainError("INVALID_MODEL_CONFIG", "Model limits are outside the supported range")
        self.endpoint, self.model, self._api_key = endpoint, model, api_key
        self.max_output_tokens, self.timeout_seconds = max_output_tokens, timeout_seconds
        self.max_response_bytes = max_response_bytes

    @classmethod
    def from_environment(cls):
        endpoint = os.environ.get("NF_MODEL_ENDPOINT")
        model = os.environ.get("NF_MODEL_NAME")
        if not endpoint or not model:
            raise DomainError("MODEL_UNAVAILABLE", "An authorized model endpoint and model must be configured")
        return cls(endpoint=endpoint, model=model, api_key=os.environ.get("NF_MODEL_API_KEY"),
                   max_output_tokens=int(os.environ.get("NF_MODEL_MAX_OUTPUT_TOKENS", "2048")),
                   timeout_seconds=float(os.environ.get("NF_MODEL_TIMEOUT_SECONDS", "60")))

    @property
    def configuration(self):
        return {"adapter_kind": "OPENAI_COMPATIBLE_HTTP", "provider": urlsplit(self.endpoint).hostname,
                "endpoint": self.endpoint, "model": self.model,
                "max_output_tokens": self.max_output_tokens, "timeout_seconds": str(self.timeout_seconds),
                "max_response_bytes": self.max_response_bytes,
                "isolation": "STATELESS_TEXT_ONLY", "isolation_evidence": "No tools or conversation state are sent by this adapter",
                "execution_kind": "REAL_EXTERNAL"}

    def check_available(self):
        if not self._api_key:
            raise DomainError("MODEL_UNAVAILABLE", "No authorized model credential is configured")

    def generate(self, request: ModelRequest) -> ModelResponse:
        self.check_available()
        if request.max_output_tokens > self.max_output_tokens:
            raise DomainError("BUDGET_EXCEEDED", "Requested output exceeds the model configuration")
        body = canonical_bytes({"model": self.model, "messages": list(request.messages),
                                "max_completion_tokens": request.max_output_tokens,
                                "response_format": {"type": "json_object"}, "stream": False})
        req = Request(self.endpoint, data=body, method="POST", headers={
            "Authorization": "Bearer " + self._api_key, "Content-Type": "application/json",
            "X-Client-Request-Id": request.request_sha256})
        try:
            # No automatic retry: the persistent call ledger decides whether
            # dispatch is new. A timeout cannot prove that billing did not occur.
            with build_opener(_NoRedirect).open(req, timeout=self.timeout_seconds) as response:
                content = response.read(self.max_response_bytes + 1)
                if len(content) > self.max_response_bytes:
                    raise ModelCallFailure("MODEL_RESPONSE_TOO_LARGE")
                provider_receipt = response.headers.get("x-request-id")
        except HTTPError as exc:
            raise ModelCallFailure("MODEL_HTTP_ERROR", http_status=exc.code) from None
        except (URLError, TimeoutError, OSError):
            raise ModelCallFailure("MODEL_TRANSPORT_UNKNOWN") from None
        try:
            raw = json.loads(content)
            choice = raw["choices"][0]
            if choice.get("finish_reason") != "stop" or choice["message"].get("tool_calls"):
                raise ValueError("Incomplete or tool response")
            text = choice["message"]["content"]
            if not isinstance(text, str):
                raise ValueError("No text")
            usage = raw.get("usage") or {}
            def usage_value(value):
                if value is not None and (type(value) is not int or value < 0):
                    raise ValueError("Invalid usage")
                return value
            return ModelResponse(text=text, provider_receipt=provider_receipt or raw.get("id"),
                input_tokens=usage_value(usage.get("prompt_tokens")),
                output_tokens=usage_value(usage.get("completion_tokens")),
                cached_input_tokens=usage_value((usage.get("prompt_tokens_details") or {}).get("cached_tokens")),
                raw_response=raw, request_sha256=request.request_sha256,
                provider_reported_usd=_openrouter_charge(self.endpoint, content, raw.get("id")))
        except (KeyError, IndexError, TypeError, ValueError):
            raise ModelCallFailure("MODEL_PROTOCOL_ERROR") from None


class TrustedHostAdapter:
    """Plug in a separately authorized Host without giving it service authority.

    The controller must retain actual Host/tool-isolation evidence. The default
    is UNVERIFIED_HOST and the application will reject character dispatch.
    A callback must execute the supplied messages in a fresh session and bind
    its response to request_sha256. This adapter is not evidence of Host
    isolation or a claim that the callback was an API-billed model.
    """
    def __init__(self, invoke: Callable[[ModelRequest], ModelResponse] | None, *,
                 host: str, model: str, max_output_tokens: int = 4096,
                 isolation_evidence: str | None = None):
        self.invoke, self.host, self.model = invoke, host, model
        self.max_output_tokens, self.isolation_evidence = max_output_tokens, isolation_evidence

    @property
    def configuration(self):
        return {"adapter_kind": "TRUSTED_HOST", "provider": self.host, "model": self.model,
                "max_output_tokens": self.max_output_tokens, "timeout_seconds": 600,
                "isolation": "STATELESS_TEXT_ONLY" if self.isolation_evidence else "UNVERIFIED_HOST",
                "isolation_evidence": self.isolation_evidence, "execution_kind": "REAL_HOST"}

    def check_available(self):
        if self.invoke is None:
            raise DomainError("MODEL_UNAVAILABLE", "The authorized Host is not available")

    def generate(self, request):
        self.check_available()
        response = self.invoke(request)
        if not isinstance(response, ModelResponse) or response.request_sha256 != request.request_sha256:
            raise ModelCallFailure("HOST_BINDING_MISMATCH")
        return response
