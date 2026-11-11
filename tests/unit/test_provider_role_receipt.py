"""Provider-reported USD can enter the role ledger only at exact DB precision."""
from decimal import Decimal

import pytest

from novel_factory.infrastructure.model_gateway import _openrouter_charge


@pytest.mark.parametrize("cost,expected", [
    ("0.012345", Decimal("0.012345")), ("0", Decimal(0)), ("0.0000005", None),
    ("0.100000000000000000001", None), ("-2", None), ("true", None), ('"0.1"', None),
])
def test_exact_provider_charge(cost, expected):
    endpoint = "https://openrouter.ai/api/v1/chat/completions"
    packet = ('{"usage":{"cost":' + cost + '}}').encode()
    assert _openrouter_charge(endpoint, packet, "gen-example") == expected
    assert _openrouter_charge("https://example.test/v1/chat/completions", packet, "gen-example") is None
    assert _openrouter_charge(endpoint, packet, None) is None
