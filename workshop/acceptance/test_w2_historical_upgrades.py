"""Upgrade both historical lines using data committed by their archived service code."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile

from alembic.config import Config
from alembic.script import ScriptDirectory
import psycopg
from psycopg import sql
import pytest

from test_w1_historical_upgrade import WORKSHOP, client, only_capabilities, run


W1 = "d039839fe68f6403ea6e5590af3ed787318d5b5f"
W2 = "0da5b7c8fab5321fa50d2a04a516f3fd95432539"
OLD_MESSAGE = """
import json
import sys
from agent_workshop.auth import authenticate, connect
from agent_workshop.messaging import ack, register_body, send
from pathlib import Path
director, actor = [json.loads(Path(p).read_text()) for p in sys.argv[1:]]
with connect(actor["database_url"]) as conn:
    sender = authenticate(conn, director["token"])
    receiver = authenticate(conn, actor["token"])
    body = register_body(conn, sender, "old-body", "actor-1", "organization", "historical message")
    result = send(conn, sender, "old-send", "actor-1", "ASSIGN", "organization", 1,
                  3600, body_ref=body["body_ref"])
    assert ack(conn, receiver, result["message_id"])["status"] == "ACKNOWLEDGED"
"""


def columns(conn):
    names = conn.execute("""
        SELECT c.table_name, c.column_name FROM information_schema.columns c
        JOIN information_schema.tables t USING (table_schema, table_name)
        WHERE c.table_schema=current_schema() AND t.table_type='BASE TABLE'
          AND c.table_name LIKE 'aw_%' AND c.table_name<>'aw_alembic_version'
        ORDER BY c.table_name,c.ordinal_position
    """).fetchall()
    tables = {}
    for table, name in names:
        tables.setdefault(table, []).append(name)
    return tables


def snapshot(conn, tables):
    result = {}
    for table, fields in tables.items():
        query = sql.SQL("SELECT row_to_json(r)::text FROM (SELECT {} FROM {}) r ORDER BY 1").format(
            sql.SQL(", ").join(map(sql.Identifier, fields)), sql.Identifier(table)
        )
        rows = [r[0] for r in conn.execute(query)]
        result[table] = (len(rows), hashlib.sha256(json.dumps(rows).encode()).hexdigest())
    return result


@pytest.mark.parametrize(("source", "start", "db_var"), [
    (W1, "w001b", "AW_W2_FROM_W1_DATABASE_URL"),
    (W2, "w002", "AW_W2_FROM_W2_DATABASE_URL"),
], ids=["released-w1", "historical-w2"])
def test_upgrade_historical_service(source, start, db_var, tmp_path, record_property):
    dsn = os.environ[db_var]
    with psycopg.connect(dsn) as conn:
        assert conn.info.server_version // 10000 == 17
        assert conn.execute("SELECT to_regclass('aw_alembic_version')").fetchone() == (None,)
    archive = tmp_path / "old.tar"
    subprocess.run(["git", "archive", "--format=tar", "--output", archive, source, "workshop"],
                   cwd=WORKSHOP.parent, check=True)
    with tarfile.open(archive) as items:
        items.extractall(tmp_path, filter="data")
    old = tmp_path / "workshop"
    actor = tmp_path / "actor.json"
    director = tmp_path / "director.json"
    run(old, dsn, "alembic", "upgrade", "head")
    run(old, dsn, "agent_workshop.admin", "install", "install", "historical-nf")
    owner = ["--installation", "install"] if start == "w001b" else []
    run(old, dsn, "agent_workshop.admin", "bootstrap", "org", "--preset", "Standard", *owner)
    run(old, dsn, "agent_workshop.admin", "enroll", "install", "org", "actor-1", actor, "--revision", "1")
    if start == "w002":
        run(old, dsn, "agent_workshop.admin", "enroll", "install", "org", "volume", director, "--revision", "1")
        subprocess.run([sys.executable, "-c", OLD_MESSAGE, director, actor], check=True, cwd=old,
                       env={**os.environ, "PYTHONPATH": str(old / "src")})
    with psycopg.connect(dsn) as conn:
        assert conn.execute("SELECT version_num FROM aw_alembic_version").fetchall() == [(start,)]
        tables = columns(conn)
        before = snapshot(conn, tables)
        assert before["aw_employees"][0] > 2 and before["aw_employment_versions"][0] > 2
        if start == "w002":
            for table in ("aw_deliveries", "aw_outbox", "aw_messages", "aw_message_bodies"):
                assert before[table][0] == 1
    scripts = ScriptDirectory.from_config(Config(str(WORKSHOP / "alembic.ini")))
    current = scripts.get_current_head()
    assert current is not None
    assert "w002c" in {rev.revision for rev in scripts.walk_revisions(base="base", head=current)}
    assert set(scripts.get_revision("w002c").down_revision) == {"w001b", "w002b"}
    run(WORKSHOP, dsn, "alembic", "upgrade", "head")
    run(WORKSHOP, dsn, "alembic", "upgrade", "head")
    with psycopg.connect(dsn) as conn:
        assert conn.execute("SELECT version_num FROM aw_alembic_version").fetchall() == [(current,)]
        assert snapshot(conn, tables) == before
        assert conn.execute("SELECT count(*) FROM aw_message_origins").fetchone() == (0,)
        assert conn.execute("SELECT bool_and(org_member) FROM aw_bindings").fetchone() == (start == "w001b",)
    record_property("source_sha", source)
    record_property("starting_revision", start)
    record_property("ending_revision", current)
    record_property("preserved_table_row_counts", json.dumps({k: v[0] for k, v in before.items()}, sort_keys=True))

    async def verify():
        async with client(WORKSHOP, actor) as original:
            if start == "w001b":
                reply = await original.call_tool("aw_roster_get", {})
                assert not reply.isError and reply.structuredContent["org_id"] == "org"
            else:
                await only_capabilities(original)
                run(WORKSHOP, dsn, "agent_workshop.admin", "claim-org", "org", "install")
                fresh = tmp_path / "new-actor.json"
                run(WORKSHOP, dsn, "agent_workshop.admin", "enroll", "install", "org",
                    "actor-1", fresh, "--revision", "1")
                await only_capabilities(original)
                async with client(WORKSHOP, fresh) as authorized:
                    mailbox = await authorized.call_tool("aw_mailbox_get", {})
                    assert not mailbox.isError and mailbox.structuredContent["items"] == [
                        {"sequence": 1, "status": "UNAVAILABLE"}
                    ]
    asyncio.run(verify())
