"""Run one network-isolated Codex app-server on stdio with a leased character's MCP channel.

The invoking service owns the private binding, Host RPC transport and lifetime.
Only the Codex binary, the fixed relay and the two FIFOs enter the role namespace.
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path

from novel_factory.infrastructure.role_container import container_command


def run(binding, codex, relay, lifetime):
    with tempfile.TemporaryDirectory(prefix="nf-role-") as directory, ExitStack() as cleanup:
        channel = Path(directory)
        for name in ("requests", "replies"):
            os.mkfifo(channel / name, 0o600)
        # Local RDWR opens never block the service while the other endpoint starts.
        # Only this pair of processes shares these fresh, private FIFOs.
        for name in ("requests", "replies"):
            keeper = os.open(channel / name, os.O_RDWR | os.O_CLOEXEC)
            cleanup.callback(os.close, keeper)
        # The child owns only the direction it actually needs. If the launcher
        # and container disappear, its request reader receives EOF.
        with (open(channel / "requests", "rb", buffering=0) as requests,
              open(channel / "replies", "wb", buffering=0) as replies):
            server = subprocess.Popen([sys.executable, "-m", "novel_factory.interfaces.mcp_stdio",
                                       "--binding", str(binding), "--character-only"],
                                      stdin=requests, stdout=replies, stderr=subprocess.DEVNULL)
        cleanup.callback(stop, server)
        args = container_command(codex, relay, channel) + [
            "/bin/codex", "app-server", "--listen", "stdio://", "-c", "project_doc_max_bytes=0",
            "-c", "analytics.enabled=false", "-c", 'mcp_servers.novelfactory.command="/bin/role-mcp"',
        ]
        for feature in ("shell_tool", "multi_agent", "apps", "browser_use", "computer_use"):
            args.extend(["--disable", feature])
        host = subprocess.Popen(args, env={"PATH": os.defpath}, stderr=subprocess.DEVNULL)
        cleanup.callback(stop, host)
        deadline = time.monotonic() + lifetime
        while time.monotonic() < deadline and server.poll() is None:
            try:
                return host.wait(timeout=min(1, max(.01, deadline - time.monotonic())))
            except subprocess.TimeoutExpired:
                pass
        raise RuntimeError("Role host deadline or MCP service exit")


def stop(process):
    if process.poll() is None:
        process.kill()
    process.wait()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binding", required=True, type=Path)
    parser.add_argument("--relay", required=True, type=Path)
    parser.add_argument("--codex", default=shutil.which("codex"))
    parser.add_argument("--lifetime", type=int, default=90)
    args = parser.parse_args()
    if not args.codex or not 1 <= args.lifetime <= 600:
        parser.error("A Codex binary and a lifetime of 1 to 600 seconds are required")
    try:
        raise SystemExit(run(args.binding.resolve(), args.codex, args.relay, args.lifetime))
    except (OSError, RuntimeError, ValueError):
        raise SystemExit("Role host unavailable; check character lease, runtime and namespace support.") from None
