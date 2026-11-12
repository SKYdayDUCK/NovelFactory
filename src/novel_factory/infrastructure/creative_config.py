"""Trusted service configuration for Creative Harness, frozen into each new AgentInput."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os

from novel_factory.contracts.registry import validate_contract
from novel_factory.domain.errors import DomainError


@dataclass(frozen=True)
class CreativeHarnessConfig:
    enabled: bool = False
    dynamic_agents: bool = False
    adaptive_instructions: bool = False
    auto_promote: bool = False

    def __post_init__(self):
        validate_contract("HarnessConfiguration", self.as_dict(), "harness")

    def as_dict(self) -> dict:
        return {
            "schema_version": "1.0",
            "kind": "CONFIGURATION",
            "creative_harness": {
                "enabled": self.enabled,
                "dynamic_agents": {"enabled": self.dynamic_agents},
                "adaptive_instructions": {
                    "enabled": self.adaptive_instructions,
                    "auto_promote": self.auto_promote,
                },
            },
        }

    @classmethod
    def parse(cls, record: dict) -> CreativeHarnessConfig:
        flags = validate_contract("HarnessConfiguration", record, "harness")["creative_harness"]
        return cls(flags["enabled"], flags["dynamic_agents"]["enabled"],
                   flags["adaptive_instructions"]["enabled"],
                   flags["adaptive_instructions"]["auto_promote"])

    @classmethod
    def from_env(cls) -> CreativeHarnessConfig:
        raw = os.environ.get("NF_CREATIVE_HARNESS_CONFIG")
        if raw is None:
            return cls()
        try:
            record = json.loads(raw)
        except ValueError as exc:
            raise DomainError("INVALID_ARGUMENT", "Invalid Creative Harness configuration JSON") from exc
        return cls.parse(record)
