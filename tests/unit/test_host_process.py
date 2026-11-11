"""The real cleanup path must stop surviving helpers without touching a sibling Host."""
import asyncio
import os
import sys
from pathlib import Path

import pytest

from novel_factory.infrastructure.host_process import start_host, stop_host


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="exercises Linux session and process-group ownership")
def test_stop_host_reaps_parent_and_stops_orphan_writer_in_subgroup(tmp_path):
    async def run():
        outsider = await asyncio.create_subprocess_exec(sys.executable, "-c", "import time; time.sleep(60)")
        # The fake Host exits cleanly on EOF; a stubborn git-like helper has a
        # different process group, shares the session, and keeps writing HOME.
        script = """
import os, pathlib, signal, sys, time
root = pathlib.Path(sys.argv[1])
if not os.fork():
    os.setpgid(0, 0)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    (root / 'child').write_text(str(os.getpid()))
    while True:
        (root / 'activity').touch()
        time.sleep(.005)
sys.stdin.buffer.read()
(root / 'eof').touch()
"""
        proc = await start_host(sys.executable, "-c", script, str(tmp_path), stdin=asyncio.subprocess.PIPE,
                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            assert os.getsid(proc.pid) == proc.pid != os.getsid(0)
            async with asyncio.timeout(3):
                while not (tmp_path / "activity").exists():
                    await asyncio.sleep(.01)
            child = int((tmp_path / "child").read_text())
            assert os.getpgid(child) == child and os.getsid(child) == proc.pid
            await asyncio.wait_for(stop_host(proc), timeout=6)
            assert proc.returncode == 0 and (tmp_path / "eof").is_file()
            state = Path(f"/proc/{child}/stat")
            if state.exists():
                assert state.read_bytes().rpartition(b") ")[2].split()[0] in {b"Z", b"X"}
            assert outsider.returncode is None
            with pytest.raises(ValueError, match="requires a process from start_host"):
                await stop_host(outsider)
        finally:
            if proc.returncode is None:
                await stop_host(proc)
            outsider.kill()
            await outsider.wait()

    asyncio.run(run())
