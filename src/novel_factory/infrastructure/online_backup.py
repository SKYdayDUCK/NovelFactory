"""Online logical backup using one exported PostgreSQL MVCC snapshot.

World/task writers keep running. The shared object-store lock only excludes
garbage collection; immutable objects referenced by the snapshot are retained
until copied. This is intentionally distinct from a physical WAL/PITR backup.
"""
from __future__ import annotations

import os
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from sqlalchemy import text

from novel_factory.domain.errors import DomainError
from novel_factory.infrastructure.objects import ObjectStore
from novel_factory.infrastructure.recovery import (
    BackupService,
    _sha,
    _write_json,
    database_snapshot,
)


class OnlineBackupService(BackupService):
    """Compatible with the existing verified restore and task-fencing path."""

    def _interpretation_inventory(self):
        """Pin every source path and digest before copying any package file."""
        groups = {
            "interpretation": dict(self._runtime_files()),
            "schemas": {p.name: p for p in sorted(self._schema_directory().glob("*.schema.json"))},
            "runtime_assets": self._runtime_assets(),
        }
        try:
            return {folder: {relative: (source, _sha(source)) for relative, source in files.items()}
                    for folder, files in groups.items()}
        except FileNotFoundError as exc:
            raise DomainError("INTERPRETATION_CHANGED", "Runtime changed while taking its backup inventory") from exc

    def _copy_interpretation(self, destination, *, initial=None):
        initial = self._interpretation_inventory() if initial is None else initial
        hashes = {folder: {relative: digest for relative, (_, digest) in files.items()}
                  for folder, files in initial.items()}
        for folder, files in initial.items():
            for relative, (source, expected) in files.items():
                target = destination / folder / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.copyfile(source, target)
                except FileNotFoundError as exc:
                    raise DomainError("INTERPRETATION_CHANGED", "Runtime disappeared during backup") from exc
                with target.open("rb") as stream:
                    os.fsync(stream.fileno())
                if _sha(target) != expected:
                    raise DomainError("INTERPRETATION_CHANGED", "Runtime changed while backing up")
        # Immediate per-file checks cannot detect a deployment that changes an
        # already-copied module while a later schema or asset is being copied.
        # Check membership too: additions/removals can alter dynamic loading.
        final = self._interpretation_inventory()
        if hashes != {folder: {relative: digest for relative, (_, digest) in files.items()}
                      for folder, files in final.items()}:
            raise DomainError("INTERPRETATION_CHANGED", "Runtime package membership or bytes changed during backup")
        return {"runtime_files": hashes["interpretation"], "schemas": hashes["schemas"],
                "runtime_assets": hashes["runtime_assets"], "runtime_asset_version": 2}

    def create(self, destination: str | Path):
        self.validate_connection_target(self.engine.url)
        dest = Path(destination).resolve()
        if dest.exists():
            raise DomainError("BACKUP_DESTINATION_EXISTS", "Backup destination must be new")
        dest.mkdir(parents=True)
        ObjectStore._fsync_dir(dest.parent)
        started = time.monotonic()
        manifest = {"format": "novel-factory-logical-backup-1", "mode": "ONLINE_EXPORTED_SNAPSHOT",
                    "created_at": datetime.now(UTC).isoformat(),
                    "backup_id": str(uuid4()), "status": "INCOMPLETE"}
        _write_json(dest / "manifest.json", manifest)
        # Keep one installed package across the database cut as well as the
        # copy stage. A deployment during pg_dump must not silently pair an old
        # database snapshot with a newly installed interpretation package.
        initial_runtime = self._interpretation_inventory()
        # Never acquire the exclusive application maintenance gate here.
        with self.objects.lock():
            with (self.engine.connect().execution_options(isolation_level="REPEATABLE READ") as conn,
                  conn.begin()):
                conn.execute(text("SET TRANSACTION READ ONLY"))
                conn.execute(text("SET LOCAL TIME ZONE 'UTC'"))
                # A dedicated bounded backup transaction may outlive the
                # short-request pool's idle limit while pg_dump imports it.
                conn.execute(text("SET LOCAL idle_in_transaction_session_timeout = '5min'"))
                snapshot = conn.scalar(text("SELECT pg_export_snapshot()"))
                manifest["snapshot_at_utc"] = conn.scalar(text("SELECT clock_timestamp()" )).isoformat()
                manifest["postgres_version"] = conn.scalar(text("SHOW server_version"))
                manifest["database_snapshot"] = database_snapshot(conn)
                rows = conn.execute(text("SELECT hash, size FROM nf_objects ORDER BY hash")).mappings().all()
                dump = self._tool("pg_dump", ["--format=custom", "--no-owner", "--no-acl",
                                  "--snapshot=" + snapshot, "--file", str(dest / "database.dump")], self.engine.url)
                with (dest / "database.dump").open("rb") as stream:
                    os.fsync(stream.fileno())
                backup_objects = ObjectStore(dest / "objects")
                objects = {}
                for row in rows:
                    ref = self.objects.verify(row["hash"], expected_size=row["size"])
                    copied = backup_objects.put(self.objects.read(ref.sha256))
                    if copied != ref:
                        raise DomainError("BACKUP_OBJECT_MISMATCH", "Copied object failed verification")
                    objects[ref.sha256] = ref.size
            manifest.update(self._copy_interpretation(dest, initial=initial_runtime))
        manifest.update({"status": "COMPLETE", "dump_sha256": _sha(dest / "database.dump"),
                         "objects": objects, "pg_dump": dump,
                         "duration_seconds": time.monotonic() - started,
                         "completed_at_utc": datetime.now(UTC).isoformat(),
                         "limitations": ["Logical snapshot, not WAL/PITR or cluster roles",
                                         "Application schema migrations must be scheduled separately",
                                         "Local content-addressed object retention lock required",
                                         "External side effects remain subject to reconciliation"]})
        for directory in sorted((p for p in dest.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
            ObjectStore._fsync_dir(directory)
        _write_json(dest / "manifest.json", manifest)
        ObjectStore._fsync_dir(dest)
        return manifest
