"""Upgrade a populated W010 source cut with every original event unchanged."""

from uuid import uuid4

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from psycopg import sql
from sqlalchemy.engine import make_url

from agent_workshop.auth import connect
from agent_workshop.migrate import config
from agent_workshop.organization import bootstrap


def test_populated_w010_preserves_historical_cut_and_guarded_downgrade(dual_databases, monkeypatch):
    base = dual_databases["aw"]
    schema = "p57_aw_history_" + uuid4().hex
    with connect(base) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    dsn = (
        make_url(base)
        .update_query_dict({"options": "-csearch_path=" + schema})
        .render_as_string(hide_password=False)
    )
    monkeypatch.setenv("AW_DATABASE_URL", dsn)
    org, installation = "stage-six", str(uuid4())
    try:
        command.upgrade(config(), "w010")
        with connect(dsn) as conn:
            conn.execute(
                "INSERT INTO aw_installations (installation_id,nf_instance_id,enabled) VALUES (%s,%s,true)",
                (installation, str(uuid4())),
            )
            bootstrap(conn, org, installation_id=installation)

        def snapshot():
            with connect(dsn) as conn:
                return conn.execute(
                    "SELECT * FROM aw_change_log WHERE org_id=%s ORDER BY seq", (org,)
                ).fetchall()

        before = snapshot()
        assert before and any(r["source"] == "aw_employment_versions" for r in before)
        migrations = config()
        expected_head = ScriptDirectory.from_config(migrations).get_current_head()
        command.upgrade(migrations, "head")
        assert before == snapshot()
        with connect(dsn) as conn:
            assert (
                conn.execute("SELECT version_num FROM aw_alembic_version").fetchone()["version_num"] == expected_head
            )
            assert (
                conn.execute("SELECT cut FROM aw_source_cuts WHERE org_id=%s", (org,)).fetchone()["cut"]
                == before[-1]["seq"]
            )
        with pytest.raises(RuntimeError, match="paired backup"):
            command.downgrade(config(), "w010")
        assert before == snapshot()
    finally:
        with connect(base) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
