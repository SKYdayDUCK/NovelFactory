"""Native filesystem, private config and host lifecycle smoke (no provider network)."""
import asyncio
import json
import os
from pathlib import Path
import socket
import sys

import pytest

from agent_runtime.host_process import isolated_host_environment, start_host, stop_host
from novel_factory.infrastructure.native_files import publish_file
from novel_factory.infrastructure.private_files import (
    check_private_directory, prepare_private_directory, private_temporary_file, read_private_bytes,
)


def test_private_binding_is_verified_and_size_bounded(tmp_path):
    private = tmp_path / "运行时 credentials"
    prepare_private_directory(private)
    check_private_directory(private)
    target = private / "binding.json"
    fd, name = private_temporary_file(private)
    try:
        with os.fdopen(fd, "wb") as source:
            source.write(b'{"token":"fixture"}')
            source.flush()
            os.fsync(source.fileno())
        publish_file(name, target)
        assert json.loads(read_private_bytes(target, 128)) == {"token": "fixture"}
        with pytest.raises(ValueError):
            read_private_bytes(target, 2)
        if os.name == "nt":
            import win32security as sec

            descriptor = sec.ConvertStringSecurityDescriptorToSecurityDescriptor(
                "D:P(A;;GR;;;WD)", sec.SDDL_REVISION_1)
            sec.SetNamedSecurityInfo(str(target), sec.SE_FILE_OBJECT, sec.DACL_SECURITY_INFORMATION,
                                     None, None, descriptor.GetSecurityDescriptorDacl(), None)
        else:
            target.chmod(0o644)
        with pytest.raises(ValueError):
            read_private_bytes(target, 128)
    finally:
        Path(name).unlink(missing_ok=True)


def test_native_host_reaps_child_after_host_exits(tmp_path):
    async def run():
        script = """import subprocess, sys
subprocess.Popen([sys.executable, "-c", sys.argv[1]])
sys.stdin.buffer.read()
"""
        child = """import socket, time
s = socket.socket()
s.bind(("127.0.0.1", 0))
s.listen()
print(s.getsockname()[1], flush=True)
time.sleep(60)
"""
        proc = await start_host(sys.executable, "-c", script, child, stdin=asyncio.subprocess.PIPE,
                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                                env=isolated_host_environment(tmp_path))
        try:
            port = int(await asyncio.wait_for(proc.stdout.readline(), 10))
        finally:
            await asyncio.wait_for(stop_host(proc), timeout=8)
        assert proc.returncode == 0
        with pytest.raises(ConnectionRefusedError):
            socket.create_connection(("127.0.0.1", port), timeout=2)

    asyncio.run(run())


@pytest.mark.skipif(os.name != "nt", reason="asserts the native Windows write-through API")
def test_windows_write_through_publication_keeps_first_writer(tmp_path, monkeypatch):
    import win32file

    moves = []
    move = win32file.MoveFileEx

    def observe(src, dest, flags):
        moves.append(flags)
        return move(src, dest, flags)

    monkeypatch.setattr(win32file, "MoveFileEx", observe)
    dest = tmp_path / "digest"
    source = tmp_path / "upload"
    source.write_bytes(b"first")
    publish_file(source, dest)
    source.write_bytes(b"second")
    with pytest.raises(FileExistsError):
        publish_file(source, dest)
    assert moves == [win32file.MOVEFILE_WRITE_THROUGH] * 2
    assert dest.read_bytes() == b"first"


@pytest.mark.skipif(os.name != "nt", reason="Tests selection of an explicit Windows native image")
def test_windows_host_selects_native_executable(monkeypatch):
    from agent_runtime.host_process import resolve_host_binary

    monkeypatch.setenv("NF_CODEX_EXECUTABLE", sys.executable)
    assert resolve_host_binary() == sys.executable
    monkeypatch.setenv("NF_CODEX_EXECUTABLE", "codex.cmd")
    assert resolve_host_binary() is None
