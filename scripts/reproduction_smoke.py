"""Exercise the shipped migration/bootstrap/HTTP/backup commands on isolated PG.

Run inside scripts/with_postgres.py or set NF_TEST_DATABASE_URL for a disposable
PostgreSQL 17 instance whose test account may create databases. The script makes
two new databases, never changes the supplied database, and removes its own
databases and temporary private credentials when finished.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

import httpx
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


def run(project: Path, environment: dict[str, str], *arguments: str) -> None:
    command = [sys.executable, *arguments]
    print("RUN", json.dumps(command), flush=True)
    subprocess.run(command, cwd=project, env=environment, check=True)


def main() -> None:
    source = os.environ.get("NF_TEST_DATABASE_URL")
    if not source:
        raise SystemExit("NF_TEST_DATABASE_URL required; use scripts/with_postgres.py")
    project = Path(__file__).resolve().parents[1]
    base = make_url(source)
    databases = ["nf_repro_" + uuid4().hex[:16], "nf_restore_" + uuid4().hex[:16]]
    admin = create_engine(base.set(database="postgres"), isolation_level="AUTOCOMMIT")
    created: list[str] = []
    try:
        with admin.connect() as connection:
            for name in databases:
                connection.execute(text(f'CREATE DATABASE "{name}"'))
                created.append(name)
        with tempfile.TemporaryDirectory(prefix="nf-reproduction-") as temporary:
            root = Path(temporary)
            environment = {
                **os.environ,
                "NF_DATABASE_URL": base.set(database=databases[0]).render_as_string(hide_password=False),
                "NF_RESTORE_DATABASE_URL": base.set(database=databases[1]).render_as_string(hide_password=False),
                "NF_OBJECT_ROOT": str(root / "objects"),
            }
            environment.pop("PYTHONPATH", None)
            run(project, environment, "-m", "alembic", "upgrade", "head")
            private = root / "access.json"
            run(project, environment, "scripts/bootstrap_fixture.py", "--history", "--output", str(private))
            access = json.loads(private.read_text())
            assert private.stat().st_mode & 0o777 == 0o600
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            server_log = root / "http.log"
            with server_log.open("w") as stream:
                server = subprocess.Popen([
                    sys.executable, "-m", "uvicorn", "novel_factory.interfaces.http:from_environment",
                    "--factory", "--host", "127.0.0.1", "--port", str(port), "--no-access-log",
                ], cwd=project, env=environment, stdout=stream, stderr=subprocess.STDOUT)
                try:
                    with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=10) as client:
                        deadline = time.monotonic() + 10
                        while True:
                            if server.poll() is not None:
                                raise RuntimeError("HTTP startup failed: " + server_log.read_text())
                            try:
                                if client.get("/health").status_code == 200:
                                    break
                            except httpx.TransportError:
                                pass
                            if time.monotonic() >= deadline:
                                raise RuntimeError("HTTP startup timeout")
                            time.sleep(0.05)
                        response = client.post("/api/v1/queries/timeline", json=access["query"],
                                               headers={"Authorization": "Bearer " + access["token"]})
                        assert response.status_code == 200, response.text
                        body = response.json()
                        assert body["read_cut"]["world_ref"] == access["world_ref"]
                        states = {item["predicate"]: item["answer"]["data"]
                                  for item in body["payload"]["state_items"]}
                        assert states["owner"]["entity_id"] == access["ids"]["protagonist"]
                        assert states["active_holder"]["entity_id"] == access["ids"]["friend"]
                        assert client.post("/api/v1/queries/timeline", json=access["query"]).status_code == 401
                finally:
                    server.terminate()
                    try:
                        server.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        server.kill()
                        server.wait()
            backup = root / "backup"
            run(project, environment, "scripts/backup.py", "create", str(backup))
            run(project, environment, "scripts/backup.py", "verify", str(backup))
            report = root / "restore_report.json"
            run(project, environment, "scripts/backup.py", "restore", str(backup),
                "--restore-object-root", str(root / "restored_objects"), "--report", str(report))
            restored = json.loads(report.read_text())
            assert restored["snapshot_equal_before_fencing"] is True
            assert restored["interpretation_verified"] is True
            assert restored["fenced_tasks"]
            assert all(task["state"] == "PAUSED" for task in restored["fenced_tasks"])
            print(json.dumps({
                "status": "PASS", "migration": "head", "private_file_mode": "0600",
                "http_transport": "real_loopback_tcp_separate_uvicorn_process", "http_query_status": 200,
                "http_missing_auth_status": 401, "historical_day": 25,
                "historical_owner": "protagonist", "historical_holder": "friend",
                "backup_cli": "PASS", "restore_cli": "PASS",
                "restored_table_count": restored["tables_verified"], "restored_object_count": restored["object_count"],
                "snapshot_equal_before_fencing": True, "credentials_printed": False,
                "task_fencing_count": len(restored["fenced_tasks"]),
            }, indent=2))
    finally:
        with admin.connect() as connection:
            for name in created:
                connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        admin.dispose()


if __name__ == "__main__":
    main()
