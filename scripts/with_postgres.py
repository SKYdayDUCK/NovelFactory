"""Run a command against a real, isolated PostgreSQL 17 server.

This keeps server and clients in the same process/network namespace. It needs
only PostgreSQL binaries and Python's standard library, not Docker or root.
The loopback-only trust authentication is for generated test data exclusively.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time


def call(command: list[str], *, env: dict[str, str] | None = None) -> None:
    print("RUN", json.dumps(command), flush=True)
    subprocess.run(command, env=env, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pg-bin", default=os.environ.get("PG_BIN"))
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--log-dir", type=Path)
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--migrate", action="store_true")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("a command is required after --")
    pg_bin = Path(args.pg_bin).resolve() if args.pg_bin else None
    if pg_bin is None:
        found = shutil.which("postgres")
        if found:
            pg_bin = Path(found).resolve().parent
        elif Path("/usr/lib/postgresql/17/bin/postgres").exists():
            pg_bin = Path("/usr/lib/postgresql/17/bin")
        else:
            parser.error("PostgreSQL 17 binaries not found; set PG_BIN or --pg-bin")
    version = subprocess.check_output([str(pg_bin / "postgres"), "--version"], text=True).strip()
    if "PostgreSQL) 17." not in version:
        parser.error(f"requires PostgreSQL 17, got {version}")
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        parser.error("initdb must run as a non-root user; use the supplied Docker Compose")
    temporary = args.data_dir is None
    root = Path(tempfile.mkdtemp(prefix="nf-pg17-")) if temporary else args.data_dir.resolve()
    root.mkdir(parents=True, exist_ok=True)
    marker = root / "NF_ISOLATED_TEST_CLUSTER.json"
    data = root / "data"
    if data.exists() and not marker.exists():
        parser.error("refusing an existing database directory without the test-cluster marker")
    logs = args.log_dir.resolve() if args.log_dir else root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    port = args.port
    if not port:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
    reused = data.exists()
    if not reused:
        call([
            str(pg_bin / "initdb"), "-D", str(data), "-U", "nf_dev",
            "--encoding=UTF8", "--locale=C", "--auth-local=reject", "--auth-host=trust",
            "--data-checksums",
        ])
        marker.write_text(json.dumps({"purpose": "isolated_test_only", "postgres": version}) + "\n")
        (data / "pg_hba.conf").write_text(
            "# Generated disposable test data only; never expose outside loopback.\n"
            "host all all 127.0.0.1/32 trust\n", encoding="utf-8"
        )
    if (data / "PG_VERSION").read_text().strip() != "17":
        parser.error("existing test cluster is not PostgreSQL 17")
    settings = {
        "listen_addresses": "'127.0.0.1'",
        "unix_socket_directories": "''",
        "port": str(port),
        "max_connections": "60",
        "shared_buffers": "'128MB'",
        "timezone": "'UTC'",
        "log_timezone": "'UTC'",
        "fsync": "on",
        "synchronous_commit": "on",
        "full_page_writes": "on",
        "log_min_duration_statement": "1000",
    }
    config = root / "nf-test.conf"
    config.write_text("\n".join(f"{key} = {value}" for key, value in settings.items()) + "\n")
    env = os.environ.copy()
    env.update({
        "NF_DATABASE_URL": f"postgresql+psycopg://nf_dev@127.0.0.1:{port}/nf_test",
        "DATABASE_URL": f"postgresql+psycopg://nf_dev@127.0.0.1:{port}/nf_test",
        "NF_TEST_DATABASE_URL": f"postgresql+psycopg://nf_dev@127.0.0.1:{port}/nf_test",
        "PG_BIN": str(pg_bin), "PGHOST": "127.0.0.1", "PGPORT": str(port),
        "PGUSER": "nf_dev", "PGDATABASE": "nf_test",
        "PATH": str(pg_bin) + os.pathsep + env.get("PATH", ""),
    })
    run = {
        "postgres": version, "port": port, "command": command,
        "started_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "authentication": "loopback_trust_generated_test_data_only",
        "fsync": True, "synchronous_commit": True, "max_connections": 60,
        "data_directory_reused": reused,
    }
    returncode = 1
    timer = time.monotonic()
    with (logs / "postgresql.log").open("ab") as server_log:
        server = subprocess.Popen(
            [str(pg_bin / "postgres"), "-D", str(data), "-c", f"config_file={config}"],
            stdout=server_log, stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 20
            while True:
                if server.poll() is not None:
                    raise RuntimeError(f"PostgreSQL failed to start; inspect {logs / 'postgresql.log'}")
                result = subprocess.run(
                    [str(pg_bin / "pg_isready"), "-h", "127.0.0.1", "-p", str(port), "-U", "nf_dev"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                if result.returncode == 0:
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError("PostgreSQL startup deadline exceeded")
                time.sleep(0.05)
            exists = subprocess.check_output(
                [str(pg_bin / "psql"), "-d", "postgres", "-Atc",
                 "SELECT 1 FROM pg_database WHERE datname='nf_test'"], env=env, text=True,
            ).strip()
            if not exists:
                call([str(pg_bin / "createdb"), "nf_test"], env=env)
            call([str(pg_bin / "psql"), "-Atc", "SELECT version(); SHOW fsync; SHOW synchronous_commit;"], env=env)
            if args.migrate:
                call([sys.executable, "-m", "alembic", "upgrade", "head"], env=env)
            print("REAL_POSTGRES_READY", version, "port", port, flush=True)
            returncode = subprocess.run(command, env=env).returncode
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.send_signal(signal.SIGINT)
                try:
                    server.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait()
            run.update({
                "exit_status": returncode, "postgres_exit_status": server.returncode,
                "duration_seconds": round(time.monotonic() - timer, 6),
                "finished_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            })
            (logs / "postgres_session.json").write_text(json.dumps(run, indent=2) + "\n")
            if temporary and not args.keep:
                if logs.is_relative_to(root):
                    print("Temporary PostgreSQL logs:", logs, "(use --log-dir to retain)", flush=True)
                else:
                    shutil.rmtree(root)
    return returncode


if __name__ == "__main__":
    raise SystemExit(main())
