"""P5 waiting-task upgrade and real Redis service ACL/CLI compatibility."""

import hashlib
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from redis import Redis
from redis.exceptions import ConnectionError, NoPermissionError
from sqlalchemy import text

from novel_factory.infrastructure.eventhub import EventHub
from novel_factory.orchestration.workflow import WorkflowService

from tests.fixtures.historical_release import run_release
from .test_workflow import accept, scope_args

ROOT = Path(__file__).resolve().parents[2]
pytestmark = [
    pytest.mark.postgres,
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("NF_TEST_REDIS_URL"),
        reason="requires disposable Redis integration environment",
    ),
]


def test_upgrade_with_parked_wait_and_protect_live_delivery(rel_env, rel_scope, tmp_path):
    env = {**os.environ, "NF_DATABASE_URL": rel_env["url"]}

    def migrate(verb, target, check=True):
        return subprocess.run(
            [sys.executable, "-m", "alembic", verb, target],
            check=check,
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
        )

    migrate("downgrade", "0007")
    # Claims and durable waits are written by P5's actual deployed TaskService.
    # Its migration head is 0007. Its original test driver builds the bound task.
    stored = run_release(ROOT, tmp_path, "9dc9c6b806b8ac43f56d0893194a5b402f3f16ca", """
import json, os
from novel_factory.infrastructure.database import Database, tables
from novel_factory.infrastructure.objects import ObjectStore
from novel_factory.orchestration.tasks import TaskService
from novel_factory.orchestration.workflow import WorkflowService
from tests.reliability.conftest import start_task
from tests.reliability.test_workflow import blocked, scope_args
db = Database()
try:
    store = ObjectStore(os.environ["HISTORICAL_OBJECTS"])
    service = TaskService(db.engine, tables, store)
    env = dict(db=db, store=store, service=service)
    scope = json.loads(os.environ["HISTORICAL_SCOPE"])
    task = start_task(env, scope)
    target = blocked(env, scope)
    WorkflowService(service).wait(target, 0, [task["task_id"]], mode="AND", **scope_args(scope))
    print(json.dumps({"task": {"task_id": task["task_id"], "generation": task["generation"]},
                      "target": target}))
finally:
    db.close()
""", {**env, "HISTORICAL_SCOPE": json.dumps(rel_scope),
      "HISTORICAL_OBJECTS": str(rel_env["store"].root)},
        fixture_files=("tests/reliability/__init__.py", "tests/reliability/conftest.py",
                       "tests/reliability/test_workflow.py"))
    task, target = stored["task"], stored["target"]
    wf = WorkflowService(rel_env["service"])
    with rel_env["db"].uow(write=False) as repo:
        before = (
            repo.conn.execute(
                text("SELECT * FROM nf_workflow_waits WHERE task_id=:id"), {"id": target}
            )
            .mappings()
            .one()
        )
    migrate("upgrade", "head")
    with rel_env["db"].uow(write=False) as repo:
        after = (
            repo.conn.execute(
                text("SELECT * FROM nf_workflow_waits WHERE task_id=:id"), {"id": target}
            )
            .mappings()
            .one()
        )
        assert after == before
    assert wf.resume_project(**scope_args(rel_scope))["awakened"] == 0
    from .test_p55_eventhub import RedisProcess

    service = RedisProcess(tmp_path)
    try:
        hub = EventHub(rel_env["db"], service.client)
        assert hub.backfill(64) > 0
        assert hub.relay_once(64) > 0
    finally:
        service.stop()
        service.client.close()
    refused = migrate("downgrade", "0007", check=False)
    assert refused.returncode and "Preserve P5.5 transport evidence" in refused.stderr
    accept(rel_env, task)
    assert wf.resume_project(**scope_args(rel_scope))["awakened"] == 1
    assert wf.resume_project(**scope_args(rel_scope))["awakened"] == 0


def test_deployed_acl_and_each_operator_entrypoint(rel_env, tmp_path):
    passwords = {user: secrets.token_hex(32) for user in ("relay", "worker", "maintenance")}
    template = (ROOT / "configs/redis-eventhub.acl.example").read_text()
    for user, password in passwords.items():
        template = template.replace(
            "<" + user.upper() + "_SHA256>", hashlib.sha256(password.encode()).hexdigest()
        )
    acl = tmp_path / "users.acl"
    acl.write_text(template)
    acl.chmod(0o600)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    proc = subprocess.Popen(
        [
            shutil.which("redis-server"),
            str(ROOT / "configs/redis-eventhub.conf"),
            "--dir",
            str(tmp_path),
            "--aclfile",
            str(acl),
            "--logfile",
            str(tmp_path / "redis.log"),
            "--port",
            str(port),
        ]
    )
    urls = {user: f"redis://{user}:{pw}@127.0.0.1:{port}/0" for user, pw in passwords.items()}

    def run(user, entry, args):
        env = {**os.environ, "NF_DATABASE_URL": rel_env["url"], "NF_REDIS_URL": urls[user]}
        p = subprocess.run(
            [sys.executable, "scripts/" + entry, *args],
            env=env,
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        return json.loads(p.stdout)

    try:
        with Redis.from_url(urls["relay"]) as relay:
            for _ in range(200):
                try:
                    if relay.ping():
                        break
                except ConnectionError:
                    pass
                assert proc.poll() is None
                time.sleep(0.01)
            with pytest.raises(NoPermissionError):
                relay.get("nf:test:no-get")
        with rel_env["db"].uow() as repo:
            from uuid import uuid4

            ident = str(uuid4())
            event_id = str(repo.outbox("world.requested", "test", ident, {}, "cli-test:" + ident))
        assert run("relay", "run_eventhub.py", ["relay", "--once"])["published"] >= 1
        assert (
            run(
                "maintenance",
                "run_eventhub.py",
                ["redrive", "--event-id", event_id, "--group", "world"],
            )["requeued"]
            == 1
        )
        assert run("relay", "run_eventhub.py", ["relay", "--once"])["published"] >= 1
        assert run("relay", "run_eventhub.py", ["status"])["redis"] == "AVAILABLE"
        assert "requeued" in run("relay", "run_eventhub.py", ["repair"])
        for group in ("model", "world", "compute", "recovery", "log", "statistics"):
            assert run("worker", "run_processor.py", ["--group", group, "--once"])["group"] == group
        with Redis.from_url(urls["worker"]) as worker, pytest.raises(NoPermissionError):
            worker.xadd("nf:test:acl", {"test": "denied"})
        with rel_env["db"].uow() as repo:
            repo.conn.execute(
                text(
                    "UPDATE nf_bus_publications SET created_at=now()-interval '1 day' WHERE event_id=:id"
                ),
                {"id": event_id},
            )
        assert (
            run("maintenance", "run_eventhub.py", ["trim", "--retention-seconds", "1"])["trimmed"]
            >= 1
        )
    finally:
        proc.terminate()
        proc.wait(timeout=10)
