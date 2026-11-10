"""P1-07/F10/F11/V2-12: real pg_dump/pg_restore and process failures."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import multiprocessing
import os
from pathlib import Path
import threading
import time
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url

from novel_factory.domain.errors import DomainError
from novel_factory.infrastructure.database import Database, tables
from novel_factory.infrastructure.objects import ObjectStore, ObjectStoreError
from novel_factory.infrastructure.recovery import BackupService, database_snapshot
from novel_factory.orchestration.tasks import TaskService, OutboxService, enqueue_outbox
from .conftest import start_task, task_spec

pytestmark = [pytest.mark.postgres, pytest.mark.recovery]


def _db_crash(url, budget_id):
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("UPDATE nf_budgets SET limit_amount=999 WHERE budget_id=:bid"), {"bid": budget_id})
        os._exit(72)


def _create_and_lose_receipt(url, object_root, spec, principal):
    engine = create_engine(url)
    service = TaskService(engine, tables, ObjectStore(object_root))
    service.create(spec, principal_id=principal, idempotency_key="process-lost-receipt")
    os._exit(73)


def _new_target(rel_env):
    name = "nf_restore_" + uuid4().hex[:16]
    with rel_env["admin"].connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    return name, make_url(rel_env["url"]).set(database=name).render_as_string(hide_password=False)


def _drop_target(rel_env, name):
    with rel_env["admin"].connect() as conn:
        conn.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))


def _evidence(name, payload):
    base = Path(os.environ.get("NF_EVIDENCE_DIR", Path(__file__).resolve().parents[3] / "evidence" / "recovery_tests"))
    base.mkdir(parents=True, exist_ok=True)
    (base / f"{name}.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n")


@pytest.mark.fault
def test_abrupt_database_process_exit_rolls_back(rel_env, rel_scope):
    budget = rel_env["service"].create_budget(rel_scope["work_id"], "12")
    proc = multiprocessing.get_context("spawn").Process(target=_db_crash, args=(rel_env["url"], budget))
    proc.start()
    proc.join(timeout=10)
    assert proc.exitcode == 72
    with rel_env["db"].engine.connect() as conn:
        assert conn.scalar(select(tables["budgets"].c.limit_amount).where(tables["budgets"].c.budget_id == budget)) == 12


@pytest.mark.fault
def test_committed_task_process_exit_keeps_original_receipt(rel_env, rel_scope):
    spec = task_spec(rel_scope)
    proc = multiprocessing.get_context("spawn").Process(target=_create_and_lose_receipt,
        args=(rel_env["url"], str(rel_env["store"].root), spec, rel_scope["principal_id"]))
    proc.start()
    proc.join(timeout=10)
    assert proc.exitcode == 73
    receipt = rel_env["service"].create(spec, principal_id=rel_scope["principal_id"], idempotency_key="process-lost-receipt")
    assert receipt["task_id"] == spec["task_id"] and receipt["generation"] == 0
    with rel_env["db"].engine.connect() as conn:
        assert conn.scalar(select(func.count()).select_from(tables["tasks"]).where(tables["tasks"].c.task_id == spec["task_id"])) == 1


def test_pg_dump_restore_checks_data_objects_and_fences_old_workers(rel_env, rel_scope, tmp_path):
    service = rel_env["service"]
    task = start_task(rel_env, rel_scope)
    budget = service.create_budget(rel_scope["work_id"], "5")
    call = service.reserve_call(task["task_id"], 1, "worker", budget_id=budget, amount="2", call_key="restore-unknown", request_hash="4" * 64)
    service.mark_call_sent(call["attempt_id"], worker_id="worker")
    outbox = OutboxService(rel_env["db"].engine, tables)
    with rel_env["db"].engine.begin() as conn:
        message = enqueue_outbox(conn, tables, "restore.only", "task", task["task_id"], {"task_id": task["task_id"]}, f"restore:{uuid4()}")
    lease = outbox.claim("old-dispatch", topic="restore.only")
    backup = BackupService(rel_env["db"].engine, rel_env["store"])
    manifest = backup.create(tmp_path / "backup")
    assert manifest["status"] == "COMPLETE"
    assert manifest["objects"]
    assert manifest["database_snapshot"]["nf_revisions"]["rows"] >= 1
    name, target_url = _new_target(rel_env)
    restored = None
    try:
        report = backup.restore(tmp_path / "backup", target_url, tmp_path / "restored_objects", tables=tables)
        assert report["snapshot_equal_before_fencing"] and report["interpretation_verified"]
        restored = Database(target_url)
        target_store = ObjectStore(tmp_path / "restored_objects")
        target_service = TaskService(restored.engine, tables, target_store)
        after = target_service.get(task["task_id"])
        assert after["state"] == "PAUSED" and after["generation"] > task["generation"]
        assert after["world_ref"] == task["world_ref"]
        with restored.engine.begin() as conn, pytest.raises(DomainError) as exc:
            target_service.guard_task(conn, task["task_id"], task["generation"])
        assert exc.value.code == "STALE_GENERATION"
        with restored.engine.connect() as conn:
            ledger = conn.execute(select(tables["call_attempts"]).where(tables["call_attempts"].c.attempt_id == call["attempt_id"])).mappings().one()
            assert ledger["state"] == "UNKNOWN"
            balance = conn.execute(select(tables["budgets"]).where(tables["budgets"].c.budget_id == budget)).mappings().one()
            assert balance["reserved_amount"] == 2 and balance["spent_amount"] == 0
            revision = conn.execute(select(tables["revisions"]).where(tables["revisions"].c.world_revision_id == task["world_ref"]["world_revision_id"])).mappings().one()
            assert revision["provenance"]["kind"] == "isolated_task_fixture"
        with pytest.raises(DomainError):
            OutboxService(restored.engine, tables).ack(message, lease["generation"], "old-dispatch")
        for digest, size in manifest["objects"].items():
            assert target_store.verify(digest).size == size
        _evidence("isolated_restore", {"source_postgres": manifest["postgres_version"], "backup": manifest,
                                       "restoration": report, "business_assertions": ["same immutable WorldRef", "old workers fenced", "unknown calls retain budget", "outbox lease cannot acknowledge", "object bytes/hash verified"]})
        with pytest.raises(DomainError) as exc:
            backup.restore(tmp_path / "backup", target_url, tmp_path / "another_root", tables=tables)
        assert exc.value.code == "RESTORE_TARGET_NOT_EMPTY"
    finally:
        if restored:
            restored.close()
        _drop_target(rel_env, name)


def test_backup_rejects_missing_object_and_interpretation(rel_env, rel_scope, tmp_path):
    start_task(rel_env, rel_scope)
    backup = BackupService(rel_env["db"].engine, rel_env["store"])
    manifest = backup.create(tmp_path / "backup")
    digest = next(iter(manifest["objects"]))
    store = ObjectStore(tmp_path / "backup" / "objects")
    original = store.read(digest)
    store.path(digest).unlink()
    with pytest.raises(ObjectStoreError) as exc:
        backup.verify(tmp_path / "backup")
    assert exc.value.code == "OBJECT_MISSING"
    store.put(original)
    runtime_name = next(iter(manifest["runtime_files"]))
    (tmp_path / "backup" / "interpretation" / runtime_name).unlink()
    with pytest.raises(DomainError) as exc:
        backup.verify(tmp_path / "backup")
    assert exc.value.code == "INTERPRETATION_UNAVAILABLE"


@pytest.mark.concurrency
def test_backup_gate_freezes_authoritative_mutations(rel_env, rel_scope, tmp_path, monkeypatch):
    backup = BackupService(rel_env["db"].engine, rel_env["store"])
    original_tool = backup._tool
    frozen, release = threading.Event(), threading.Event()
    def block_dump(name, args, url):
        if name == "pg_dump":
            frozen.set()
            assert release.wait(timeout=10)
        return original_tool(name, args, url)
    monkeypatch.setattr(backup, "_tool", block_dump)
    with ThreadPoolExecutor(max_workers=2) as pool:
        capture = pool.submit(backup.create, tmp_path / "backup")
        assert frozen.wait(timeout=5)
        mutation = pool.submit(rel_env["service"].create_budget, rel_scope["work_id"], "3")
        deadline = time.monotonic() + 5
        observed_wait = False
        while time.monotonic() < deadline:
            with rel_env["db"].engine.connect() as conn:
                observed_wait = bool(conn.scalar(text("SELECT EXISTS(SELECT 1 FROM pg_locks WHERE locktype='advisory' AND NOT granted)")))
            if observed_wait:
                break
            time.sleep(0.01)
        try:
            assert observed_wait and not mutation.done()
        finally:
            release.set()
        manifest = capture.result(timeout=10)
        new_budget = mutation.result(timeout=10)
    # Snapshot was taken before the blocked writer; it is a coherent cutoff.
    with rel_env["db"].engine.connect() as conn:
        current_count = conn.scalar(select(func.count()).select_from(tables["budgets"]))
    assert manifest["database_snapshot"]["nf_budgets"]["rows"] + 1 == current_count
    _evidence("backup_gate", {"observed_postgres_advisory_wait": observed_wait,
        "frozen_budget_count": manifest["database_snapshot"]["nf_budgets"]["rows"], "post_resume_budget_count": current_count})



def test_backup_runtime_profile_is_required_and_bound(rel_env, rel_scope, tmp_path, monkeypatch):
    import shutil
    backup = BackupService(rel_env["db"].engine, rel_env["store"])
    manifest = backup.create(tmp_path / "backup")
    assert "runtime/query_profiles.json" in manifest["runtime_assets"]
    assert manifest["runtime_asset_version"] == 2
    assert "runtime/roles/character_actor.md" in manifest["runtime_assets"]
    assert "runtime/skills/simulate-character/SKILL.md" in manifest["runtime_assets"]
    saved = tmp_path / "backup" / "runtime_assets" / "runtime" / "query_profiles.json"
    original = saved.read_bytes()
    saved.unlink()
    with pytest.raises(DomainError) as exc:
        backup.verify(tmp_path / "backup")
    assert exc.value.code == "INTERPRETATION_UNAVAILABLE"
    saved.write_bytes(original)
    role = tmp_path / "backup" / "runtime_assets" / "runtime" / "roles" / "character_actor.md"
    original_role = role.read_bytes()
    role.unlink()
    with pytest.raises(DomainError) as exc:
        backup.verify(tmp_path / "backup")
    assert exc.value.code == "INTERPRETATION_UNAVAILABLE"
    role.write_bytes(original_role)
    changed = tmp_path / "changed_profile.json"
    changed.write_bytes(original + b"\n")
    monkeypatch.setattr(backup, "_runtime_assets", lambda: {"runtime/query_profiles.json": changed})
    with pytest.raises(DomainError) as exc:
        backup.verify(tmp_path / "backup")
    assert exc.value.code == "INTERPRETATION_UNAVAILABLE"


def test_backup_resolves_wheel_runtime_assets(rel_env, tmp_path):
    package = tmp_path / "site-packages" / "novel_factory"
    schemas = package / "data" / "contracts"
    profile = package / "data" / "runtime" / "query_profiles.json"
    schemas.mkdir(parents=True)
    profile.parent.mkdir(parents=True)
    profile.write_text('{"profiles": []}')
    for kind in ("roles", "skills"):
        catalog = profile.parent / kind / "catalog.json"
        catalog.parent.mkdir()
        catalog.write_text('{}')
    backup = BackupService(rel_env["db"].engine, rel_env["store"], package_root=package)
    assert backup._schema_directory() == schemas
    assert backup._runtime_assets()["runtime/query_profiles.json"] == profile
