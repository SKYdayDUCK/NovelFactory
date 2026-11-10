"""Migrate an empty PostgreSQL database to P0 and verify contract bytes."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from sqlalchemy import create_engine, text


def main() -> None:
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "0001"], check=True)
    engine = create_engine(os.environ["NF_DATABASE_URL"])
    with engine.connect() as connection:
        revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        assert revision == "0001", revision
        rows = connection.execute(text("SELECT name, sha256, schema_version FROM nf_contract_baseline ORDER BY name")).mappings().all()
        assert len(rows) == 3
        for row in rows:
            assert row["sha256"] == hashlib.sha256((Path("contracts") / row["name"]).read_bytes()).hexdigest()
        with engine.connect() as other:
            pids = [connection.execute(text("SELECT pg_backend_pid()")).scalar_one(),
                    other.execute(text("SELECT pg_backend_pid()")).scalar_one()]
            assert len(set(pids)) == 2
        version = connection.execute(text("SHOW server_version")).scalar_one()
    engine.dispose()
    # Repeating an already applied migration must not mutate its registry.
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "0001"], check=True)
    print(json.dumps({"status": "PASS", "migration": revision, "postgres": version,
                      "independent_backend_pids": pids, "contract_rows": [dict(row) for row in rows]}, indent=2))


if __name__ == "__main__":
    main()
