"""Offline operator inventory for Codex + DeepSeek. Never authorizes or dispatches a task."""

import argparse
import json
import os
from pathlib import Path

from agent_runtime.credentials import resolve
from agent_runtime.host_process import resolve_host_binary
from agent_runtime.providers import ProfileError, read_profiles


def inspect_profiles(path):
    rows = []
    for key, profile in read_profiles(path).items():
        reasons = []
        if profile.model.startswith("REPLACE_"):
            reasons.append("MODEL_NOT_SELECTED")
        if not resolve(profile.secret_ref):
            reasons.append("CREDENTIAL_NOT_CONFIGURED")
        if profile.kind == "codex-cli" and not resolve_host_binary():
            reasons.append("CODEX_EXECUTABLE_UNAVAILABLE")
        rows.append(
            {
                "profile": key,
                "kind": profile.kind,
                "auth_mode": profile.auth_mode,
                "model": profile.model,
                "max_output_tokens": profile.max_output_tokens,
                "local_checks": "BLOCKED" if reasons else "PASS",
                "conditions": reasons,
            }
        )
    ready = {r["kind"] for r in rows if r["local_checks"] == "PASS"}
    return {
        "local_checks": "PASS" if {"codex-cli", "deepseek"} <= ready else "BLOCKED_ENVIRONMENT",
        "profiles": rows,
        "live_opt_in_present": os.environ.get("NF_RUNTIME_LIVE_APPROVED") == "1",
        "provider_authentication": "NOT_TESTED",
        "real_model_calls": 0,
        "live_parallelism": "NOT_EXECUTED",
        "settled_provider_amount": None,
        "independent_reader_quality": "NOT_EVALUATED",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profiles", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = inspect_profiles(args.profiles)
    except ProfileError as exc:
        parser.exit(2, str(exc) + "\n")
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["local_checks"] == "PASS" else 2)
