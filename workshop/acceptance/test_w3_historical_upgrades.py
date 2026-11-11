"""Preserve real W3 pending and previously submitted intents through an in-place upgrade."""
import json
import os
import subprocess
import sys
import tarfile

from alembic.config import Config
from alembic.script import ScriptDirectory
import psycopg
import pytest

from test_w1_historical_upgrade import WORKSHOP, run
from test_w2_historical_upgrades import columns, snapshot


@pytest.mark.parametrize(("old_revision", "source", "database"), [
    ("w003", "15d1cf51c357627866d06bd49fd326798dda56a3", "AW_W3_FROM_W3_DATABASE_URL"),
    ("w003a", "ea7549cb471e5822bb3ce29723484cf9c7b06d6f", "AW_W3_FROM_W3A_DATABASE_URL"),
])
def test_old_queue_upgrade(old_revision, source, database, tmp_path, record_property):
    dsn = os.environ[database]
    archive = tmp_path / "old.tar"
    with psycopg.connect(dsn) as conn:
        assert conn.info.server_version // 10000 == 17
        assert conn.execute("SELECT to_regclass('aw_alembic_version')").fetchone() == (None,)
    subprocess.run(["git", "archive", "--format=tar", "--output", archive, source, "workshop"],
                   cwd=WORKSHOP.parent, check=True)
    with tarfile.open(archive) as members:
        members.extractall(tmp_path, filter="data")
    old = tmp_path / "workshop"
    state = tmp_path / "private-bindings.json"
    receipt = tmp_path / "receipt.json"
    head = ScriptDirectory.from_config(Config(str(WORKSHOP / "alembic.ini"))).get_current_head()
    assert head
    probe = WORKSHOP / "acceptance" / "delegation_upgrade_probe.py"

    def exercise(package, action):
        env = {**os.environ, "AW_DATABASE_URL": dsn, "AW_EXPECTED_HEAD": head,
               "PYTHONPATH": os.pathsep.join(str(package / part) for part in ("src", "tests")),
               "AW_RECEIPT": str(receipt)}
        subprocess.run([sys.executable, probe, action, state], cwd=package, env=env, check=True)

    try:
        run(old, dsn, "alembic", "upgrade", "head")
        exercise(old, "seed")
        with psycopg.connect(dsn) as conn:
            assert conn.execute("SELECT version_num FROM aw_alembic_version").fetchall() == [(old_revision,)]
            tables = columns(conn)
            original = snapshot(conn, tables)
            assert original["aw_delegations"][0] == original["aw_budget_holds"][0] == 2
        run(WORKSHOP, dsn, "alembic", "upgrade", "head")
        run(WORKSHOP, dsn, "alembic", "upgrade", "head")
        with psycopg.connect(dsn) as conn:
            assert snapshot(conn, tables) == original
            assert conn.execute("SELECT to_regclass('aw_current_message_edges') IS NOT NULL").fetchone() == (True,)
        exercise(WORKSHOP, "check")
        record_property("source_sha", source)
        record_property("receipt", json.dumps(json.loads(receipt.read_text()), sort_keys=True))
    finally:
        state.unlink(missing_ok=True)
