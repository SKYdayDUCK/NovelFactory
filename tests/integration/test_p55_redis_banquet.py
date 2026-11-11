"""Real CLI processes, a PostgreSQL workflow, an isolated Redis and a synthetic HTTPS model."""

import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
from contextlib import ExitStack, suppress
from pathlib import Path

import pytest
from sqlalchemy import text
from test_p5_autonomous_banquet import run_banquet
from test_p5_supervisor import _job, backend

from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.audit_log import LogService
from novel_factory.application.statistics import StatisticsService
from novel_factory.application.supervisor import HostJournal, RuntimeSupervisor
from novel_factory.fixtures import load_fixture
from novel_factory.infrastructure.codex_host import CodexAppServerAdapter
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


def test_completed_real_text_turn_stops_background_helper_without_another_send(
    kernel_env, tmp_path
):
    binary = shutil.which("codex")
    if not binary or not Path("/proc/self/stat").is_file():
        pytest.skip("requires the installed Codex CLI and Linux process observation")
    marker = tmp_path / "helper.pid"
    wrapper = tmp_path / "codex-with-helper"
    # A subprocess in the Host's own session remains after the real CLI exits.
    # It has no model access and does not intercept or rewrite any Host input.
    wrapper.write_text(
        "#!/bin/sh\n/bin/sleep 90 </dev/null >/dev/null 2>&1 &\n"
        f'echo $! > {shlex.quote(str(marker))}\nexec {shlex.quote(binary)} "$@"\n'
    )
    wrapper.chmod(0o700)
    kernel = kernel_env["kernel"]
    world = load_fixture(kernel)
    work, branch = (world["world_ref"][k] for k in ("work_id", "branch_id"))
    try:
        with backend() as (http, requests, _):
            adapter = CodexAppServerAdapter(http, HostJournal(kernel), binary=str(wrapper))
            gateway = AgentGateway(kernel, {"test": adapter})
            supervisor = RuntimeSupervisor(kernel, gateway)
            task = _job(
                kernel, gateway, supervisor, world, kernel.tasks.create_budget(work, "2"), "READY"
            )
            result = supervisor.run_once(world["principal"], work, branch)
            assert result["status"] == "RESULT_STORED"
            assert supervisor.run_once(world["principal"], work, branch)["status"] == "IDLE"
            assert len(requests) == 1
            with kernel.db.uow(write=False) as repo:
                attempt = repo.one("call_attempts", {"task_id": task.task["task_id"]})
                evidence = gateway._read_audit(repo, attempt["attempt_id"])["payload"]
                assert evidence["outcome"] == "VALIDATED_CANDIDATE" and evidence["reason"] is None
                # This fixture provides token usage and no charge; accounting remains open.
                assert attempt["state"] == "UNKNOWN" and evidence["usage"]["actual_amount"] is None
                assert (
                    repo.one("host_turns", {"attempt_id": attempt["attempt_id"]})["phase"]
                    == "COMPLETED"
                )
            pid = int(marker.read_text())
            try:
                state = Path(f"/proc/{pid}/stat").read_bytes().rsplit(b") ", 1)[1].split()[0]
            except FileNotFoundError:
                state = None
            assert state in {None, b"Z", b"X"}, "Background Host helper remained live"
    finally:
        if marker.is_file():
            with suppress(ProcessLookupError, FileNotFoundError):
                pid = int(marker.read_text())
                # Close an injected helper if a regression leaves it running.
                if Path(f"/proc/{pid}/cmdline").read_bytes() == b"/bin/sleep\x0090\x00":
                    os.kill(pid, signal.SIGKILL)
