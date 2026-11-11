"""Auditable offline logical backups of PostgreSQL plus immutable objects.

This is a P1 logical backup, not WAL/PITR. It requires all application writers
(including tasks and authorization changes) to take the shared maintenance
advisory gate. The backup holds the exclusive gate and the shared object lock.
Restoration only accepts an empty, explicitly supplied isolated database.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from novel_factory.domain.errors import DomainError
from novel_factory.infrastructure.objects import ObjectStore
from novel_factory.orchestration.tasks import MAINTENANCE_GATE, TaskService, mutation_gate


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _write_json(path: Path, value):
    with path.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def database_snapshot(conn) -> dict:
    """Exact server textual JSON row digest per table, including empty tables."""
    quote = conn.dialect.identifier_preparer.quote
    result = {}
    for name in sorted(inspect(conn).get_table_names(schema="public")):
        rows = conn.execute(text(f'SELECT to_jsonb(t)::text FROM public.{quote(name)} AS t')).scalars().all()
        h = hashlib.sha256()
        for row in sorted(rows):
            value = row.encode("utf-8")
            h.update(len(value).to_bytes(8, "big"))
            h.update(value)
        result[name] = {"rows": len(rows), "sha256": h.hexdigest()}
    return result


def fence_recovered_tasks(engine, object_store, *, tables=None):
    """Fence a verified isolated restore before starting any API or workers.

    Works for a logical restore and a physically recovered cluster. Unknown
    external calls retain reservations; this function never sends a request.
    The caller must keep ingress stopped until this transaction commits.
    """
    if tables is None:
        from novel_factory.infrastructure.database import tables
    service = TaskService(engine, tables, object_store)
    fenced = []
    with engine.begin() as conn:
        mutation_gate(conn)
        task_table, calls, outbox = tables["tasks"], tables["call_attempts"], tables["outbox"]
        rows = conn.execute(select(task_table).where(task_table.c.state.not_in(
            ["ACCEPTED", "CANCELLED", "FAILED"])).order_by(task_table.c.task_id).with_for_update()).mappings().all()
        # An older backup may predate the additive workflow migration.
        has_waits = inspect(conn).has_table("nf_workflow_waits")
        for row in rows:
            task = dict(row)
            spec = {**service._spec(conn, task), "context_bundle_id": task["context_bundle_id"],
                    "dependency_fingerprint": task["dependency_fingerprint"]}
            generation = task["generation"] + 1
            state = "BLOCKED" if task["state"] == "BLOCKED" else "PAUSED"
            service._save_input(conn, task["task_id"], generation, spec, state)
            if state == "BLOCKED" and has_waits:
                # Preserve the barrier when fencing generations. Converting an
                # unresolved wait to a manually resumable PAUSED bypasses it.
                params = {"id": task["task_id"], "old": task["generation"], "new": generation}
                conn.execute(text("""
                    INSERT INTO nf_workflow_waits(task_id,generation,mode,grant_epoch,created_at)
                    SELECT task_id,:new,mode,grant_epoch,created_at FROM nf_workflow_waits
                    WHERE task_id=:id AND generation=:old AND woken_at IS NULL
                """), params)
                conn.execute(text("""
                    INSERT INTO nf_workflow_edges(task_id,generation,predecessor_id)
                    SELECT e.task_id,:new,e.predecessor_id FROM nf_workflow_edges e
                    JOIN nf_workflow_waits w USING (task_id,generation)
                    WHERE e.task_id=:id AND e.generation=:old AND w.woken_at IS NULL
                """), params)
            conn.execute(task_table.update().where(task_table.c.task_id == task["task_id"]).values(
                generation=generation, state=state, lease_owner=None, lease_expiry=None, attempt_id=None))
            fenced.append({"task_id": task["task_id"], "old_generation": task["generation"],
                           "generation": generation, "state": state})
        conn.execute(calls.update().where(calls.c.state == "SENT").values(state="UNKNOWN"))
        conn.execute(outbox.update().where(outbox.c.state == "DELIVERING").values(
            state="PENDING", generation=outbox.c.generation + 1, lease_owner=None, lease_expiry=None))
    return fenced


class BackupService:
    def __init__(self, engine, object_store: ObjectStore, *, pg_bin=None, package_root=None):
        self.engine, self.objects = engine, object_store
        self.pg_bin = Path(pg_bin or os.environ.get("PG_BIN", "/usr/bin"))
        self.package_root = Path(package_root or Path(__file__).resolve().parents[1])

    @staticmethod
    def validate_connection_target(url):
        """Fail before connecting if Python and libpq tools could disagree.

        This finite recovery interface supports one explicit TCP endpoint,
        user and database, plus the SSL options reproduced by _pg_env. It
        deliberately rejects service files, hostaddr overrides, multi-host or
        socket routing and connection-string database names. The caller's
        process environment is never edited, including in a threaded service.
        """
        try:
            url = make_url(url)
            explicit = (url.get_backend_name() == "postgresql" and url.host and url.username
                        and url.database and url.port is not None and 1 <= url.port <= 65535)
        except (ArgumentError, TypeError, ValueError):
            raise DomainError("BACKUP_TARGET_AMBIGUOUS", "Recovery requires an explicit PostgreSQL connection target") from None
        if not explicit:
            raise DomainError("BACKUP_TARGET_AMBIGUOUS", "Recovery requires explicit PostgreSQL host, port, user and database")
        if any(mark in url.host for mark in (",", "/", "\\")) or any(char.isspace() for char in url.host):
            raise DomainError("BACKUP_TARGET_AMBIGUOUS", "Recovery supports one explicit TCP host")
        if "=" in url.database or "://" in url.database or "\x00" in url.database:
            raise DomainError("BACKUP_TARGET_AMBIGUOUS", "A recovery database name must not be a connection string")
        allowed = {"sslmode", "sslrootcert", "sslcert", "sslkey"}
        if any(key not in allowed or not isinstance(value, str) or not value
               for key, value in url.query.items()):
            raise DomainError("BACKUP_TARGET_AMBIGUOUS", "Recovery URL options must be explicitly supported single SSL values")
        if any(os.environ.get(key) for key in ("PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE")):
            raise DomainError("BACKUP_TARGET_AMBIGUOUS", "Remove ambient PostgreSQL hostaddr/service routing before recovery")
        return url

    @staticmethod
    def _pg_env(url):
        url = BackupService.validate_connection_target(url)
        env = os.environ.copy()
        for key in ("PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE", "PGOPTIONS"):
            env.pop(key, None)
        env.update({"PGHOST": url.host, "PGPORT": str(url.port),
                    "PGUSER": url.username, "PGDATABASE": url.database,
                    "PGCONNECT_TIMEOUT": "10", "PGAPPNAME": "novel_factory_backup"})
        if url.password:
            env["PGPASSWORD"] = url.password
        else:
            env.pop("PGPASSWORD", None)
        for key, value in url.query.items():
            if key in {"sslmode", "sslrootcert", "sslcert", "sslkey"}:
                env["PG" + key.upper()] = str(value)
        return env

    def _tool(self, name, args, url):
        executable = self.pg_bin / name
        if not executable.is_file():
            raise DomainError("BACKUP_TOOL_UNAVAILABLE", f"Required PostgreSQL tool {name} is unavailable")
        completed = subprocess.run([str(executable), *args], env=self._pg_env(url), capture_output=True, text=True, timeout=180, check=False)
        if completed.returncode:
            # No credentials/DSN are included in the public error. Raw tool
            # stderr is retained only in the caller's private exception chain.
            error = RuntimeError(completed.stderr[-8000:])
            raise DomainError("BACKUP_TOOL_FAILED", f"{name} exited with status {completed.returncode}") from error
        return {"tool": name, "exit_code": completed.returncode, "stdout": completed.stdout, "stderr": completed.stderr}

    def _runtime_files(self):
        for path in sorted(self.package_root.rglob("*.py")):
            if "__pycache__" not in path.parts:
                yield path.relative_to(self.package_root).as_posix(), path

    def _schema_directory(self):
        packaged = self.package_root / "data" / "contracts"
        checkout = self.package_root.parents[1] / "contracts"
        return packaged if packaged.is_dir() else checkout

    def _runtime_assets(self):
        packaged = self.package_root / "data" / "runtime"
        checkout = self.package_root.parents[1] / "runtime"
        root = packaged if packaged.is_dir() else checkout
        assets = {"runtime/" + p.relative_to(root).as_posix(): p
                  for p in sorted(root.rglob("*")) if p.is_file() and p.suffix in {".json", ".md"}}
        required = {"runtime/query_profiles.json", "runtime/roles/catalog.json", "runtime/skills/catalog.json"}
        if not required.issubset(assets):
            raise DomainError("INTERPRETATION_UNAVAILABLE", "Runtime authorization, role or skill catalog is unavailable")
        return assets

    def create(self, destination: str | Path):
        self.validate_connection_target(self.engine.url)
        dest = Path(destination).resolve()
        if dest.exists():
            raise DomainError("BACKUP_DESTINATION_EXISTS", "Backup destination must be new")
        dest.mkdir(parents=True)
        ObjectStore._fsync_dir(dest.parent)
        manifest = {"format": "novel-factory-logical-backup-1", "created_at": datetime.now(UTC).isoformat(),
                    "backup_id": str(uuid4()), "status": "INCOMPLETE"}
        _write_json(dest / "manifest.json", manifest)
        with self.objects.lock(), self.engine.connect() as conn:
            locked = False
            try:
                conn.execute(text("SELECT pg_advisory_lock(:key)"), {"key": MAINTENANCE_GATE})
                locked = True
                conn.commit()  # Session lock survives this transaction.
                conn.execute(text("SET TIME ZONE 'UTC'"))
                manifest["postgres_version"] = conn.scalar(text("SHOW server_version"))
                manifest["database_snapshot"] = database_snapshot(conn)
                object_rows = conn.execute(text("SELECT hash, size FROM nf_objects ORDER BY hash")).mappings().all()
                conn.commit()
                result = self._tool("pg_dump", ["--format=custom", "--no-owner", "--no-acl", "--file", str(dest / "database.dump")], self.engine.url)
                with (dest / "database.dump").open("rb") as stream:
                    os.fsync(stream.fileno())
                backup_objects = ObjectStore(dest / "objects")
                hashes = {}
                for row in object_rows:
                    ref = self.objects.verify(row["hash"], expected_size=row["size"])
                    copied = backup_objects.put(self.objects.path(ref.sha256).read_bytes())
                    if copied != ref:
                        raise DomainError("BACKUP_OBJECT_MISMATCH", "Copied object failed verification")
                    hashes[ref.sha256] = ref.size
                runtime_hashes = {}
                for relative, source in self._runtime_files():
                    target = dest / "interpretation" / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, target)
                    with target.open("rb") as stream:
                        os.fsync(stream.fileno())
                    runtime_hashes[relative] = _sha(target)
                # Include original/public schemas with interpretation code.
                schema_dir = self._schema_directory()
                schema_hashes = {}
                if schema_dir.is_dir():
                    for source in sorted(schema_dir.glob("*.schema.json")):
                        target = dest / "schemas" / source.name
                        target.parent.mkdir(exist_ok=True)
                        shutil.copyfile(source, target)
                        with target.open("rb") as stream:
                            os.fsync(stream.fileno())
                        schema_hashes[source.name] = _sha(target)
                asset_hashes = {}
                for relative, source in self._runtime_assets().items():
                    target = dest / "runtime_assets" / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, target)
                    with target.open("rb") as stream:
                        os.fsync(stream.fileno())
                    asset_hashes[relative] = _sha(target)
                manifest.update({"status": "COMPLETE", "dump_sha256": _sha(dest / "database.dump"),
                                 "objects": hashes, "runtime_files": runtime_hashes, "schemas": schema_hashes, "runtime_assets": asset_hashes,
                                 "runtime_asset_version": 2,
                                 "pg_dump": result, "limitations": ["Logical backup; no WAL/PITR or cluster roles", "All writers must participate in maintenance gate", "External side effects require independent reconciliation"]})
                for directory in sorted((p for p in dest.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
                    ObjectStore._fsync_dir(directory)
                _write_json(dest / "manifest.json", manifest)
                ObjectStore._fsync_dir(dest)
            finally:
                if locked:
                    if conn.in_transaction():
                        conn.rollback()
                    conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": MAINTENANCE_GATE})
                    conn.commit()
        return manifest

    def verify(self, source: str | Path, *, require_runtime=True):
        src = Path(source).resolve()
        try:
            manifest = json.loads((src / "manifest.json").read_text())
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            raise DomainError("BACKUP_INCOMPLETE", "Backup manifest is missing or invalid") from exc
        if manifest.get("format") != "novel-factory-logical-backup-1" or manifest.get("status") != "COMPLETE":
            raise DomainError("BACKUP_INCOMPLETE", "Backup is not complete")
        if _sha(src / "database.dump") != manifest["dump_sha256"]:
            raise DomainError("BACKUP_CORRUPT", "Database dump hash is invalid")
        objects = ObjectStore(src / "objects")
        for digest, size in manifest["objects"].items():
            objects.verify(digest, expected_size=size)
        if require_runtime:
            # Extra installed members can change import resolution without
            # altering any saved file (e.g. alpha/__init__.py shadows alpha.py).
            # Match the whole interpretation inventory, as online creation does.
            installed_files = dict(self._runtime_files())
            if set(manifest["runtime_files"]) != set(installed_files):
                raise DomainError("INTERPRETATION_UNAVAILABLE", "Installed interpretation file set differs from backup")
            installed_schemas = {path.name for path in self._schema_directory().glob("*.schema.json")}
            if set(manifest.get("schemas", {})) != installed_schemas:
                raise DomainError("INTERPRETATION_UNAVAILABLE", "Installed schema set differs from backup")
        for relative, digest in manifest["runtime_files"].items():
            target = (src / "interpretation" / relative).resolve()
            if not target.is_relative_to(src / "interpretation") or not target.is_file() or _sha(target) != digest:
                raise DomainError("INTERPRETATION_UNAVAILABLE", "Backup interpretation file is unavailable")
            if require_runtime:
                installed = self.package_root / relative
                if not installed.is_file() or _sha(installed) != digest:
                    raise DomainError("INTERPRETATION_UNAVAILABLE", "Installed interpretation differs from backup; use the saved compatible version")
        assets = manifest.get("runtime_assets")
        asset_version = manifest.get("runtime_asset_version", 1)
        required = {"runtime/query_profiles.json"}
        if asset_version == 2:
            required |= {"runtime/roles/catalog.json", "runtime/skills/catalog.json"}
        if (not isinstance(assets, dict) or asset_version not in {1, 2}
                or not required.issubset(assets)
                or (asset_version == 1 and set(assets) != required)):
            raise DomainError("INTERPRETATION_UNAVAILABLE", "Backup runtime asset manifest is incomplete")
        installed_assets = self._runtime_assets() if require_runtime else {}
        if require_runtime and asset_version == 2 and set(assets) != set(installed_assets):
            raise DomainError("INTERPRETATION_UNAVAILABLE", "Installed runtime asset set differs from backup")
        for relative, digest in assets.items():
            target = (src / "runtime_assets" / relative).resolve()
            if not target.is_relative_to(src / "runtime_assets") or not target.is_file() or _sha(target) != digest:
                raise DomainError("INTERPRETATION_UNAVAILABLE", "Backup runtime role, skill or authorization asset is unavailable")
            if require_runtime and (relative not in installed_assets or _sha(installed_assets[relative]) != digest):
                raise DomainError("INTERPRETATION_UNAVAILABLE", "Installed runtime asset differs from backup")
        for name, digest in manifest.get("schemas", {}).items():
            target = (src / "schemas" / name).resolve()
            if not target.is_relative_to(src / "schemas") or not target.is_file() or _sha(target) != digest:
                raise DomainError("BACKUP_CORRUPT", "Backup schema is unavailable")
            if require_runtime:
                installed_schema = self._schema_directory() / name
                if not installed_schema.is_file() or _sha(installed_schema) != digest:
                    raise DomainError("INTERPRETATION_UNAVAILABLE", "Installed schema differs from backup")
        return manifest

    def restore(self, source: str | Path, target_url, object_root: str | Path, *, tables=None, fence_tasks=True):
        target_url = self.validate_connection_target(target_url)
        src = Path(source).resolve()
        manifest = self.verify(src)
        target = create_engine(target_url, pool_size=2, max_overflow=0)
        try:
            with target.connect() as conn:
                if inspect(conn).get_table_names(schema="public"):
                    raise DomainError("RESTORE_TARGET_NOT_EMPTY", "Restore requires an empty isolated database")
                target_version = conn.scalar(text("SHOW server_version_num"))
                if str(target_version)[:2] != "17":
                    raise DomainError("DATABASE_VERSION_UNSUPPORTED", "P1 restoration is verified on PostgreSQL 17")
            restored_store = ObjectStore(object_root)
            if restored_store.inventory():
                raise DomainError("RESTORE_TARGET_NOT_EMPTY", "Restore requires an empty isolated object directory")
            for digest, size in manifest["objects"].items():
                backup_store = ObjectStore(src / "objects")
                ref = restored_store.put(backup_store.read(digest))
                if ref.sha256 != digest or ref.size != size:
                    raise DomainError("BACKUP_OBJECT_MISMATCH", "Restored object failed verification")
            result = self._tool("pg_restore", ["--exit-on-error", "--no-owner", "--no-acl", "--dbname", make_url(target_url).database, str(src / "database.dump")], target_url)
            with target.connect() as conn:
                conn.execute(text("SET TIME ZONE 'UTC'"))
                snapshot = database_snapshot(conn)
                if snapshot != manifest["database_snapshot"]:
                    raise DomainError("RESTORE_VERIFICATION_FAILED", "Restored database differs from frozen backup")
            fenced = []
            if fence_tasks:
                fenced = fence_recovered_tasks(target, restored_store, tables=tables)
            verification = {"backup_id": manifest["backup_id"], "snapshot_equal_before_fencing": True,
                            "object_count": len(manifest["objects"]), "tables_verified": len(snapshot),
                            "fenced_tasks": fenced, "pg_restore": result,
                            "interpretation_verified": True}
            return verification
        finally:
            target.dispose()
