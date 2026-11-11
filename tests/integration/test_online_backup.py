"""P4-02: concurrent writes, a fixed backup cut, real restore and missing objects."""
from concurrent.futures import ThreadPoolExecutor
import threading
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url

from novel_factory.application.kernel import Kernel
from novel_factory.fixtures import load_fixture, point, run_command
from novel_factory.infrastructure.database import Database, tables
from novel_factory.infrastructure.objects import ObjectStore, ObjectStoreError
from novel_factory.infrastructure.online_backup import OnlineBackupService

pytestmark = [pytest.mark.postgres, pytest.mark.integration, pytest.mark.recovery]


def test_online_backup_restores_exact_cut_without_blocking_world_writers(kernel_env, tmp_path, monkeypatch):
    kernel = kernel_env["kernel"]
    fixture = load_fixture(kernel)
    old = fixture["world_ref"]
    backup = OnlineBackupService(kernel.db.engine, kernel.objects)
    original = backup._tool
    snapshot_ready, writer_done = threading.Event(), threading.Event()

    def dump_after_concurrent_write(name, args, url):
        if name == "pg_dump":
            assert any(arg.startswith("--snapshot=") for arg in args)
            snapshot_ready.set()
            assert writer_done.wait(15), "Online snapshot must not hold the exclusive writer gate"
        return original(name, args, url)

    monkeypatch.setattr(backup, "_tool", dump_after_concurrent_write)
    with ThreadPoolExecutor(max_workers=1) as pool:
        capture = pool.submit(backup.create, tmp_path / "backup")
        assert snapshot_ready.wait(10)
        try:
            accepted = run_command(kernel, fixture["principal"], old, "TRANSFER_ITEM", {
                "item_id": fixture["ids"]["sword"], "from_id": fixture["ids"]["smith"],
                "to_id": fixture["ids"]["protagonist"], "transfer_kind": "GIFT", "at": point(10)})
        finally:
            writer_done.set()
        manifest = capture.result(30)
    assert manifest["mode"] == "ONLINE_EXPORTED_SNAPSHOT"
    assert accepted["world_ref"] != old
    name = "nf_online_restore_" + uuid4().hex[:12]
    with kernel_env["admin"].connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    target = make_url(kernel_env["url"]).set(database=name).render_as_string(hide_password=False)
    database = None
    try:
        verified = backup.restore(tmp_path / "backup", target, tmp_path / "restored", tables=tables)
        assert verified["snapshot_equal_before_fencing"]
        database = Database(target)
        restored = Kernel(database, ObjectStore(tmp_path / "restored"))
        assert restored.authenticate(fixture["token"]) == fixture["principal"]
        with restored.db.uow(write=False) as repo:
            assert restored.head(repo, old["work_id"]) == old
            assert not repo.many("events", {"work_id": old["work_id"]})
        assert manifest["objects"]
        digest = next(iter(manifest["objects"]))
        ObjectStore(tmp_path / "backup" / "objects").path(digest).unlink()
        with pytest.raises(ObjectStoreError) as missing:
            backup.verify(tmp_path / "backup")
        assert missing.value.code == "OBJECT_MISSING"
    finally:
        if database:
            database.close()
        with kernel_env["admin"].connect() as conn:
            conn.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
