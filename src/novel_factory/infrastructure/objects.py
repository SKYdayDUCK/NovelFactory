"""Durable content-addressed objects for the single-host P1 deployment.

Writers and reference transactions hold a shared process-safe filesystem lock.
Garbage collection holds the exclusive lock while consulting committed database
references.  This lock order (file lock, then database maintenance gate) is part
of the ObjectStore port and must be preserved by callers.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Callable, Iterable, Iterator

_HASH = re.compile(r"^[0-9a-f]{64}$")


class ObjectStoreError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = False


@dataclass(frozen=True)
class ObjectRef:
    sha256: str
    size: int

    def as_dict(self) -> dict:
        return {"sha256": self.sha256, "size": self.size}


class ObjectStore:
    """Atomic, non-overwriting SHA-256 store on a local POSIX filesystem."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.objects = self.root / "sha256"
        self.temporary = self.root / "tmp"
        self.objects.mkdir(exist_ok=True)
        self.temporary.mkdir(exist_ok=True)
        self._lock_path = self.root / ".store.lock"
        # Ensure directory entries exist durably before accepting any object.
        self._fsync_dir(self.root)
        self._fsync_dir(self.root.parent)

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    @contextmanager
    def lock(self, *, exclusive: bool = False) -> Iterator[None]:
        """Hold over both verification and the database reference commit.

        Do not nest exclusive/shared locks: each invocation owns a separate fd.
        """
        fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def path(self, digest: str) -> Path:
        if not isinstance(digest, str) or not _HASH.fullmatch(digest):
            raise ObjectStoreError("OBJECT_HASH_INVALID", "Invalid object hash")
        return self.objects / digest[:2] / digest[2:]

    def verify(self, digest: str, *, expected_size: int | None = None) -> ObjectRef:
        """Verify bytes and reconfirm durable publication before any reference.

        A competing or killed writer may have linked this name without syncing
        its directory yet. Visibility alone is therefore not a persistence
        acknowledgement. Reference callers hold the shared store lock until
        their database transaction commits.
        """
        p = self.path(digest)
        try:
            if p.is_symlink():
                raise ObjectStoreError("OBJECT_CORRUPT", "Object path is a symlink")
            size = 0
            h = hashlib.sha256()
            with p.open("rb") as f:
                for block in iter(lambda: f.read(1024 * 1024), b""):
                    h.update(block)
                    size += len(block)
                if h.hexdigest() != digest or (expected_size is not None and size != expected_size):
                    raise ObjectStoreError("OBJECT_CORRUPT", "Object content verification failed")
                os.fsync(f.fileno())
            self._fsync_dir(p.parent)
            self._fsync_dir(self.objects)
        except FileNotFoundError as exc:
            raise ObjectStoreError("OBJECT_MISSING", "Object is not durable") from exc
        return ObjectRef(digest, size)

    def put(self, content: bytes, *, fault: Callable[[str], None] | None = None) -> ObjectRef:
        if not isinstance(content, bytes):
            raise TypeError("ObjectStore.put requires bytes")
        digest = hashlib.sha256(content).hexdigest()
        dest = self.path(digest)
        with self.lock():
            if dest.exists():
                return self.verify(digest, expected_size=len(content))
            if not dest.parent.exists():
                dest.parent.mkdir(exist_ok=True)
                self._fsync_dir(self.objects)
            fd, temp_name = tempfile.mkstemp(prefix="upload-", dir=self.temporary)
            temp = Path(temp_name)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(content)
                    stream.flush()
                    if fault:
                        fault("after_write")
                    os.fchmod(stream.fileno(), 0o444)
                    os.fsync(stream.fileno())
                    if fault:
                        fault("after_file_fsync")
                # link is atomic and fails if another writer already published
                # the same content. Unlike replace, it never overwrites bytes.
                try:
                    os.link(temp, dest)
                except FileExistsError:
                    self.verify(digest, expected_size=len(content))
                if fault:
                    fault("after_publish")
                self._fsync_dir(dest.parent)
                result = self.verify(digest, expected_size=len(content))
                if fault:
                    fault("before_return")
                return result
            finally:
                temp.unlink(missing_ok=True)
                self._fsync_dir(self.temporary)

    def read(self, digest: str) -> bytes:
        with self.lock():
            self.verify(digest)
            return self.path(digest).read_bytes()

    def inventory(self) -> dict[str, int]:
        """Verified inventory; use a lock when concurrent GC is possible."""
        result = {}
        for directory in sorted(self.objects.iterdir()):
            if not directory.is_dir() or directory.is_symlink():
                continue
            for p in sorted(directory.iterdir()):
                digest = directory.name + p.name
                if _HASH.fullmatch(digest):
                    result[digest] = self.verify(digest).size
        return result

    def collect(
        self,
        referenced_hashes: Callable[[], Iterable[str]],
        *,
        grace_seconds: float = 3600,
        now: float | None = None,
    ) -> dict[str, list[str]]:
        """Delete old orphans while preventing put/reference/GC races.

        The callback MUST read current committed authoritative references after
        this method takes the exclusive file lock. Reference registration MUST
        hold this store's shared lock until its database transaction commits.
        All candidate and formal references (including task results) must be
        included. Temporary files left by killed processes are cleaned too.
        """
        if grace_seconds < 0:
            raise ValueError("GC grace period cannot be negative")
        cutoff = (time.time() if now is None else now) - grace_seconds
        removed, temporary = [], []
        with self.lock(exclusive=True):
            referenced = set(referenced_hashes())
            for digest in self.inventory():
                p = self.path(digest)
                if digest not in referenced and p.stat().st_mtime <= cutoff:
                    p.unlink()
                    self._fsync_dir(p.parent)
                    removed.append(digest)
            for p in self.temporary.iterdir():
                if p.is_file() and p.name.startswith("upload-") and p.stat().st_mtime <= cutoff:
                    p.unlink()
                    temporary.append(p.name)
            self._fsync_dir(self.temporary)
        return {"objects": removed, "temporary": temporary}
