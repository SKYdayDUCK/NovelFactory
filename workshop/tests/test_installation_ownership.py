"""W1 installation isolation and upgrade from real W0 tokens and a populated W1 database."""
import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy.engine import make_url

from agent_workshop.admin import enroll
from agent_workshop.auth import Denied, authenticate, connect, digest
from agent_workshop.organization import bootstrap, claim_organization, roster
from agent_workshop.server import PROTOCOL, dispatch


@pytest.fixture
def isolated_database(monkeypatch):
    base = os.environ["AW_DATABASE_URL"]
    schema = "aw_upgrade_" + uuid4().hex
    with connect(base) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    dsn = make_url(base).set(drivername="postgresql").update_query_dict(
        {"options": "-csearch_path=" + schema}
    ).render_as_string(hide_password=False)
    monkeypatch.setenv("AW_DATABASE_URL", dsn)
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    try:
        yield dsn, config
    finally:
        with connect(base) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def capabilities_only(dsn, token, expected_instance):
    binding = {"database_url": dsn, "token": token}
    assert dispatch(binding) == ["aw_capabilities"]
    assert dispatch(binding, "aw_capabilities", {"protocol_version": PROTOCOL})["nf_instance_id"] == expected_instance
    with pytest.raises(Denied, match="^VIEW_DENIED$"):
        dispatch(binding, "aw_roster_get", {})
    with pytest.raises(Denied, match="^VIEW_DENIED$"):
        dispatch(binding, "aw_hire", {})


def test_w000_and_populated_w001_upgrade_keeps_old_tokens_scoped(isolated_database):
    dsn, config = isolated_database
    command.upgrade(config, "w000")
    tokens = {}
    with connect(dsn) as conn:
        for owner in ("left", "right"):
            token = uuid4().hex
            tokens[owner] = token
            conn.execute("INSERT INTO aw_installations VALUES (%s,%s,true)", (owner, "nf-" + owner))
            conn.execute(
                """INSERT INTO aw_bindings
                   (token_digest,installation_id,org_id,employee_id,employment_revision,grant_epoch,expires_at)
                   VALUES (%s,%s,'legacy','manager',1,1,now()+interval '1 hour')""",
                (digest(token), owner),
            )
    command.upgrade(config, "w001")
    with connect(dsn) as conn:
        conn.execute("INSERT INTO aw_organizations VALUES ('legacy',1,'Minimal','legacy-preset-hash','scope')")
        conn.execute("""INSERT INTO aw_org_revisions (org_id,revision,preset,preset_sha,changed_by,change_kind)
                        VALUES ('legacy',1,'Minimal','legacy-preset-hash','manager','BOOTSTRAP')""")
        conn.execute("""INSERT INTO aw_employees (org_id,employee_id,employment_revision,role_id,team_id,scope_ref,status)
                        VALUES ('legacy','manager',1,'workshop_manager','control','scope','ACTIVE'),
                               ('legacy','author',1,'author','author','scope','ACTIVE')""")
        conn.execute("""INSERT INTO aw_employment_versions
                        SELECT org_id,employee_id,employment_revision,1,role_id,team_id,supervisor_id,scope_ref,status
                        FROM aw_employees WHERE org_id='legacy'""")
        conn.execute("INSERT INTO aw_edges VALUES ('legacy',1,'manager','author','REPORT','scope')")
        conn.execute("""INSERT INTO aw_org_operations VALUES
                        ('legacy','manager',1,'legacy-key','legacy-request','{"revision":1}')""")

    def history():
        # Compare every original column even if a later migration adds metadata to the same row.
        columns = {
            "aw_employees": "org_id employee_id employment_revision role_id team_id supervisor_id scope_ref status",
            "aw_employment_versions": "org_id employee_id employment_revision org_revision role_id team_id supervisor_id scope_ref status",
            "aw_org_revisions": "org_id revision preset preset_sha changed_by change_kind created_at",
            "aw_edges": "org_id graph_revision src dst kind scope_ref",
            "aw_org_operations": "org_id employee_id employment_revision operation_id request_sha receipt",
        }
        with connect(dsn) as conn:
            return {
                table: conn.execute(
                    sql.SQL("SELECT {} FROM {} WHERE org_id='legacy' ORDER BY 1,2,3").format(
                        sql.SQL(",").join(map(sql.Identifier, names.split())), sql.Identifier(table)
                    )
                ).fetchall()
                for table, names in columns.items()
            }

    before = history()
    command.upgrade(config, "head")
    for owner, token in tokens.items():
        capabilities_only(dsn, token, "nf-" + owner)
    with connect(dsn) as conn:
        assert conn.execute("SELECT owner_installation_id FROM aw_organizations").fetchone()["owner_installation_id"] is None
        with pytest.raises(Denied, match="^ORGANIZATION_UNAVAILABLE$"):
            enroll(conn, "left", "legacy", "manager", 1, 1)
        claim_organization(conn, "legacy", "left")
        claim_organization(conn, "legacy", "left")
        with pytest.raises(Denied, match="^ORGANIZATION_UNAVAILABLE$"):
            claim_organization(conn, "legacy", "right")
        with pytest.raises(Denied, match="^ORGANIZATION_UNAVAILABLE$"):
            enroll(conn, "right", "legacy", "manager", 1, 1)
        with pytest.raises(Denied, match="^STALE_EMPLOYMENT$"):
            enroll(conn, "left", "legacy", "manager", 2, 1)
        renewed = enroll(conn, "left", "legacy", "manager", 1, 1)
    for owner, token in tokens.items():
        capabilities_only(dsn, token, "nf-" + owner)
    binding = {"database_url": dsn, "token": renewed}
    assert "aw_hire" in dispatch(binding)
    view = dispatch(binding, "aw_roster_get", {})
    assert view["revision"] == 1 and {row["employee_id"] for row in view["employees"]} == {"manager", "author"}
    assert history() == before


def test_new_org_has_one_provisioning_installation(isolated_database):
    dsn, config = isolated_database
    command.upgrade(config, "head")
    with connect(dsn) as conn:
        conn.execute("INSERT INTO aw_installations VALUES ('one','nf-one',true)")
        first = enroll(conn, "one", "future", "manager", 1, 1)
        bootstrap(conn, "future")
        conn.execute("INSERT INTO aw_installations VALUES ('two','nf-two',true)")
        with pytest.raises(Denied, match="^INSTALLATION_REQUIRED$"):
            bootstrap(conn, "other")
        with pytest.raises(Denied, match="^ORGANIZATION_UNAVAILABLE$"):
            bootstrap(conn, "future", installation_id="two")
        bootstrap(conn, "other", installation_id="two")
        member = enroll(conn, "two", "other", "manager", 1, 1)
        session = authenticate(conn, member)
        assert roster(conn, session)["org_id"] == "other"
        with pytest.raises(Denied, match="^ORGANIZATION_UNAVAILABLE$"):
            enroll(conn, "two", "future", "manager", 1, 1)
        with pytest.raises(Denied, match="^STALE_EMPLOYMENT$"):
            enroll(conn, "one", "future", "not-hired", 1, 1)
    capabilities_only(dsn, first, "nf-one")
