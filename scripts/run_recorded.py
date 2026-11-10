"""Run a command and retain exact stdout, stderr, exit status and timing.

Use: python scripts/run_recorded.py evidence/name -- command arguments ...
Do not put credentials in the command; pass them by environment or a private .pgpass.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
import platform
import shlex
import subprocess
import sys
import time


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output_prefix", type=Path)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        parser.error("a command is required after --")
    prefix: Path = args.output_prefix
    prefix.parent.mkdir(parents=True, exist_ok=True)
    started = dt.datetime.now(dt.timezone.utc)
    timer = time.monotonic()
    metadata = {
        "command": command,
        "command_display": shlex.join(command),
        "cwd": str(Path.cwd()),
        "started_at_utc": started.isoformat(),
        "python": sys.version,
        "platform": platform.platform(),
    }
    with prefix.with_suffix(".log").open("w", encoding="utf-8") as log:
        log.write(json.dumps(metadata, ensure_ascii=False) + "\n")
        log.flush()
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        assert process.stdout is not None
        for line in iter(process.stdout.readline, b""):
            value = line.decode("utf-8", errors="replace")
            sys.stdout.write(value)
            sys.stdout.flush()
            log.write(value)
            log.flush()
        returncode = process.wait()
        metadata.update({
            "exit_status": returncode,
            "duration_seconds": round(time.monotonic() - timer, 6),
            "finished_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        })
        log.write("\nRUN_METADATA " + json.dumps(metadata, ensure_ascii=False) + "\n")
    prefix.with_suffix(".json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return returncode


if __name__ == "__main__":
    raise SystemExit(main())
