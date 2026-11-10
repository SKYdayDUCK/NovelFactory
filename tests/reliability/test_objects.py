"""P1-03 / F10: bytes, durability, process crashes and GC fencing."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import multiprocessing
import os
import stat
import time

import pytest

from novel_factory.infrastructure.objects import ObjectStore, ObjectStoreError


def _killed_writer(root, stage):
    store = ObjectStore(root)
    def crash(point):
        if point == stage:
            os._exit(71)
    store.put(b"a persisted object", fault=crash)


def test_content_addressing_and_parallel_puts(tmp_path):
    store = ObjectStore(tmp_path)
    with ThreadPoolExecutor(max_workers=8) as pool:
        refs = list(pool.map(lambda _: store.put(b"same bytes"), range(16)))
    assert len(set(refs)) == 1
    ref = refs[0]
    assert ref.sha256 == hashlib.sha256(b"same bytes").hexdigest()
    assert store.read(ref.sha256) == b"same bytes"
    assert store.inventory() == {ref.sha256: 10}


def test_corruption_and_missing_fail_closed(tmp_path):
    store = ObjectStore(tmp_path)
    ref = store.put(b"immutable")
    os.chmod(store.path(ref.sha256), 0o600)
    store.path(ref.sha256).write_bytes(b"corrupted")
    with pytest.raises(ObjectStoreError, match="verification failed"):
        store.read(ref.sha256)
    with pytest.raises(ObjectStoreError) as missing:
        store.verify("0" * 64)
    assert missing.value.code == "OBJECT_MISSING"
    with pytest.raises(ObjectStoreError):
        store.path("../escape")


@pytest.mark.parametrize("stage", ["after_write", "after_file_fsync", "after_publish"])
def test_abrupt_process_exit_recovery(tmp_path, stage):
    p = multiprocessing.get_context("spawn").Process(target=_killed_writer, args=(str(tmp_path), stage))
    p.start()
    p.join(timeout=10)
    assert p.exitcode == 71
    store = ObjectStore(tmp_path)
    # There can be a durable orphan after publish, never a database reference.
    result = store.collect(lambda: set(), grace_seconds=0, now=time.time() + 1)
    assert list(store.temporary.iterdir()) == []
    assert store.inventory() == {}
    ref = store.put(b"a persisted object")
    assert store.read(ref.sha256) == b"a persisted object"


def test_gc_keeps_referenced_and_young_objects(tmp_path):
    store = ObjectStore(tmp_path)
    keep = store.put(b"referenced")
    orphan = store.put(b"orphan")
    assert store.collect(lambda: {keep.sha256}, grace_seconds=3600)["objects"] == []
    assert store.collect(lambda: {keep.sha256}, grace_seconds=0, now=time.time() + 1)["objects"] == [orphan.sha256]
    assert store.read(keep.sha256) == b"referenced"


def test_reference_transaction_excludes_gc(tmp_path):
    import threading
    store = ObjectStore(tmp_path)
    ref = store.put(b"in-flight reference")
    references = set()
    entered = threading.Event()
    finished = threading.Event()
    def collect():
        entered.set()
        store.collect(lambda: set(references), grace_seconds=0, now=time.time() + 1)
        finished.set()
    with store.lock():
        thread = threading.Thread(target=collect)
        thread.start()
        assert entered.wait(2)
        assert not finished.wait(0.05)
        # Simulated callback data only for filesystem lock proof; PG reference
        # integration is independently covered in database tests.
        references.add(ref.sha256)
    thread.join(timeout=3)
    assert finished.is_set()
    assert store.read(ref.sha256) == b"in-flight reference"


@pytest.mark.parametrize("operation", ["verify", "put"])
def test_reuse_after_publish_crash_reconfirms_durable_directory_entry(tmp_path, monkeypatch, operation):
    process = multiprocessing.get_context("spawn").Process(target=_killed_writer, args=(str(tmp_path), "after_publish"))
    process.start()
    process.join(timeout=10)
    assert process.exitcode == 71
    store = ObjectStore(tmp_path)
    digest = hashlib.sha256(b"a persisted object").hexdigest()
    path = store.path(digest)
    assert path.is_file()
    # The killed writer linked the name but did not sync its directory. A
    # second process must not equate visible bytes with durable publication.
    synced = set()
    original = os.fsync
    def fsync(fd):
        info = os.fstat(fd)
        synced.add((stat.S_IFMT(info.st_mode), info.st_ino))
        original(fd)
    monkeypatch.setattr(os, "fsync", fsync)
    with store.lock():
        ref = store.verify(digest) if operation == "verify" else store.put(b"a persisted object")
    assert ref.sha256 == digest
    for target in (path, path.parent, store.objects):
        info = target.stat()
        assert (stat.S_IFMT(info.st_mode), info.st_ino) in synced
