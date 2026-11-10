"""Isolated PostgreSQL fixtures; no SQLite or in-memory DB fallback."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, insert, select, text
from sqlalchemy.engine import make_url

from novel_factory.infrastructure.database import Database, tables
from novel_factory.infrastructure.objects import ObjectStore
from novel_factory.orchestration.tasks import TaskService, mutation_gate

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def rel_env(tmp_path_factory):
    url = os.environ.get("NF_TEST_DATABASE_URL")
    if not url:
        pytest.fail("Real PostgreSQL required: run scripts/with_postgres.py --migrate -- pytest tests/reliability")
    dbname = "nf_rel_" + uuid4().hex[:16]
    admin = create_engine(make_url(url).set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        assert int(conn.scalar(text("SHOW server_version_num"))) // 10000 == 17
        conn.execute(text(f'CREATE DATABASE "{dbname}"'))
    target = make_url(url).set(database=dbname).render_as_string(hide_password=False)
    env = {**os.environ, "NF_DATABASE_URL": target, "DATABASE_URL": target}
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT, env=env, check=True)
    db = Database(target, pool_size=8)
    store = ObjectStore(tmp_path_factory.mktemp("reliability_objects"))
    service = TaskService(db.engine, tables, store)
    yield {"db": db, "store": store, "service": service, "url": target, "admin": admin, "tables": tables}
    db.close()
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE "{dbname}" WITH (FORCE)'))
    admin.dispose()


@pytest.fixture
def rel_scope(rel_env):
    # The task mechanics fixture is a minimal *real* SQL work/branch/revision.
    # Full QingShuang/BaiQiao domain histories are tested by kernel suites.
    ids = {k: str(uuid4()) for k in ("work_id", "branch_id", "world_revision_id", "principal_id", "grant_id")}
    artifact = {"artifact_id": str(uuid4()), "artifact_revision_id": str(uuid4()), "sha256": "0" * 64}
    with rel_env["db"].uow() as repo:
        repo.add("works", {"work_id": ids["work_id"], "name": "isolated reliable-task fixture", "policy_version": "policy-test", "policy_ref": artifact, "rules_ref": artifact, "interpretation_ref": artifact})
        repo.add("branches", {"work_id": ids["work_id"], "branch_id": ids["branch_id"], "kind": "CANON"})
        repo.add("revisions", {"work_id": ids["work_id"], "branch_id": ids["branch_id"], "world_revision_id": ids["world_revision_id"], "branch_seq": 0, "selection": [], "frontiers": {}, "scope_versions": {}, "snapshot": {}, "interpretation_ref": artifact, "manifest_hash": "0" * 64, "state_hash": "0" * 64, "provenance": {"kind": "isolated_task_fixture"}})
        repo.change("branches", {"branch_id": ids["branch_id"]}, {"head_revision_id": ids["world_revision_id"]})
        repo.change("works", {"work_id": ids["work_id"]}, {"canon_branch_id": ids["branch_id"]})
        repo.add("grants", {"grant_id": ids["grant_id"], "principal_id": ids["principal_id"], "work_id": ids["work_id"], "token_hash": hashlib.sha256(uuid4().bytes).hexdigest(), "role_id": "writer", "branch_ids": [ids["branch_id"]], "capabilities": ["TASK_EXECUTE"], "planes": ["WORLD"], "max_story_time": {"tick": 999, "phase": 0}})
    return ids


def task_spec(scope, *, kind="scene_draft"):
    data = json.loads((ROOT / "contracts/examples/task_spec.json").read_text())
    data.update(task_id=str(uuid4()), generation=0, context_bundle_id=None, dependency_fingerprint=None,
                state="READY", task_kind=kind, capability_grant_id=scope["grant_id"],
                world_ref={key: scope[key] for key in ("work_id", "branch_id", "world_revision_id")},
                wall_deadline=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
    data["budget"]["max_amount"] = "20.00"
    data["input_artifacts"] = []
    return data


def start_task(env, scope, *, kind="scene_draft", worker="worker", bind=True):
    spec = task_spec(scope, kind=kind)
    service = env["service"]
    service.create(spec, principal_id=scope["principal_id"], idempotency_key=spec["task_id"])
    task = service.claim(worker, task_id=spec["task_id"])
    assert task and task["generation"] == 1
    if bind:
        bind_task(env, task, scope, worker=worker)
        task = service.get(task["task_id"])
    return task


def bind_task(env, task, scope, *, worker="worker"):
    bundle_id = str(uuid4())
    dependency = hashlib.sha256(task["task_id"].encode()).hexdigest()
    payload = json.loads((ROOT / "contracts/examples/context_bundle.json").read_text())
    payload.update(task_id=task["task_id"], generation=task["generation"], context_bundle_id=bundle_id,
                   world_ref=task["world_ref"], dependency_fingerprint=dependency, role_id="writer", actor_id=None,
                   facts=[], limitations=[])
    ref = env["store"].put(json.dumps(payload).encode())
    artifact = {"artifact_id": str(uuid4()), "artifact_revision_id": str(uuid4()), "sha256": ref.sha256}
    with env["store"].lock(), env["db"].uow() as repo:
        repo.add("objects", {"hash": ref.sha256, "size": ref.size}, ignore_conflict=True)
        repo.add("object_refs", {"owner_type": "context", "owner_id": bundle_id, "object_hash": ref.sha256})
        repo.add("contexts", {"context_bundle_id": bundle_id, "work_id": scope["work_id"], "task_id": task["task_id"], "generation": task["generation"], "principal_id": scope["principal_id"], "grant_id": scope["grant_id"], "payload": payload, "dependency_manifest": {}, "artifact_ref": artifact, "read_cut": {}, "dependency_fingerprint": dependency})
        env["service"].bind_context_in_transaction(repo.conn, task["task_id"], task["generation"], worker,
            context_bundle_id=bundle_id, dependency_fingerprint=dependency, world_ref=task["world_ref"])
    return bundle_id


def task_result(task):
    return {"schema_version": "2.0", "task_id": task["task_id"], "generation": task["generation"],
            "attempt_id": task["attempt_id"], "result_id": str(uuid4()), "context_bundle_id": task["context_bundle_id"],
            "dependency_fingerprint": task["dependency_fingerprint"], "status": "SUCCEEDED", "artifacts": [],
            "safe_summary": "Isolated output stored; no world effects accepted."}
