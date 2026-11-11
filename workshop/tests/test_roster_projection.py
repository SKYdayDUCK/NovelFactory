"""PostgreSQL checks for public roster projections and independent staffing slots."""
import os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from psycopg.errors import UniqueViolation
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

from agent_workshop.auth import Denied, Session, connect
from agent_workshop.organization import TOOLS, bootstrap, roster, state
from agent_workshop.roster_projection import catalog_binding


@pytest.fixture
def organization():
    url = os.environ["AW_DATABASE_URL"]
    org, installation = uuid4().hex, uuid4().hex
    with connect(url) as conn:
        conn.execute("INSERT INTO aw_installations VALUES (%s,%s,true)", (installation, "nf-test"))
        bootstrap(conn, org, "Standard", installation_id=installation)
    return url, org, installation


def member(org, installation, employee_id="manager", revision=1):
    return Session(org, employee_id, revision, 1, installation, org_member=True)


def hire(conn, manager, employee_id, revision, **kwargs):
    return TOOLS["aw_hire"][2](
        conn, manager, employee_id=employee_id, expected_revision=revision,
        operation_id="hire-" + employee_id, role_id="character_actor",
        supervisor_id="volume", team_id="actors", **kwargs,
    )


def test_roster_privacy_and_persistent_public_names(organization):
    url, org, installation = organization
    manager = member(org, installation)
    with connect(url) as conn:
        hire(conn, manager, "performer-a", 1, display_name="林青", slot_id="speaking-a")
        hire(conn, manager, "performer-b", 2)
    with connect(url) as conn:
        actor = member(org, installation, "performer-a")
        view = roster(conn, actor)
        rows = {row["employee_id"]: row for row in view["employees"]}
        assert set(rows) == {"volume", "performer-a"}
        own, public = rows["performer-a"], rows["volume"]
        assert own["display_name"] == "林青" and own["slot_id"] == "speaking-a"
        assert own["capability_profile"] == {"organization_role": "character_actor",
                                             "nf_role_modes": ["character_actor"]}
        assert own["skill_manifest_ref"] == {"nf_commit": catalog_binding()["nf_commit"],
                                             "catalog_sha256": catalog_binding()["skill_catalog_hash"]}
        assert own["authorized_scope"] == {"organization": org, "scope_ref": "organization"}
        assert own["allowed_routes"] and all(r["src"] == "performer-a" for r in own["allowed_routes"])
        assert public == {"employee_id": "volume", "role_id": "volume_director", "team_id": "production",
                          "supervisor_id": None, "employment_revision": 1, "status": "ACTIVE",
                          "display_name": "volume"}
        admin_rows = {r["employee_id"]: r for r in roster(conn, manager)["employees"]}
        assert admin_rows["performer-a"]["display_name"] == "林青"
        assert admin_rows["performer-b"]["display_name"] == "performer-b"
        assert "authorized_scope" not in admin_rows["performer-a"]
        assert admin_rows["manager"]["skill_manifest_ref"] is None
        assert admin_rows["manager"]["capability_profile"]["nf_role_modes"] == []
        # The proposed edge closes the existing manager chain chief -> volume -> chief.
        with pytest.raises(Denied, match="REASSIGN_REPORTS_FIRST|SUPERVISOR_CYCLE"):
            TOOLS["aw_transfer"][2](
                conn, manager, operation_id="cycle", expected_revision=3, employee_id="chief",
                role_id="character_actor", supervisor_id="volume", team_id="production",
            )
        unchanged = roster(conn, manager)
        assert unchanged["revision"] == 3
        assert next(r for r in unchanged["employees"] if r["employee_id"] == "chief")["supervisor_id"] == "manager"
        assert "routes" not in state(conn, actor)
        assert all("allowed_routes" not in row for row in state(conn, actor)["employees"])
        assert conn.execute(
            "SELECT display_name,slot_id FROM aw_employment_versions WHERE org_id=%s AND employee_id=%s",
            (org, "performer-b"),
        ).fetchone() == {"display_name": "performer-b", "slot_id": "performer-b"}


def test_competing_employees_cannot_claim_one_active_slot(organization):
    url, org, installation = organization
    manager = member(org, installation)
    barrier = Barrier(2)

    def claim(employee_id):
        with connect(url) as conn:
            barrier.wait(10)
            try:
                hire(conn, manager, employee_id, 1, slot_id="speaking")
            except Denied as error:
                return employee_id, str(error)
            return employee_id, "OK"

    with ThreadPoolExecutor(2) as pool:
        attempts = dict(pool.map(claim, ["student-a", "student-b"]))
    assert sorted(attempts.values()) == ["OK", "STALE_ORG_REVISION"]
    winner = next(emp for emp, outcome in attempts.items() if outcome == "OK")
    loser = next(emp for emp, outcome in attempts.items() if outcome != "OK")
    with connect(url) as conn:
        # Retrying with a fresh org revision exercises slot occupancy independently of CAS.
        with pytest.raises(Denied, match="SLOT_OCCUPIED"):
            hire(conn, manager, loser, 2, slot_id="speaking")
        assert conn.execute(
            "SELECT employee_id FROM aw_employees WHERE org_id=%s AND slot_id='speaking' AND status='ACTIVE'",
            (org,),
        ).fetchall() == [{"employee_id": winner}]
        with pytest.raises(UniqueViolation), conn.transaction():
            conn.execute("UPDATE aw_employees SET slot_id='speaking' WHERE org_id=%s AND employee_id='actor-1'",
                         (org,))
        TOOLS["aw_suspend"][2](conn, manager, operation_id="pause", expected_revision=2, employee_id=winner)
        assert hire(conn, manager, loser, 3, slot_id="speaking")["revision"] == 4
        with pytest.raises(Denied, match="SLOT_OCCUPIED"):
            TOOLS["aw_transfer"][2](
                conn, manager, operation_id="reactivate", expected_revision=4, employee_id=winner,
                role_id="character_actor", supervisor_id="volume", team_id="actors",
            )
        assert conn.execute(
            """SELECT display_name,slot_id,status FROM aw_employment_versions
               WHERE org_id=%s AND employee_id=%s ORDER BY employment_revision""", (org, winner),
        ).fetchall() == [
            {"display_name": winner, "slot_id": "speaking", "status": status}
            for status in ("ACTIVE", "SUSPENDED")
        ]



def test_existing_w001_directory_and_tenure_backfill():
    root = Path(__file__).resolve().parents[1]
    scripts = ScriptDirectory.from_config(Config(str(root / "alembic.ini")))
    engine = create_engine(make_url(os.environ["AW_DATABASE_URL"]).set(drivername="postgresql+psycopg"))
    try:
        with engine.connect() as conn:
            # The running Workshop schema stays available, and connection close rolls this fixture back.
            schema = "legacy_directory_" + uuid4().hex
            conn.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
            conn.exec_driver_sql(f'SET LOCAL search_path TO "{schema}"')
            with Operations.context(MigrationContext.configure(conn)):
                scripts.get_revision("w000").module.upgrade()
                scripts.get_revision("w001").module.upgrade()
                conn.exec_driver_sql("""
                    INSERT INTO aw_organizations VALUES ('legacy',2,'Standard','frozen','org');
                    INSERT INTO aw_org_revisions
                        (org_id,revision,preset,preset_sha,changed_by,change_kind)
                        VALUES ('legacy',1,'Standard','frozen','manager','BOOTSTRAP'),
                               ('legacy',2,'Standard','frozen','manager','SUSPEND');
                    INSERT INTO aw_employees VALUES
                        ('legacy','performer',2,'character_actor','actors',NULL,'org','SUSPENDED');
                    INSERT INTO aw_employment_versions VALUES
                        ('legacy','performer',1,1,'character_actor','actors',NULL,'org','ACTIVE'),
                        ('legacy','performer',2,2,'character_actor','actors',NULL,'org','SUSPENDED');
                """)
                scripts.get_revision("w001a").module.upgrade()
                scripts.get_revision("w001b").module.upgrade()
            assert conn.exec_driver_sql(
                "SELECT employee_id,display_name,slot_id,status FROM aw_employees"
            ).all() == [("performer", "performer", "performer", "SUSPENDED")]
            assert conn.exec_driver_sql(
                """SELECT employment_revision,display_name,slot_id,status FROM aw_employment_versions
                   ORDER BY employment_revision"""
            ).all() == [(1, "performer", "performer", "ACTIVE"),
                        (2, "performer", "performer", "SUSPENDED")]
    finally:
        engine.dispose()
