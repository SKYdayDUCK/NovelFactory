import json

import pytest

from novel_factory.domain.errors import DomainError
from novel_factory.infrastructure.creative_config import CreativeHarnessConfig


@pytest.mark.unit
def test_absent_configuration_retains_all_static_defaults(monkeypatch):
    monkeypatch.delenv("NF_CREATIVE_HARNESS_CONFIG", raising=False)
    cfg = CreativeHarnessConfig.from_env()
    assert (cfg.enabled, cfg.dynamic_agents, cfg.adaptive_instructions, cfg.auto_promote) == (False,) * 4
    assert cfg == CreativeHarnessConfig.parse(cfg.as_dict())


@pytest.mark.parametrize("values", [
    (True, False, False, False), (True, True, False, False),
    (True, False, True, False), (True, True, True, True),
])
def test_explicit_configuration_round_trips_at_service_boundary(monkeypatch, values):
    cfg = CreativeHarnessConfig(*values)
    monkeypatch.setenv("NF_CREATIVE_HARNESS_CONFIG", json.dumps(cfg.as_dict()))
    assert CreativeHarnessConfig.from_env() == cfg


@pytest.mark.parametrize("change", ["unknown", "missing", "coercion", "parent", "promotion"])
def test_malformed_or_unsafe_configuration_does_not_become_static(monkeypatch, change):
    cfg = CreativeHarnessConfig().as_dict()
    flags = cfg["creative_harness"]
    if change == "unknown":
        flags["another_flag"] = True
    elif change == "missing":
        del flags["adaptive_instructions"]
    elif change == "coercion":
        flags["enabled"] = 1
    elif change == "parent":
        flags["dynamic_agents"]["enabled"] = True
    else:
        flags["enabled"] = True
        flags["adaptive_instructions"]["auto_promote"] = True
    monkeypatch.setenv("NF_CREATIVE_HARNESS_CONFIG", json.dumps(cfg))
    with pytest.raises(DomainError) as err:
        CreativeHarnessConfig.from_env()
    assert err.value.code == "INVALID_ARGUMENT"


def test_deployed_defs_are_identical_to_the_frozen_s0_shapes():
    from pathlib import Path
    from novel_factory.contracts._schema_bundle import SCHEMAS
    source = Path(__file__).resolve().parents[2] / "contracts/creative_harness/creative_harness.schema.json"
    candidates = json.loads(source.read_text())["$defs"]
    deployed = SCHEMAS["harness.schema.json"]["$defs"]
    assert "ContextPacket" in deployed
    for name, contract in deployed.items():
        assert contract == candidates[name]
