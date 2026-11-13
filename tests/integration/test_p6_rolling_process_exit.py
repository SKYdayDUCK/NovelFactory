"""Kill a separate P6 writing process after the loopback provider has received its request."""

import json
import os
import subprocess
import sys
from pathlib import Path
from threading import Event
from time import monotonic, sleep

import pytest
from test_p5_supervisor import backend
from test_p6_rolling_production import connect, request
from test_release import setup_batch

from novel_factory.application.production_pipeline import pipeline_status
from novel_factory.application.rolling_production import register_rolling, rolling_status
from novel_factory.application.supervisor import RuntimeSupervisor

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def writer_process():
    from test_p5_supervisor import SyntheticHTTP

    from novel_factory.application.authorization import Principal
    from novel_factory.application.kernel import Kernel
    from novel_factory.infrastructure.database import Database
    from novel_factory.infrastructure.objects import ObjectStore

    p = json.load(sys.stdin)
    db = Database(p["db"])
    kernel = Kernel(db, ObjectStore(p["objects"]))
    # Keep production's default lease length. The test supplies a short, real
    # PostgreSQL execution lease via its normal claim method, without editing timestamps.
    claim = kernel.tasks.claim_in_transaction

    def short_lease(*args, **kwargs):
        return claim(*args, **{**kwargs, "lease_seconds": 3})

    kernel.tasks.claim_in_transaction = short_lease
    adapter = SyntheticHTTP(
        endpoint=p["endpoint"],
        model="fixture-no-inference",
        api_key="synthetic-fixture-only",
        allow_loopback_http=True,
    )
    supervisor = RuntimeSupervisor(kernel, connect(kernel, adapter))
    owner = Principal(p["grant_id"], p["principal_id"])
    for _ in range(8):
        supervisor.run_once(owner, p["work"], p["branch"])
    db.close()


def test_terminated_writer_sent_attempt_becomes_unknown_and_does_not_resend(kernel_env):
    kernel = kernel_env["kernel"]
    f = setup_batch(kernel, count=1)
    wr, owner = f["world_ref"], f["principal"]
    work, branch = wr["work_id"], wr["branch_id"]
    with backend() as (adapter, calls, server):
        gateway = connect(kernel, adapter)
        register_rolling(kernel, gateway, owner, **request(kernel, f, 1))
        node_key = "draft:" + f["scenes"][0]["scene_contract"]["scene_id"]
        server.started, server.release = Event(), Event()
        packet = {
            "db": kernel_env["url"],
            "objects": str(kernel.objects.root),
            "work": work,
            "branch": branch,
            "endpoint": adapter.endpoint,
            "grant_id": owner.grant_id,
            "principal_id": owner.principal_id,
        }
        env = dict(
            os.environ,
            PYTHONPATH=os.pathsep.join(
                [str(Path(__file__).parent), os.environ.get("PYTHONPATH", "")]
            ),
        )
        child = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve())],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        try:
            child.stdin.write(json.dumps(packet))
            child.stdin.close()
            assert server.started.wait(20), (
                child.stderr.read() if child.poll() is not None else "No provider request"
            )
            child.kill()
            child.wait(timeout=10)
            assert child.returncode != 0
        finally:
            server.release.set()
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)
        before = pipeline_status(kernel, owner, work, branch)["nodes"][node_key]
        with kernel.db.uow(write=False) as repo:
            sent = repo.one("call_attempts", {"task_id": before["task_id"]})
            assert sent["state"] == "SENT"
        restarted = RuntimeSupervisor(kernel, gateway)
        until = monotonic() + 12
        while monotonic() < until:
            restarted.run_once(owner, work, branch)
            board = pipeline_status(kernel, owner, work, branch)["nodes"]
            if board[node_key]["state"] == "STOPPED":
                break
            sleep(0.1)
        assert board[node_key]["state"] == "STOPPED" and board[node_key]["reason"] == "TASK_PAUSED"
        with kernel.db.uow(write=False) as repo:
            attempts = repo.many("call_attempts", {"task_id": before["task_id"]})
            assert len(attempts) == 1 and attempts[0]["attempt_id"] == sent["attempt_id"]
            assert attempts[0]["state"] == "UNKNOWN"
            recovered = repo.one("tasks", {"task_id": before["task_id"]})
            assert recovered["generation"] > sent["generation"] and recovered["lease_owner"] is None
        assert (
            len(calls) == 1 and rolling_status(kernel, owner, work, branch)["frozen_chapters"] == 0
        )


if __name__ == "__main__":
    writer_process()
