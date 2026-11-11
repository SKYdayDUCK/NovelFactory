import os
from uuid import uuid4

import pytest
from agent_workshop import messaging
from agent_workshop.admin import enroll
from agent_workshop.auth import Denied, authenticate, connect
from agent_workshop.organization import bootstrap, claim_organization


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
        assert director.org_member and actor.org_member
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


def test_upgrade_from_w002_keeps_mail_without_inventing_an_origin(monkeypatch):
    from alembic import command
    from alembic.config import Config
    from psycopg import sql
    from sqlalchemy.engine import make_url

    base = os.environ["AW_DATABASE_URL"]
    name = "aw_upgrade_" + uuid4().hex
    with connect(base) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
    scoped = (
        make_url(base)
        .update_query_dict({"options": "-csearch_path=" + name})
        .render_as_string(hide_password=False)
    )
    monkeypatch.setenv("AW_DATABASE_URL", scoped)
    try:
        command.upgrade(Config("alembic.ini"), "w002")
        org, mid = "legacy", uuid4()
        with connect(scoped) as conn:
            # w002 predates installation ownership, current member grants and public roster fields.
            conn.execute("INSERT INTO aw_installations VALUES ('previous-install','nf-old',true)")
            conn.execute("""INSERT INTO aw_organizations (org_id,active_revision,preset,preset_sha,scope_ref)
                            VALUES ('legacy',1,'Minimal','historical-preset','organization')""")
            conn.execute("""INSERT INTO aw_org_revisions
                            (org_id,revision,preset,preset_sha,changed_by,change_kind)
                            VALUES ('legacy',1,'Minimal','historical-preset','manager','BOOTSTRAP')""")
            conn.execute("""INSERT INTO aw_employees
                            (org_id,employee_id,employment_revision,role_id,team_id,supervisor_id,scope_ref,status)
                            VALUES ('legacy','volume',1,'volume_director','production',NULL,'organization','ACTIVE'),
                                   ('legacy','actor-1',1,'character_actor','actors','volume','organization','ACTIVE')""")
            conn.execute("""INSERT INTO aw_employment_versions
                            SELECT org_id,employee_id,employment_revision,1,role_id,team_id,supervisor_id,scope_ref,status
                            FROM aw_employees WHERE org_id='legacy'""")
            conn.execute("INSERT INTO aw_edges VALUES ('legacy',1,'volume','actor-1','ASSIGN','organization')")
            conn.execute(
                """INSERT INTO aw_messages(org_id,message_id,operation_id,sender,sender_revision,recipient,
                recipient_revision,graph_revision,kind,scope_ref,root_id,hops,expires_at) VALUES
                (%s,%s,'old-send','volume',1,'actor-1',1,1,'ASSIGN','organization',%s,0,now()+interval '1 hour')""",
                (org, mid, mid),
            )
            conn.execute("INSERT INTO aw_deliveries VALUES(%s,%s,'actor-1',1,1,NULL)", (org, mid))
            conn.execute(
                "INSERT INTO aw_outbox(event_id,org_id,message_id) VALUES(%s,%s,%s)",
                (uuid4(), org, mid),
            )
        command.upgrade(Config("alembic.ini"), "head")
        with connect(scoped) as conn:
            claim_organization(conn, org, "previous-install")
            actor = authenticate(conn, enroll(conn, "previous-install", org, "actor-1", 1, 1))
            assert actor.org_member
            assert conn.execute(
                "SELECT 1 FROM aw_message_origins WHERE org_id=%s AND message_id=%s", (org, mid)
            ).fetchone() is None
            assert messaging.mailbox(conn, actor)["items"] == [
                {"sequence": 1, "status": "UNAVAILABLE"}
            ]
            assert (
                conn.execute(
                    "SELECT count(*) AS n FROM aw_messages WHERE message_id=%s", (mid,)
                ).fetchone()["n"]
                == 1
            )
    finally:
        with connect(base) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))
