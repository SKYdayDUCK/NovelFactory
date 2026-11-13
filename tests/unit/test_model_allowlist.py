"""Operator-selected Flash/Codex policy denies an expensive route before secret access."""

import json

import pytest

from agent_runtime import credentials
from agent_runtime.providers import Profile, ProfileError, check_model_policy


def test_disabled_pro_is_rejected_without_resolving_credential(monkeypatch):
    monkeypatch.setenv("NF_RUNTIME_LIVE_APPROVED", "1")
    monkeypatch.setenv("NF_RUNTIME_ALLOWED_MODELS", json.dumps({"deepseek": ["deepseek-flash"], "codex-cli": ["gpt-6.1-sol"]}))
    monkeypatch.setattr(credentials, "resolve", lambda *_: pytest.fail("Disabled model accessed a credential"))
    with pytest.raises(ProfileError, match="MODEL_NOT_ALLOWED"):
        Profile("deepseek", "deepseek-v4-pro", "NO_REAL_CREDENTIAL", 8192).preflight()
    check_model_policy("deepseek", "deepseek-flash")
    check_model_policy("codex-cli", "gpt-6.1-sol")


@pytest.mark.parametrize("raw", ["", "[]", "{}", '{"deepseek":[]}', '{"unknown":["x"]}',
                                 '{"deepseek":[1]}', '{"deepseek":["x"],"deepseek":["y"]}'])
def test_invalid_host_policy_fails_closed(monkeypatch, raw):
    monkeypatch.setenv("NF_RUNTIME_ALLOWED_MODELS", raw)
    with pytest.raises(ProfileError, match="INVALID_MODEL_POLICY"):
        check_model_policy("deepseek", "deepseek-flash")
