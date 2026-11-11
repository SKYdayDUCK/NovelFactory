"""Linux mount/network/PID boundary for a single leased character's MCP channel."""
import os
import shutil
from pathlib import Path


def container_command(codex, relay, channel):
    """Administrator-selected static executables only; never mount a host directory of code."""
    unshare, bwrap = shutil.which("unshare"), shutil.which("bwrap")
    if os.name != "posix" or not unshare or not bwrap:
        raise RuntimeError("Linux user namespaces and bubblewrap are required")
    return [
        unshare, "--user", "--map-current-user", "--net", "--",
        bwrap, "--unshare-user", "--unshare-pid", "--unshare-ipc", "--unshare-uts",
        "--disable-userns", "--cap-drop", "ALL", "--new-session", "--die-with-parent",
        "--clearenv", "--setenv", "PATH", "/bin", "--setenv", "HOME", "/work",
        "--setenv", "CODEX_HOME", "/work/.codex", "--setenv", "TMPDIR", "/tmp",
        "--proc", "/proc", "--dev", "/dev", "--dir", "/bin",
        "--ro-bind", str(Path(codex).resolve(strict=True)), "/bin/codex",
        "--ro-bind", str(Path(relay).resolve(strict=True)), "/bin/role-mcp",
        "--ro-bind", str(channel), "/channel",
        "--size", "67108864", "--tmpfs", "/work", "--dir", "/work/.codex", "--size", "33554432", "--tmpfs", "/tmp",
        "--remount-ro", "/", "--chdir", "/work", "--",
    ]
