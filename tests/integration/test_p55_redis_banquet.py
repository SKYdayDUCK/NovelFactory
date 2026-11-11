"""Real CLI processes, a PostgreSQL workflow, an isolated Redis and a synthetic HTTPS model."""

import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from contextlib import ExitStack

import pytest
from sqlalchemy import text
from test_p5_autonomous_banquet import run_banquet

from novel_factory.application.audit_log import LogService
from novel_factory.application.statistics import StatisticsService
from scripts.pitr_streams import StreamsDrill

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


class RedisWorkers:
    def __init__(self, stack, tmp_path):
        self.stack, self.root = stack, tmp_path
        self.children = []

    @staticmethod
    def stop(proc):
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=10)

    def start(self, command, env):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        server = StreamsDrill(self.root, port)
        self.stack.callback(server.close)
        env["NF_REDIS_URL"] = f"redis://127.0.0.1:{port}/0"
        subprocess.run([*command, "--set-transport", "redis"], env=env, check=True, timeout=15)
        programs = [[sys.executable, "scripts/run_eventhub.py", "relay"]]
        for group in ("model", "compute", "recovery", "world", "log", "statistics"):
            programs.append([sys.executable, "scripts/run_processor.py", "--group", group])
        for proc_type in ("compute", "world", "recovery"):
            programs.append([*command, "--processor", proc_type, "--workers", "1"])
        for n, program in enumerate(programs):
            log = self.stack.enter_context((self.root / f"service-{n}.log").open("w"))
            proc = subprocess.Popen(
                program, env=env, stdout=log, stderr=log, start_new_session=True
            )
            self.stack.callback(self.stop, proc)
            self.children.append(proc)

    def check(self, kernel, principal, work, branch, trace):
        stats = StatisticsService(kernel)
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            assert all(child.poll() is None for child in self.children)
            rows = LogService.trace(kernel, principal, work, branch, trace)
            types = {row["event_type"] for row in rows}
            if {"model.completed", "world.accepted", "supervisor.editorial.delivery"} <= types:
                snapshot = stats.verify_statistics(
                    principal, work, branch, filters={"trace_id": trace}
                )
                if snapshot["consistent"] and snapshot["processed_events"] >= len(rows):
                    with kernel.db.uow(write=False) as repo:
                        assert (
                            repo.conn.scalar(
                                text(
                                    "SELECT count(*) FROM nf_bus_subscriptions WHERE status='DEAD'"
                                )
                            )
                            == 0
                        )
                    assert snapshot["metrics"]["world.accepted_events"] > 0
                    assert snapshot["metrics"]["model.fixture.calls"] >= 7
                    return
            time.sleep(0.25)
        pytest.fail(
            f"Redis scene trace or statistics stalled; observed event types: {sorted(types)}"
        )


def test_eventhub_multi_process_banquet(kernel_env, tmp_path):
    if not os.environ.get("NF_TEST_REDIS_URL") or not shutil.which("redis-server"):
        pytest.skip("requires disposable Redis and redis-server for a private scene instance")
    with ExitStack() as stack:
        run_banquet(kernel_env, tmp_path, redis_workers=RedisWorkers(stack, tmp_path))
