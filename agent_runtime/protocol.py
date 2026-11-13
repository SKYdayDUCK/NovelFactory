"""Portable invocation and receipt types shared by legacy and routed clients."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


@dataclass(frozen=True)
class DryRunSpec:
    seed: int = 0
    review_pass_rate: float = 0.7
    outcome: str = "normal"
    payload: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ModelRequest:
    messages: tuple[dict, ...]
    request_sha256: str
    max_output_tokens: int
    attempt_id: str | None = None
    dry_run: bool = False
    simulation: DryRunSpec | None = None


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
    dry_run: bool = False


class ModelCallFailure(Exception):
    """Only stable diagnostics are exposed; provider error text can be secret."""
    def __init__(self, code: str, *, sent: bool = True, http_status: int | None = None,
                 received: ModelResponse | None = None):
        super().__init__(code)
        self.code, self.sent, self.http_status = code, sent, http_status
        self.received = received
