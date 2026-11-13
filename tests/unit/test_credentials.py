"""A stored provider credential survives processes without entering catalogs or output."""
import json
import os
import subprocess
import sys
from uuid import uuid4

import pytest

from agent_runtime import credentials
from agent_runtime.providers import Profile, ProfileError

pytestmark = pytest.mark.unit


@pytest.fixture
def reference(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    name = "NF_TEST_VAULT_" + uuid4().hex.upper()
    yield name
    credentials.remove(name)


def test_local_store_is_resolved_in_a_fresh_process_and_status_is_redacted(reference):
    credentials.put(reference, "synthetic-private-key")
    result = subprocess.run([sys.executable, "-c",
        ("from agent_runtime.credentials import resolve; import sys; "
         "assert resolve(sys.argv[1]) == 'synthetic-private-key'"), reference],
        capture_output=True, text=True, check=True)
    assert not result.stdout and not result.stderr
    visible = json.dumps(credentials.status(reference))
    assert "synthetic-private-key" not in visible
    assert credentials.status(reference)["source"] == "local-store"
    credentials.remove(reference)
    assert credentials.resolve(reference) is None


def test_environment_override_does_not_overwrite_the_stored_key(reference, monkeypatch):
    credentials.put(reference, "stored-synthetic")
    monkeypatch.setenv(reference, "environment-synthetic")
    assert credentials.resolve(reference) == "environment-synthetic"
    assert credentials.status(reference)["source"] == "environment"
    monkeypatch.delenv(reference)
    assert credentials.resolve(reference) == "stored-synthetic"


def test_provider_uses_vault_only_after_live_opt_in(reference, monkeypatch):
    profile = Profile(kind="deepseek", model="synthetic", secret_ref=reference, max_output_tokens=32)
    monkeypatch.delenv("NF_RUNTIME_LIVE_APPROVED", raising=False)
    monkeypatch.setattr(credentials, "_stored", lambda _: pytest.fail("unauthorized credential read"))
    with pytest.raises(ProfileError, match="MODEL_CALL_NOT_AUTHORIZED"):
        profile.preflight()
    monkeypatch.setattr(credentials, "_stored", lambda _: "synthetic-key")
    monkeypatch.setenv("NF_RUNTIME_LIVE_APPROVED", "1")
    assert profile.preflight() == "synthetic-key"
    assert "synthetic-key" not in json.dumps(profile.describe())


def test_cli_accepts_stdin_and_never_prints_the_value(reference):
    result = subprocess.run([sys.executable, "scripts/nf.py", "credentials", "set", reference, "--stdin"],
                            input="synthetic-cli-key\n", capture_output=True, text=True, check=True)
    assert json.loads(result.stdout)["configured"] is True
    assert "synthetic-cli-key" not in result.stdout + result.stderr
    assert credentials.resolve(reference) == "synthetic-cli-key"


@pytest.mark.parametrize("name", ["../secret", "LOWER-case", "", "A/B"])
def test_invalid_references_are_rejected(name):
    with pytest.raises(credentials.CredentialError, match="INVALID_CREDENTIAL_REFERENCE"):
        credentials.put(name, "synthetic")


@pytest.mark.skipif(os.name == "nt", reason="POSIX store permissions; Windows uses Credential Manager")
def test_posix_store_rejects_public_or_symlinked_secrets(reference, monkeypatch, tmp_path):
    credentials.put(reference, "synthetic")
    path = credentials._directory() / reference
    path.chmod(0o644)
    with pytest.raises(credentials.CredentialError, match="CREDENTIAL_STORE_NOT_PRIVATE"):
        credentials.resolve(reference)
    path.unlink()
    other = tmp_path / "outside"
    other.write_text("synthetic")
    path.symlink_to(other)
    with pytest.raises(OSError):
        credentials.resolve(reference)
