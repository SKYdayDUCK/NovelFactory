"""Record authorized model-route availability without reading or logging secrets.

This is a connectivity preflight, not a model production or literary acceptance.
The optional Host probe uses the existing login, default policy, and a read-only
ephemeral invocation. It never changes credentials/configuration or retries a
failed Host by weakening its security settings.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from novel_factory.domain.errors import DomainError
from novel_factory.infrastructure.model_gateway import OpenAICompatibleAdapter


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--probe-existing-host", action="store_true")
    args = parser.parse_args()
    record = {
        "recorded_at_utc": datetime.now(UTC).isoformat(),
        "purpose": "AUTHORIZED_MODEL_ROUTE_PREFLIGHT_ONLY",
        "configured": {key: bool(os.environ.get(key)) for key in (
            "NF_MODEL_ENDPOINT", "NF_MODEL_NAME", "NF_MODEL_API_KEY",
            "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
        )},
        "credentials_read_from_files": False,
        "credentials_or_user_configuration_modified": False,
        "http_provider_requests_sent": 0,
        "production_model_requests_sent": 0,
        "actual_cost": None,
        "cost_status": "NO_PROVIDER_BILLING_EVIDENCE",
        "literary_acceptance": "NOT_EVALUATED",
    }
    try:
        OpenAICompatibleAdapter.from_environment().check_available()
        record["http_route"] = "CONFIGURED_NOT_DISPATCHED"
    except (DomainError, ValueError) as error:
        record["http_route"] = "BLOCKED_ENVIRONMENT"
        record["http_reason"] = error.code if isinstance(error, DomainError) else "INVALID_MODEL_CONFIG"
    host = shutil.which("codex")
    record["host_installed"] = host is not None
    if args.probe_existing_host and host:
        try:
            status = subprocess.run([host, "login", "status"], check=False, text=True, capture_output=True, timeout=10)
            logged_in = status.returncode == 0 and "Logged in" in status.stdout + status.stderr
        except subprocess.TimeoutExpired:
            logged_in = False
            record["host_login_status"] = "BLOCKED_ENVIRONMENT_TIMEOUT"
        record["existing_host_login_detected"] = logged_in
        if logged_in:
            with tempfile.TemporaryDirectory(prefix="nf-host-connectivity-") as directory:
                command = [host, "exec", "--sandbox", "read-only", "--ephemeral",
                           "--skip-git-repo-check", "--json", "-C", directory,
                           ('Connectivity check only. Do not use tools or access any files. '
                            'Return exactly {"connectivity":"ok"}.')]
                try:
                    result = subprocess.run(command, check=False, text=True, capture_output=True, timeout=35)
                    combined = result.stdout + result.stderr
                    record["host_probe"] = {
                        "attempts": 1, "exit_status": result.returncode,
                        "sandbox": "read-only", "ephemeral": True,
                        "configuration_overrides": [], "prompt_contains_project_or_private_data": False,
                        "status": "CONNECTIVITY_ONLY" if result.returncode == 0 else "BLOCKED_ENVIRONMENT",
                        "diagnostics": [name for name, marker in (
                            ("READ_ONLY_FILESYSTEM", "Read-only file system"),
                            ("CONFIGURATION_ERROR", "config"),
                            ("SERVICE_INITIALIZATION_FAILED", "initialize"),
                            ("PERMISSION_DENIED", "Permission denied"),
                        ) if marker.lower() in combined.lower()],
                        "raw_output_retained": False,
                        "raw_output_omission_reason": "Host diagnostics can include private runtime configuration",
                        "host_isolation_verified": False,
                    }
                except subprocess.TimeoutExpired:
                    record["host_probe"] = {"attempts": 1, "status": "BLOCKED_ENVIRONMENT",
                                            "diagnostics": ["TIMEOUT_35_SECONDS"],
                                            "host_isolation_verified": False}
    record["real_production_acceptance"] = "BLOCKED_ENVIRONMENT"
    if record["http_route"] == "CONFIGURED_NOT_DISPATCHED":
        record["real_production_acceptance"] = "READY_FOR_SEPARATE_BOUNDED_PRODUCTION_RUN"
    record["author_full_read"] = "PENDING_AUTHOR"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
