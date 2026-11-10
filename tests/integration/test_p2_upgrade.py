"""An accepted P1 world survives the additive P2 migration byte-for-byte."""
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url

from novel_factory.application.kernel import Kernel
from novel_factory.fixtures import load_fixture, point, run_command
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.objects import ObjectStore
from novel_factory.infrastructure.recovery import database_snapshot


pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def test_p1_accepted_world_is_preserved_by_p2_additive_migration(kernel_env, tmp_path):
    name = "nf_p2_upgrade_" + uuid4().hex[:12]
    admin = kernel_env["admin"]
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    target = make_url(kernel_env["url"]).set(database=name).render_as_string(hide_password=False)
    root = Path(__file__).resolve().parents[2]
    env = {**os.environ, "NF_DATABASE_URL": target}
    db = None
    try:
        subprocess.run([sys.executable, "-m", "alembic", "upgrade", "0002"],
                       cwd=root, env=env, check=True, capture_output=True)
        db = Database(target)
        kernel = Kernel(db, ObjectStore(tmp_path / "objects"))
        fixture = load_fixture(kernel, "white_bridge")
        accepted = run_command(kernel, fixture["principal"], fixture["world_ref"], "RESOURCE_TRANSFER", {
            "from_account": fixture["ids"]["warehouse"], "to_account": fixture["ids"]["relief"],
            "resource_id": fixture["ids"]["grain"], "quantity": "12", "unit": "bag", "at": point(10),
        })
        with db.engine.connect() as conn:
            before = database_snapshot(conn)
        subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"],
                       cwd=root, env=env, check=True, capture_output=True)
        with db.engine.connect() as conn:
            after = database_snapshot(conn)
        for table, digest in before.items():
            if table != "alembic_version":
                assert after[table] == digest, table
        for table in ("nf_simulation_interactions", "nf_simulation_intents", "nf_simulation_outcomes"):
            assert after[table]["rows"] == 0
        assert kernel.accept(fixture["principal"], accepted["request"]) == accepted["receipt"]
        with db.uow(write=False) as repo:
            assert kernel.head(repo, fixture["world_ref"]["work_id"]) == accepted["world_ref"]
    finally:
        if db is not None:
            db.close()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
