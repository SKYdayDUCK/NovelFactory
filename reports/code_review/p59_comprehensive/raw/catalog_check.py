"""Observe catalog loading, without model calls or credentials."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from agent_runtime.providers import read_profiles
from novel_factory.infrastructure.runtime_providers import load_runtime_profiles

with TemporaryDirectory() as directory:
    path = Path(directory) / "profiles.json"
    for port in ("443", "70000", "typo"):
        path.write_text(json.dumps({"profiles": {"fixture": {
            "kind": "openai-compatible", "endpoint": f"https://example.org:{port}/v1/chat/completions",
            "secret_ref": "NF_UNUSED", "model": "fixture", "max_output_tokens": 64}}}))
        for name, action in (("read_profiles", lambda: read_profiles(path)),
                             ("load_runtime_profiles", lambda: load_runtime_profiles(path, None))):
            try:
                action()
                result = "accepted"
            except Exception as exc:
                result = type(exc).__name__ + " " + str(getattr(exc, "code", exc))
            print(json.dumps({"port": port, "function": name, "result": result}))
