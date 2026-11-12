"""Own the lifetime of a stdio Host and its private subprocess group."""
from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
from contextlib import suppress
from pathlib import Path

# These features are reported by the pinned CLI's features list. Both adapters
# register their MCP endpoints explicitly and do not consume a plugin catalog.
ISOLATED_PLUGIN_OPTIONS = (
    "-c", "features.plugins=false",
    "-c", "features.remote_plugin=false",
    "-c", "features.plugin_discovery_prefetch=false",
    "-c", "features.plugin_sharing=false",
)


async def start_host(*argv, **kwargs):
    """Start a session owned by just this invocation, never the caller's group."""
    job = None
    if os.name == "posix":
        kwargs["start_new_session"] = True
    elif os.name == "nt":
        from ._windows_host import create_job

        job, name = create_job()
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        argv = (sys.executable, "-I", str(Path(__file__).with_name("_windows_host.py")), name, *argv)
    try:
        proc = await asyncio.create_subprocess_exec(*argv, **kwargs)
    except BaseException:
        if job is not None:
            job.Close()
        raise
    proc._nf_host_job = job
    proc._nf_host_group = proc.pid
    return proc


def isolated_host_environment(home):
    base = str(home)
    env = {"HOME": base, "CODEX_HOME": str(Path(base) / "codex"), "PATH": os.defpath, "NO_PROXY": "127.0.0.1"}
    if os.name == "nt":
        system = Path(os.environ["SystemRoot"])
        # Windows process/DLL initialization needs SystemRoot. The child gets
        # private profile and temporary directories rather than the user's.
        env.update(SystemRoot=str(system), PATH=str(system / "System32"), USERPROFILE=base,
                   TMP=base, TEMP=base, LOCALAPPDATA=base, APPDATA=base)
    return env


def _live_groups(session):
    if session <= 1 or session in {os.getsid(0), os.getpgrp()}:
        raise RuntimeError("Host cleanup refused a shared process group")
    if not sys.platform.startswith("linux"):
        try:
            os.killpg(session, 0)
        except ProcessLookupError:
            return set()
        return {session}

    # MCP/git helpers may form a second group inside our private session. A
    # host that already exited can still leave these helpers writing its HOME.
    groups = set()
    with os.scandir("/proc") as entries:
        for entry in entries:
            if not entry.name.isdecimal():
                continue
            try:
                record = (Path(entry.path) / "stat").read_bytes().rpartition(b") ")[2].split()
            except (FileNotFoundError, ProcessLookupError, PermissionError):
                continue
            if len(record) >= 4 and int(record[3]) == session and record[0] not in {b"Z", b"X"}:
                groups.add(int(record[2]))
    return groups


async def _discard(reader):
    if reader is not None:
        while await reader.read(65536):
            pass


async def stop_host(proc):
    """Close stdio, then terminate surviving owned helpers before returning."""
    cleanup = asyncio.create_task(_stop_owned(proc))
    try:
        await asyncio.shield(cleanup)
    except asyncio.CancelledError:
        # Teardown owns the temporary directory until every child has stopped.
        await cleanup
        raise


async def _stop_owned(proc):
    session = getattr(proc, "_nf_host_group", None)
    if session != proc.pid:
        raise ValueError("Host cleanup requires a process from start_host")
    if proc.stdin:
        proc.stdin.close()
    finished = asyncio.gather(proc.wait(), _discard(proc.stdout), _discard(proc.stderr))
    try:
        # Give the app-server time to shut down and reap its own MCP children.
        with suppress(TimeoutError):
            await asyncio.wait_for(asyncio.shield(finished), 1.5)
        if os.name == "posix":
            for sig, grace in ((signal.SIGTERM, .65), (signal.SIGKILL, 2)):
                groups = _live_groups(session)
                if not groups:
                    break
                for group in groups:
                    if group == os.getpgrp():
                        raise RuntimeError("Host cleanup refused the caller's group")
                    with suppress(ProcessLookupError):
                        os.killpg(group, sig)
                deadline = asyncio.get_running_loop().time() + grace
                while _live_groups(session) and asyncio.get_running_loop().time() < deadline:
                    await asyncio.sleep(.03)
            if _live_groups(session):
                raise RuntimeError("Owned Host helpers did not stop within the deadline")
        elif os.name == "nt":
            # Closing the final handle fences descendants after parent exit too.
            proc._nf_host_job.Close()
        elif proc.returncode is None:
            with suppress(ProcessLookupError):
                proc.kill()
        await asyncio.wait_for(asyncio.shield(finished), 2)
    finally:
        if not finished.done():
            finished.cancel()
            with suppress(asyncio.CancelledError):
                await finished
