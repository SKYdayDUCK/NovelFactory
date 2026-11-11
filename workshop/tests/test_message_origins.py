import os
from uuid import uuid4

import pytest
from agent_workshop import messaging
from agent_workshop.admin import enroll
from agent_workshop.auth import Denied, authenticate, connect
from agent_workshop.organization import bootstrap


@pytest.fixture
def team():
    org, install = uuid4().hex, uuid4().hex
    dsn = os.environ["AW_DATABASE_URL"]
    with connect(dsn) as conn:
        conn.execute("INSERT INTO aw_installations VALUES (%s,%s,true)", (install, str(uuid4())))
        bootstrap(conn, org, "Standard", installation_id=install)
        tokens = {
            name: enroll(conn, install, org, name, 1, 1)
            for name in ("manager", "volume", "actor-1")
        }
    return dsn, org, tokens


def test_sender_credential_revoke_blocks_delivery_with_preserved_receipt(team):
    dsn, org, tokens = team
    with connect(dsn) as c:
        director = authenticate(c, tokens["volume"])
        actor = authenticate(c, tokens["actor-1"])
        b = messaging.register_body(c, director, "b", "actor-1", "organization", "fixture")
        r = messaging.send(
            c, director, "key", "actor-1", "ASSIGN", "organization", 1, 30, body_ref=b["body_ref"]
        )
        assert messaging.body_get(c, actor, r["message_id"])["content"] == "fixture"
    with connect(dsn) as c:
        c.execute(
            "UPDATE aw_bindings SET revoked=true,grant_epoch=grant_epoch+1 WHERE token_digest=%s",
            (director.binding_digest,),
        )
        assert messaging.mailbox(c, actor)["items"] == [{"sequence": 1, "status": "UNAVAILABLE"}]
        with pytest.raises(Denied, match="SENDER_BINDING_UNVERIFIED"):
            messaging.ack(c, actor, r["message_id"])
        with pytest.raises(Denied, match="SENDER_BINDING_UNVERIFIED"):
            messaging.body_get(c, actor, r["message_id"])
        assert (
            c.execute("SELECT count(*) AS n FROM aw_outbox WHERE org_id=%s", (org,)).fetchone()["n"]
            == 1
        )


@pytest.mark.parametrize("historical_authority", ["capability_only", "other_installation"])
def test_retained_sender_requires_membership_in_the_owning_installation(team, historical_authority):
    dsn, org, tokens = team
    with connect(dsn) as conn:
        director = authenticate(conn, tokens["volume"])
        actor = authenticate(conn, tokens["actor-1"])
        body = messaging.register_body(conn, director, "kept-body", "actor-1", "organization", "fixture")
        receipt = messaging.send(conn, director, "kept-send", "actor-1", "ASSIGN", "organization", 1, 30, body_ref=body["body_ref"])
        assert messaging.body_get(conn, actor, receipt["message_id"])["content"] == "fixture"
    with connect(dsn) as conn:
        if historical_authority == "capability_only":
            conn.execute("UPDATE aw_bindings SET org_member=false WHERE token_digest=%s", (director.binding_digest,))
        else:
            other = uuid4().hex
            conn.execute("INSERT INTO aw_installations VALUES(%s,'other-nf',true)", (other,))
            conn.execute("UPDATE aw_bindings SET installation_id=%s WHERE token_digest=%s", (other, director.binding_digest))
        assert messaging.mailbox(conn, actor)["items"] == [{"sequence": 1, "status": "UNAVAILABLE"}]
        for method in (messaging.ack, messaging.body_get):
            with pytest.raises(Denied, match="SENDER_BINDING_UNVERIFIED"):
                method(conn, actor, receipt["message_id"])
        assert conn.execute("SELECT count(*) AS n FROM aw_outbox WHERE org_id=%s", (org,)).fetchone()["n"] == 1


def test_upgrade_from_w002_keeps_mail_without_inventing_an_origin(monkeypatch, tmp_path):
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
        old = seed_historical_w002(scoped, org, tmp_path)
        command.upgrade(Config("alembic.ini"), "w002b")
        with connect(scoped) as conn:
            assert conn.execute("SELECT owner_installation_id FROM aw_organizations").fetchone()["owner_installation_id"] is None
            assert not conn.execute("SELECT org_member FROM aw_bindings").fetchone()["org_member"]
            claim_organization(conn, org, old["installation_id"])
            actor = authenticate(conn, enroll(conn, old["installation_id"], org, "actor-1", 1, 1))
            assert messaging.mailbox(conn, actor)["items"] == [{"sequence": 1, "status": "UNAVAILABLE"}]
            with pytest.raises(Denied, match="SENDER_BINDING_UNVERIFIED"):
                messaging.body_get(conn, actor, old["message_id"])
            assert conn.execute("SELECT count(*) AS n FROM aw_messages WHERE message_id=%s", (old["message_id"],)).fetchone()["n"] == 1
            assert conn.execute("SELECT count(*) AS n FROM aw_employees WHERE org_id=%s", (org,)).fetchone()["n"] == old["member_count"]
    finally:
        with connect(base) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))
