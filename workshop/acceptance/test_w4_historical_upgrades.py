"""Upgrade populated signed W3, original W4 and the W4b graph with a colliding W2 revision."""
import json
import os
import subprocess
import sys
import tarfile

import psycopg
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from psycopg import sql

from test_w1_historical_upgrade import WORKSHOP, run
from test_w2_historical_upgrades import columns, snapshot

SIGNED_W3 = "e6763d844f023d7c6a13060dbaebca0416fe3099"
ORIGINAL_W4 = "71b471416c9407e348d4bdce817588def5f11d5b"
LEGACY_W4A = "6f57575419559dc5438b38bc2ab68adaf94bc30e"
REHEARSED_W4B = "4f7d8ca35bdf0e04045986950edf5819bd572e80"


def release(directory, sha):
    directory.mkdir()
    archive = directory / "source.tar"
    subprocess.run(["git", "archive", "--format=tar", "--output", archive, sha, "workshop"],
                   cwd=WORKSHOP.parent, check=True)
    with tarfile.open(archive) as contents:
        contents.extractall(directory, filter="data")
    return directory / "workshop"


@pytest.mark.parametrize(("source", "revision", "variable"), [
    (SIGNED_W3, "w003b", "AW_W4_FROM_W3_DATABASE_URL"),
    (ORIGINAL_W4, "w004", "AW_W4_FROM_W4_DATABASE_URL"),
    (REHEARSED_W4B, "w004b", "AW_W4_FROM_W4B_DATABASE_URL"),
])
def test_release_history_reaches_canonical_routes(source, revision, variable, tmp_path, record_property):
    dsn = os.environ[variable]
    with psycopg.connect(dsn) as conn:
        assert conn.info.server_version // 10000 == 17
        assert conn.execute("SELECT to_regclass('aw_alembic_version')").fetchone() == (None,)
    seed_source = LEGACY_W4A if revision == "w004b" else source
    old = release(tmp_path/"seed", seed_source)
    run(old, dsn, "alembic", "upgrade", "head")
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(str(old / p) for p in ("src", "tests")),
           "AW_DATABASE_URL": dsn}
    if revision == "w003b":
        # Real pre-W4 account, slot, employee credential, pending request and HELD amount.
        subprocess.run([sys.executable, "-c", "from test_delegation import setup_slot, make; "
                        "dsn, org, installation, token = setup_slot(); make(dsn, token)"],
                       cwd=old, env=env, check=True)
    else:
        # This archived application creates the skill, snapshots, holds and requests.
        # Its two settled partner receipt fixtures have no supplier/model calls.
        subprocess.run([sys.executable, str(WORKSHOP/"tests"/"test_legacy_paid_reviews.py")],
                       cwd=old, env=env, check=True, capture_output=True)
    if source != seed_source:
        installed = release(tmp_path/"installed", source)
        run(installed, dsn, "alembic", "upgrade", "head")
    projection = "SELECT to_regclass('aw_current_message_edges') IS NOT NULL"
    with psycopg.connect(dsn) as conn:
        assert conn.execute("SELECT version_num FROM aw_alembic_version").fetchall() == [(revision,)]
        assert conn.execute(projection).fetchone() == (revision == "w003b",)
        tables = columns(conn)
        before = snapshot(conn, tables)
        assert before["aw_delegations"][0] == before["aw_budget_holds"][0] > 0
        if revision != "w003b":
            assert before["aw_reviews"][0] == before["aw_skill_snapshots"][0] == 3
            assert before["aw_remote_receipts"][0] == 2
    script = ScriptDirectory.from_config(Config(str(WORKSHOP/"alembic.ini")))
    head = script.get_current_head()
    assert head and "w004c" in {r.revision for r in script.walk_revisions(head=head)}
    assert script.get_revision("w004").down_revision == "w003"
    assert set(script.get_revision("w004b").down_revision) == {"w004a", "w003b"}
    run(WORKSHOP, dsn, "alembic", "upgrade", "head")
    run(WORKSHOP, dsn, "alembic", "upgrade", "head")
    with psycopg.connect(dsn) as conn:
        assert conn.execute("SELECT version_num FROM aw_alembic_version").fetchall() == [(head,)]
        assert conn.execute(projection).fetchone() == (True,)
        assert before == snapshot(conn, tables)
        # A frozen reviewer->lead ASK is retained in raw W1 history and filtered from the signed active view.
        for table, expected in (("aw_edges", revision != "w003b"), ("aw_current_message_edges", False)):
            query = sql.SQL("""SELECT EXISTS (SELECT 1 FROM {} WHERE src='reviewer'
                             AND dst='review-lead' AND kind='ASK')""").format(sql.Identifier(table))
            assert conn.execute(query).fetchone() == (expected,)
        assert conn.execute("SELECT count(*) FROM aw_current_message_edges WHERE kind='REPORT'").fetchone()[0] > 0
    record_property("archived_application", seed_source)
    record_property("starting_schema_source", source)
    record_property("upgrade", f"{revision}->{head}")
    record_property("preserved_rows_and_hashes", json.dumps(before, sort_keys=True))
