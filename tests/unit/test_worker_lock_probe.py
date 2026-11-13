"""Liveness probes must not block behind a worker or create a missing lock."""
import subprocess
import sys

from novel_factory.infrastructure.native_files import file_lock, lock_is_held


def test_probe_observes_the_same_live_lock_across_processes(tmp_path):
    path = tmp_path / "worker.lock"
    assert not lock_is_held(path) and not path.exists()
    with file_lock(path):
        assert lock_is_held(path)
        result = subprocess.run([sys.executable, "-c",
            ("from novel_factory.infrastructure.native_files import lock_is_held; import sys; "
             "assert lock_is_held(sys.argv[1])"), str(path)], timeout=10, check=False)
        assert result.returncode == 0
    assert not lock_is_held(path)
