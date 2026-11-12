"""Restore a W2 database with a genuine retained sender digest and preserve its eight employees."""
import os
from uuid import uuid4

import pytest
from agent_workshop import messaging
from agent_workshop.admin import enroll
from agent_workshop.auth import Denied, authenticate, connect


def test_w002a_origin_hash_does_not_inherit_new_organization_membership(monkeypatch, tmp_path):
    from alembic import command
    from alembic.config import Config
    from legacy_w002 import seed_historical_w002
    from psycopg import sql
    from sqlalchemy.engine import make_url

    from agent_workshop.organization import claim_organization

    base = os.environ["AW_DATABASE_URL"]
    name = "aw_upgrade_" + uuid4().hex
    with connect(base) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
    scoped = make_url(base).update_query_dict(
        {"options": "-csearch_path=" + name}
    ).render_as_string(hide_password=False)
    monkeypatch.setenv("AW_DATABASE_URL", scoped)
    try:
        org = "legacy"
        old = seed_historical_w002(scoped, org, tmp_path, revision="w002a")
        command.upgrade(Config("alembic.ini"), "w002c")
        with connect(scoped) as conn:
            assert conn.execute("SELECT owner_installation_id FROM aw_organizations").fetchone()["owner_installation_id"] is None
            assert not conn.execute("SELECT org_member FROM aw_bindings").fetchone()["org_member"]
            origins = conn.execute("SELECT count(*) AS n FROM aw_message_origins WHERE org_id=%s", (org,)).fetchone()["n"]
            assert origins == 1
            claim_organization(conn, org, old["installation_id"])
            actor = authenticate(conn, enroll(conn, old["installation_id"], org, "actor-1", 1, 1))
            assert messaging.mailbox(conn, actor)["items"] == [{"sequence": 1, "status": "UNAVAILABLE"}]
            with pytest.raises(Denied, match="SENDER_BINDING_UNVERIFIED"):
                messaging.body_get(conn, actor, old["message_id"])
            assert conn.execute("SELECT count(*) AS n FROM aw_messages WHERE message_id=%s", (old["message_id"],)).fetchone()["n"] == 1
            assert conn.execute("SELECT count(*) AS n FROM aw_employees WHERE org_id=%s", (org,)).fetchone()["n"] == old["member_count"]
            director = authenticate(conn, enroll(conn, old["installation_id"], org, "volume", 1, 1))
            fresh = messaging.send(conn, director, "after-reissue", "actor-1", "ASSIGN", "organization", 1, 60)
            inbox = messaging.mailbox(conn, actor)["items"]
            assert inbox[0] == {"sequence": 1, "status": "UNAVAILABLE"}
            assert inbox[1]["message_id"] == fresh["message_id"]
    finally:
        with connect(base) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))
