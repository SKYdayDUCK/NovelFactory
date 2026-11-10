"""P4-02 physical base backup/WAL replay in generated, isolated PG17 clusters.

Accepts no production database URL. Both clusters and every recovery command
operate inside a newly created output directory. A deliberate later commit
must be absent at the selected named recovery point. Do not use the measured
small-fixture recovery time as a production RTO promise.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time

from sqlalchemy import text

from novel_factory.application.kernel import Kernel
from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import create_bound_task, load_fixture, point, run_command
from novel_factory.infrastructure.database import Database, tables
from novel_factory.infrastructure.objects import ObjectStore
from novel_factory.infrastructure.online_backup import OnlineBackupService
from novel_factory.infrastructure.recovery import database_snapshot, fence_recovered_tasks


def _check(condition, reason):
    # Recovery evidence must remain enforced under python -O as well.
    if not condition:
        raise RuntimeError(reason)


def _port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")


def _file_sha(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _run(command, *, env=None):
    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=180)
    if result.returncode:
        raise RuntimeError(f"{Path(command[0]).name} failed: {result.stderr[-6000:]}")
    return {"exit_code": result.returncode, "stdout": result.stdout.strip()}


def _config(path, settings):
    def value(item):
        return str(item) if type(item) is int else "'" + str(item).replace("'", "''") + "'"
    path.write_text("\n".join(f"{key} = {value(item)}" for key, item in settings.items()) + "\n")


def _start(pg_bin, data, config, port, log):
    stream = log.open("ab")
    process = subprocess.Popen([str(pg_bin / "postgres"), "-D", str(data), "-c", f"config_file={config}"],
                               stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
    stream.close()
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"PostgreSQL stopped: {log.read_text()[-6000:]}")
        result = subprocess.run([str(pg_bin / "pg_isready"), "-h", "127.0.0.1", "-p", str(port), "-U", "nf_dev"],
                                capture_output=True, timeout=3)
        if result.returncode == 0:
            return process
        time.sleep(0.05)
    _stop(process)
    raise RuntimeError("Isolated PostgreSQL startup exceeded 45 seconds")


def _stop(process, *, crash=False):
    if process is None or process.poll() is not None:
        return
    # Only the process group created by _start is affected.
    os.killpg(process.pid, signal.SIGKILL if crash else signal.SIGINT)
    process.wait(timeout=15)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--pg-bin", default=os.environ.get("PG_BIN", "/usr/lib/postgresql/17/bin"), type=Path)
    parser.add_argument("--keep-clusters", action="store_true")
    args = parser.parse_args()
    root, pg_bin = args.output.resolve(), args.pg_bin.resolve()
    # This is a dedicated CLI process with generated credentials and clusters.
    # libpq PGHOSTADDR/service settings can redirect even an explicit URL host;
    # discard all inherited PostgreSQL connection policy before any connection.
    # The operator's binary path is the only PG setting this drill retains.
    cleared_pg_environment = sorted(key for key in os.environ if key.startswith("PG") and key != "PG_BIN")
    for key in cleared_pg_environment:
        os.environ.pop(key, None)
    if root.exists():
        parser.error("output must be a new directory; existing data is never overwritten")
    if os.geteuid() == 0:
        parser.error("run as a non-root account, like PostgreSQL initdb")
    version = _run([str(pg_bin / "postgres"), "--version"])["stdout"]
    if "PostgreSQL) 17." not in version:
        parser.error("this recovery protocol is verified for PostgreSQL17")
    root.mkdir(parents=True)
    _write(root / "NF_ISOLATED_RECOVERY_DRILL.json", {"purpose": "generated_test_data_only", "postgres": version})
    source_data, archive = root / "source", root / "wal-archive"
    archive.mkdir()
    _run([str(pg_bin / "initdb"), "-D", str(source_data), "-U", "nf_dev", "--encoding=UTF8",
          "--locale=C", "--auth-local=reject", "--auth-host=trust", "--data-checksums"])
    (source_data / "pg_hba.conf").write_text("host all all 127.0.0.1/32 trust\nhost replication nf_dev 127.0.0.1/32 trust\n")
    # Archive commands must never overwrite a differing segment. Atomic rename
    # and fsync also avoid accepting a half-copied archive on restart.
    archiver = root / "archive_segment.py"
    archiver.write_text(
        "import os,pathlib,sys,hashlib\n"
        "src,dst=map(pathlib.Path,sys.argv[1:])\n"
        "if dst.exists():\n"
        " if src.read_bytes()!=dst.read_bytes(): raise RuntimeError('WAL archive collision')\n"
        "else:\n"
        " tmp=dst.with_suffix('.partial')\n"
        " with tmp.open('wb') as f:\n"
        "  f.write(src.read_bytes());f.flush();os.fsync(f.fileno())\n"
        " os.replace(tmp,dst)\n"
        " fd=os.open(dst.parent,os.O_DIRECTORY);os.fsync(fd);os.close(fd)\n")
    source_port, restored_port = _port(), _port()
    while restored_port == source_port:
        restored_port = _port()
    settings = {"listen_addresses": "127.0.0.1", "unix_socket_directories": "", "port": source_port,
                "max_connections": 40, "shared_buffers": "128MB", "timezone": "UTC", "log_timezone": "UTC",
                "fsync": "on", "synchronous_commit": "on", "full_page_writes": "on",
                "wal_level": "replica", "archive_mode": "on", "max_wal_senders": 4,
                "archive_command": f"{shlex.quote(sys.executable)} {shlex.quote(str(archiver))} %p {shlex.quote(str(archive))}/%f"}
    source_config = root / "source.conf"
    _config(source_config, settings)
    source = restored = database = recovered_database = None
    result = {"status": "FAILED", "postgres": version, "execution_kind": "REAL_ISOLATED_POSTGRESQL",
              "source_port": source_port, "restored_port": restored_port,
              "durability": {"fsync": True, "synchronous_commit": True, "data_checksums": True},
              "model_calls": 0, "production_data_accessed": False,
              "libpq_environment_isolated": True, "cleared_pg_environment_names": cleared_pg_environment}
    started = time.monotonic()
    try:
        source = _start(pg_bin, source_data, source_config, source_port, root / "source.log")
        pg_env = {**os.environ, "PGHOST": "127.0.0.1", "PGPORT": str(source_port), "PGUSER": "nf_dev",
                  "PGDATABASE": "nf_pitr", "PG_BIN": str(pg_bin)}
        for variable in ("PGPASSWORD", "PGSERVICE", "PGSERVICEFILE", "PGOPTIONS"):
            pg_env.pop(variable, None)
        _run([str(pg_bin / "createdb"), "nf_pitr"], env=pg_env)
        source_url = f"postgresql+psycopg://nf_dev@127.0.0.1:{source_port}/nf_pitr"
        _run([sys.executable, "-m", "alembic", "upgrade", "head"], env={**pg_env, "NF_DATABASE_URL": source_url})
        database = Database(source_url, pool_size=4)
        objects = ObjectStore(root / "source-objects")
        kernel = Kernel(database, objects)
        world = load_fixture(kernel, "white_bridge")
        principal, initial = world["principal"], world["world_ref"]

        def transfer(wr, quantity, day):
            return run_command(kernel, principal, wr, "RESOURCE_TRANSFER", {
                "from_account": world["ids"]["warehouse"], "to_account": world["ids"]["relief"],
                "resource_id": world["ids"]["grain"], "quantity": quantity, "unit": "bag", "at": point(day)})

        first = transfer(initial, "10", 10)
        task = create_bound_task(kernel, principal, first["world_ref"], point(10))
        budget = kernel.tasks.create_budget(initial["work_id"], "5")
        call = kernel.tasks.reserve_call(task["task_id"], task["generation"], task["worker_id"],
                                        budget_id=budget, amount="0.5", call_key="pitr-unknown",
                                        request_hash="a" * 64, token_amount=100)
        kernel.tasks.mark_call_sent(call["attempt_id"], worker_id=task["worker_id"])
        base_started = time.monotonic()
        base = root / "basebackup"
        result["pg_basebackup"] = _run([str(pg_bin / "pg_basebackup"), "-D", str(base), "-Fp", "-Xstream",
                                        "--checkpoint=fast", "--manifest-checksums=SHA256"], env=pg_env)
        result["base_backup_seconds"] = time.monotonic() - base_started
        result["pg_verifybackup"] = _run([str(pg_bin / "pg_verifybackup"), str(base)])
        target = transfer(first["world_ref"], "5", 20)
        backup_service = OnlineBackupService(database.engine, objects, pg_bin=pg_bin)
        logical = backup_service.create(root / "target-logical-snapshot")
        # This workload is exclusively owned by the drill. No application write
        # occurs between this verified snapshot and the named recovery point.
        with database.engine.connect() as conn:
            target_at = conn.scalar(text("SELECT clock_timestamp()"))
            target_lsn = conn.scalar(text("SELECT pg_create_restore_point('nf_p4_verified_target')"))
            conn.commit()
        later = transfer(target["world_ref"], "7", 30)
        with database.engine.connect() as conn:
            wal_file = conn.scalar(text("SELECT pg_walfile_name(pg_current_wal_lsn())"))
            conn.execute(text("SELECT pg_switch_wal()"))
            conn.commit()
        deadline = time.monotonic() + 30
        while not (archive / wal_file).is_file():
            if time.monotonic() > deadline:
                raise RuntimeError("Required target WAL segment was not durably archived")
            time.sleep(0.05)
        with database.engine.connect() as conn:
            result["archiver"] = dict(conn.execute(text("SELECT archived_count, failed_count FROM pg_stat_archiver")).mappings().one())
        crash_at = datetime.now(timezone.utc)
        recovery_started = time.monotonic()
        _stop(source, crash=True)
        database.close()
        database = None
        recovery_data = root / "restored"
        shutil.copytree(base, recovery_data)
        (recovery_data / "recovery.signal").touch()
        restore_config = root / "restore.conf"
        restore_settings = {**settings, "port": restored_port, "archive_mode": "off",
                            "restore_command": f"cp {shlex.quote(str(archive))}/%f %p",
                            "recovery_target_name": "nf_p4_verified_target", "recovery_target_action": "promote"}
        _config(restore_config, restore_settings)
        restored = _start(pg_bin, recovery_data, restore_config, restored_port, root / "restored.log")
        recovered_url = f"postgresql+psycopg://nf_dev@127.0.0.1:{restored_port}/nf_pitr"
        recovered_database = Database(recovered_url, pool_size=4)
        # _start can see a hot-standby server before the target promotion.
        deadline = time.monotonic() + 30
        while True:
            with recovered_database.engine.connect() as conn:
                recovering = conn.scalar(text("SELECT pg_is_in_recovery()"))
            if not recovering:
                break
            if time.monotonic() > deadline:
                raise RuntimeError("Physical recovery did not reach the configured named target")
            time.sleep(0.05)
        with recovered_database.engine.connect() as conn:
            conn.execute(text("SET TIME ZONE 'UTC'"))
            recovered_snapshot = database_snapshot(conn)
        _check(recovered_snapshot == logical["database_snapshot"], "Physical database differs from exact target")
        backup_service.verify(root / "target-logical-snapshot")
        recovered_objects = ObjectStore(root / "restored-objects")
        for digest, size in logical["objects"].items():
            content = ObjectStore(root / "target-logical-snapshot" / "objects").read(digest)
            ref = recovered_objects.put(content)
            _check(ref.sha256 == digest and ref.size == size, "Recovered object digest or size mismatch")
        recovered = Kernel(recovered_database, recovered_objects)
        _check(recovered.authenticate(world["token"]) == principal, "Restored authentication differs")
        with recovered.db.uow(write=False) as repo:
            _check(recovered.head(repo, initial["work_id"]) == target["world_ref"], "Restored HEAD differs")
            _check(recovered.revision(repo, initial)["branch_seq"] == 0, "Initial immutable history changed")
            _check(repo.one("revisions", {"world_revision_id": later["world_ref"]["world_revision_id"]}) is None, "Later revision was not excluded")
            expected_state_hash = recovered.revision(repo, target["world_ref"])["state_hash"]
        fenced = fence_recovered_tasks(recovered_database.engine, recovered_objects, tables=tables)
        with recovered.db.uow(write=False) as repo:
            _check(repo.one("call_attempts", {"attempt_id": call["attempt_id"]})["state"] == "UNKNOWN", "External call was not retained as unknown")
            _check(repo.one("budgets", {"budget_id": budget})["reserved_amount"] == Decimal("0.5"), "Reserved budget was lost")
            _check(recovered.revision(repo, target["world_ref"])["state_hash"] == expected_state_hash, "Task fencing changed world state")
        try:
            with recovered.db.uow() as repo:
                recovered.tasks.guard_task(repo.conn, task["task_id"], task["generation"])
        except DomainError as exc:
            _check(exc.code == "STALE_GENERATION", "Old worker did not receive the generation fence")
        else:
            raise AssertionError("A worker from before the disaster was allowed to resume")
        # Replaying a pre-disaster accepted command returns its exact receipt.
        _check(recovered.accept(principal, target["request"]) == target["receipt"], "Restored accepted receipt differs")
        result.update(status="PASS", target_world_ref=target["world_ref"], target_lsn=str(target_lsn),
                      restored_state_sha256=expected_state_hash, snapshot_equal_before_fencing=True,
                      table_count=len(recovered_snapshot), object_count=len(logical["objects"]),
                      recovery_seconds_to_verified_service=time.monotonic() - recovery_started,
                      selected_recovery_point_age_seconds=(crash_at - target_at).total_seconds(),
                      target_commits_lost=0, deliberately_excluded_later_world_commits=1,
                      restored_world_event_count=2, unknown_calls_retained=1,
                      reserved_amount_retained="0.5", fenced_task_count=len(fenced),
                      old_worker_rejected=True, original_commit_receipt_equal=True,
                      limitation="Small isolated local fixture; selected-point RPO/RTO, not production SLO or remote storage test")
        _write(root / "target_database_digests.json", recovered_snapshot)
        _write(root / "fenced_tasks.json", fenced)
    finally:
        if recovered_database:
            recovered_database.close()
        if database:
            database.close()
        _stop(restored)
        _stop(source)
        result["total_seconds"] = time.monotonic() - started
        _write(root / "result.json", result)
        _write(root / "file_checksums.json", {p.relative_to(root).as_posix(): _file_sha(p)
                    for p in root.rglob("*") if p.is_file() and p.suffix in {".json", ".log", ".conf"}
                    and not any(part in {"source", "restored", "basebackup"} for part in p.relative_to(root).parts)})
        if not args.keep_clusters and result["status"] == "PASS":
            for directory in [source_data, root / "restored", root / "basebackup", archive,
                              root / "source-objects", root / "restored-objects", root / "target-logical-snapshot"]:
                if directory.is_dir():
                    shutil.rmtree(directory)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str), flush=True)
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
