"""HTTP tests use a migrated PostgreSQL 17 database and real application services."""
from __future__ import annotations

from copy import deepcopy
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from novel_factory.application.kernel import Kernel
from novel_factory.application.queries import WorldQueryService
from novel_factory.fixtures import load_fixture, create_bound_task, point
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.objects import ObjectStore

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def service_env(tmp_path_factory):
    url = os.environ.get("NF_TEST_DATABASE_URL")
    if not url:
        pytest.fail("Real PostgreSQL required; run scripts/with_postgres.py -- .venv/bin/python -m pytest tests/service")
    name = "nf_http_" + uuid4().hex[:16]
    admin = create_engine(make_url(url).set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        assert int(connection.scalar(text("SHOW server_version_num"))) // 10000 == 17
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    target = make_url(url).set(database=name).render_as_string(hide_password=False)
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT,
        env={**os.environ, "NF_DATABASE_URL": target, "DATABASE_URL": target}, check=True)
    db = Database(target, pool_size=8)
    kernel = Kernel(db, ObjectStore(tmp_path_factory.mktemp("http_objects")))
    yield {"db": db, "kernel": kernel, "url": target}
    db.close()
    with admin.connect() as connection:
        connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
    admin.dispose()


@pytest.fixture
def service_world(service_env):
    return load_fixture(service_env["kernel"], "r1_sword")


def make_client(env, world, *, actor=False):
    kernel = env["kernel"]
    issued = kernel.issue_grant(world["principal"], world["world_ref"]["work_id"],
        role_id="character_actor" if actor else "adjudication_advisor",
        actor_id=world["ids"]["protagonist"] if actor else None,
        branch_ids=[world["world_ref"]["branch_id"]],
        capabilities=["READ", "COMPILE", "TASK_CREATE", "TASK_CLAIM", "ARTIFACT_WRITE"],
        planes=["BELIEF"] if actor else ["WORLD"], max_story_time=point(42))
    task = create_bound_task(kernel, issued["principal"], world["world_ref"], point(1),
        [world["ids"]["sword"]], ["owner", "active_holder"],
        plane="BELIEF" if actor else "WORLD", actor_id=world["ids"]["protagonist"] if actor else None)
    cut = {"plane": "BELIEF", "actor_id": world["ids"]["protagonist"], "known_at": point(1)} if actor else {"plane": "WORLD"}
    request = {"schema_version": "2.1", "task_id": task["task_id"], "generation": task["generation"],
        "read_contract": {"revision": {"mode": "FIXED", "world_ref": world["world_ref"]},
                          "process_policy": "REQUIRE_EVALUATED", "index_policy": "AUTHORITATIVE", "max_wait_ms": 0},
        "knowledge_cut": cut, "subject_time": {"kind": "POINT", "at": point(1)},
        "query": {"kind": "STATE", "subject_ids": [world["ids"]["sword"]], "predicates": ["owner", "active_holder"]},
        "page_size": 50, "cursor": None}
    result = WorldQueryService(kernel).query(issued["principal"], request)
    if not actor:
        for item in result["payload"]["state_items"]:
            assert item["answer"]["data"] == {"kind": "ENTITY", "entity_id": world["ids"]["smith"]}
    return {"token": issued["token"], "principal": issued["principal"], "task": task,
            "request": request, "expected_payload": deepcopy(result["payload"])}
