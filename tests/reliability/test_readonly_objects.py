"""Actual NTFS recovery of historical POSIX 0444 objects, without model calls."""
import multiprocessing
import os
import stat
import threading
import time

import pytest

from novel_factory.infrastructure.native_files import durable_reader
from novel_factory.infrastructure.objects import ObjectStore, ObjectStoreError

pytestmark = pytest.mark.skipif(os.name != "nt", reason="NTFS readonly recovery")


def _readonly(path):
    import win32con
    import win32file
    return bool(win32file.GetFileAttributes(str(path)) & win32con.FILE_ATTRIBUTE_READONLY)


def _parallel_verify(root, digest, start, results):
    try:
        store = ObjectStore(root)
        assert start.wait(10)
        for _ in range(12):
            with store.lock():
                store.verify(digest)
        results.put(None)
    except BaseException as error:  # noqa: BLE001 - child must report failure to its parent.
        results.put(repr(error))


def _overlapping_reader(root, digest, entered, release, results):
    try:
        store = ObjectStore(root)
        with store.lock(), durable_reader(store.path(digest),
                readonly_lock=store.temporary / ("durability-" + digest + ".lock")) as stream:
            entered.put(True)
            assert release.wait(10)
            assert stream.read() == b"synthetic shared object"
            os.fsync(stream.fileno())
        results.put(None)
    except BaseException as error:  # noqa: BLE001 - child must report failure to its parent.
        results.put(repr(error))


def test_readonly_verify_really_flushes_and_preserves_bytes(tmp_path, monkeypatch):
    store = ObjectStore(tmp_path)
    content = b"synthetic immutable recovery object"
    ref = store.put(content)
    path = store.path(ref.sha256)
    os.chmod(path, stat.S_IREAD)
    original = os.fsync
    flushed = []

    def fsync(descriptor):
        assert _readonly(path)  # Restoration precedes both hashing and durability acknowledgement.
        original(descriptor)  # A read-only Windows OS handle would actually fail here.
        flushed.append(os.fstat(descriptor).st_size)

    monkeypatch.setattr(os, "fsync", fsync)
    try:
        with store.lock():
            assert store.verify(ref.sha256, expected_size=len(content)) == ref
        assert store.read(ref.sha256) == content
        assert flushed == [len(content), len(content)]
        assert _readonly(path)
    finally:
        os.chmod(path, stat.S_IWRITE)


@pytest.mark.parametrize("failure", ["hash", "size", "flush", "restore"])
def test_readonly_failure_never_acknowledges_and_releases_handles(tmp_path, monkeypatch, failure):
    import win32con
    import win32file

    store = ObjectStore(tmp_path)
    ref = store.put(b"synthetic failure object")
    path = store.path(ref.sha256)
    if failure == "hash":
        path.write_bytes(b"corruption")
    os.chmod(path, stat.S_IREAD)
    original_attributes = win32file.SetFileAttributes
    restore_calls = []
    if failure == "flush":
        def failing_fsync(_):
            raise OSError("synthetic flush failure")
        monkeypatch.setattr(os, "fsync", failing_fsync)
    elif failure == "restore":
        def fail_first_restore(target, attributes):
            if attributes & win32con.FILE_ATTRIBUTE_READONLY:
                restore_calls.append(attributes)
                if len(restore_calls) == 1:
                    raise OSError("synthetic restoration failure")
            original_attributes(target, attributes)
        monkeypatch.setattr(win32file, "SetFileAttributes", fail_first_restore)
    try:
        expected = ObjectStoreError if failure in {"hash", "size"} else OSError
        with pytest.raises(expected), store.lock():
            store.verify(ref.sha256, expected_size=1 if failure == "size" else None)
        assert _readonly(path)
        if failure == "restore":
            assert len(restore_calls) == 2  # Finally retries restoration, but failure still propagates.
        # No leaked control/CRT handles: deletion/rename needs FILE_SHARE_DELETE.
        os.chmod(path, stat.S_IWRITE)
        moved = path.with_name("released")
        path.rename(moved)
        moved.rename(path)
    finally:
        original_attributes(str(path), win32con.FILE_ATTRIBUTE_NORMAL)


def test_writable_readers_overlap_across_processes(tmp_path):
    store = ObjectStore(tmp_path)
    ref = store.put(b"synthetic shared object")
    ctx = multiprocessing.get_context("spawn")
    entered, results, release = ctx.Queue(), ctx.Queue(), ctx.Event()
    processes = [ctx.Process(target=_overlapping_reader,
        args=(str(tmp_path), ref.sha256, entered, release, results)) for _ in range(2)]
    try:
        for process in processes:
            process.start()
        # Both handles must coexist; serializing ordinary reads would not reach this point.
        assert entered.get(timeout=15)
        assert entered.get(timeout=15)
        release.set()
        assert [results.get(timeout=15) for _ in processes] == [None, None]
    finally:
        release.set()
        for process in processes:
            process.join(timeout=15)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
    assert [process.exitcode for process in processes] == [0, 0]
    assert list(store.temporary.iterdir()) == []


@pytest.mark.parametrize("readonly", [False, True])
def test_actual_cross_process_concurrent_verification(tmp_path, readonly):
    store = ObjectStore(tmp_path)
    ref = store.put(b"synthetic shared object")
    path = store.path(ref.sha256)
    if readonly:
        os.chmod(path, stat.S_IREAD)
    ctx = multiprocessing.get_context("spawn")
    start, results = ctx.Event(), ctx.Queue()
    processes = [ctx.Process(target=_parallel_verify,
        args=(str(tmp_path), ref.sha256, start, results)) for _ in range(4)]
    try:
        for process in processes:
            process.start()
        start.set()
        assert [results.get(timeout=30) for _ in processes] == [None] * len(processes)
        assert _readonly(path) is readonly
        assert path.read_bytes() == b"synthetic shared object"
    finally:
        for process in processes:
            process.join(timeout=10)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        os.chmod(path, stat.S_IWRITE)
    assert [process.exitcode for process in processes] == [0] * len(processes)


def test_readonly_verification_does_not_upgrade_store_lock(tmp_path):
    store = ObjectStore(tmp_path)
    ref = store.put(b"synthetic referenced object")
    path = store.path(ref.sha256)
    os.chmod(path, stat.S_IREAD)
    entered, finished = threading.Event(), threading.Event()
    errors = []

    def collect():
        entered.set()
        try:
            assert store.collect(lambda: {ref.sha256}, grace_seconds=0,
                now=time.time() + 1) == {"objects": [], "temporary": []}
        except BaseException as error:  # noqa: BLE001 - thread must report failure to its parent.
            errors.append(error)
        finally:
            finished.set()

    thread = threading.Thread(target=collect, daemon=True)
    try:
        with store.lock():
            thread.start()
            assert entered.wait(3)
            assert not finished.wait(0.05)
            assert store.verify(ref.sha256) == ref
            assert _readonly(path)
        thread.join(timeout=5)
        assert finished.is_set()
        assert errors == []
        assert _readonly(path)
    finally:
        os.chmod(path, stat.S_IWRITE)
