"""Record reproducibility metadata without dumping environment secrets."""
from __future__ import annotations

import datetime as dt
import importlib.metadata as metadata
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys


def main() -> None:
    packages = []
    for distribution in sorted(metadata.distributions(), key=lambda d: d.metadata["Name"].lower()):
        info = distribution.metadata
        packages.append({
            "name": info["Name"], "version": distribution.version,
            "requires_python": info.get("Requires-Python"),
            "license_expression": info.get("License-Expression"),
            "license": info.get("License"),
            "license_classifiers": [x for x in info.get_all("Classifier", []) if x.startswith("License ::")],
        })
    memory = {}
    if Path("/proc/meminfo").exists():
        memory = dict(
            line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines()
            if line.startswith(("MemTotal:", "MemAvailable:", "SwapTotal:"))
        )
    cpu = []
    if Path("/proc/cpuinfo").exists():
        cpu = sorted(set(
            line.split(":", 1)[1].strip() for line in Path("/proc/cpuinfo").read_text().splitlines()
            if line.startswith("model name")
        ))
    pg_bin = os.environ.get("PG_BIN")
    postgres = None
    if pg_bin:
        postgres = subprocess.check_output([str(Path(pg_bin) / "postgres"), "--version"], text=True).strip()
    record = {
        "recorded_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "python": sys.version, "implementation": platform.python_implementation(),
        "platform": platform.platform(), "os_release": platform.freedesktop_os_release(),
        "machine": platform.machine(), "logical_cpu_count": os.cpu_count(), "cpu_model": cpu,
        "memory": memory, "disk_usage_bytes": shutil.disk_usage(Path.cwd())._asdict(),
        "postgres": postgres, "packages": packages,
        "target_host": "HTTP API / Python application service; no external LLM Host integration in P0/P1",
        "agent_runner": "ChatGPT code execution workspace; native Codex/Claude runtime compatibility not exercised",
    }
    print(json.dumps(record, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
