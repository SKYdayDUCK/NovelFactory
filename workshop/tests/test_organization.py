import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest

from agent_workshop.auth import Denied, Session, connect
from agent_workshop.organization import PRESETS, TOOLS, bootstrap, require_employee, roster


def identity(org, employee="manager", rev=1):
    return Session(org, employee, rev, 1, org, org_member=True)


def install(conn, installation):
    conn.execute(
        "INSERT INTO aw_installations (installation_id, nf_instance_id) VALUES (%s,%s)",
        (installation, "fixture-nf"),
    )


@pytest.fixture
def team():
    url = os.environ["AW_DATABASE_URL"]
    org = uuid4().hex
    with connect(url) as conn:
        install(conn, org)
        bootstrap(conn, org, "Standard", installation_id=org)
    return url, org


def call(conn, session, action, **fields):
    return TOOLS["aw_" + action][2](conn, session, **fields)


@pytest.mark.parametrize("preset", PRESETS)
def test_frozen_presets_repeated_bootstrap(preset):
    url = os.environ["AW_DATABASE_URL"]
    org = uuid4().hex
    with connect(url) as conn:
        install(conn, org)
        first = bootstrap(conn, org, preset, installation_id=org)
        assert bootstrap(conn, org, preset, installation_id=org) == first
        view = roster(conn, identity(org))
        assert len({e["employee_id"] for e in view["employees"]}) == len(view["employees"])
        assert sum(e["role_id"] == "character_actor" for e in view["employees"]) == 3
        assert len(first["preset_sha"]) == 64
        with pytest.raises(Denied, match="BOOTSTRAP_MISMATCH"):
            bootstrap(conn, org, "Minimal" if preset != "Minimal" else "Standard", installation_id=org)


def test_tenure_acl_history_and_reactivation(team):
    url, org = team
    manager, actor = identity(org), identity(org, "actor-1")
    with connect(url) as conn:
        assert {x["employee_id"] for x in roster(conn, actor)["employees"]} == {"volume", "actor-1"}
        with pytest.raises(Denied, match="MANAGER_REQUIRED"):
            call(
                conn, actor, "suspend", operation_id="a", expected_revision=1, employee_id="actor-2"
            )
        with pytest.raises(Denied, match="INVALID_SUPERVISOR"):
            call(
                conn,
                manager,
                "transfer",
                operation_id="x",
                expected_revision=1,
                employee_id="actor-1",
                role_id="character_actor",
                supervisor_id="actor-1",
                team_id="actors",
            )
        args = dict(operation_id="r", expected_revision=1, employee_id="actor-1")
        receipt = call(conn, manager, "suspend", **args)
        assert call(conn, manager, "suspend", **args) == receipt
        with pytest.raises(Denied, match="IDEMPOTENCY_MISMATCH"):
            call(conn, manager, "end", **args)
        with pytest.raises(Denied, match="STALE_EMPLOYMENT"):
            roster(conn, actor)
        call(
            conn,
            manager,
            "transfer",
            operation_id="s",
            expected_revision=2,
            employee_id="actor-1",
            role_id="character_actor",
            supervisor_id="volume",
            team_id="actors",
        )
        with pytest.raises(Denied, match="STALE_EMPLOYMENT"):
            roster(conn, actor)
        assert require_employee(conn, identity(org, "actor-1", 3))["status"] == "ACTIVE"
        call(conn, manager, "end", operation_id="z", expected_revision=3, employee_id="actor-1")
        history = conn.execute(
            """SELECT status FROM aw_employment_versions WHERE org_id=%s AND
            employee_id='actor-1' ORDER BY employment_revision""",
            (org,),
        ).fetchall()
        assert [x["status"] for x in history] == ["ACTIVE", "SUSPENDED", "ACTIVE", "ENDED"]


@pytest.mark.parametrize("same_key", [True, False])
def test_competing_hires_use_postgres_connections(team, same_key):
    url, org = team
    barrier = Barrier(2)

    def hire(key):
        with connect(url) as conn:
            barrier.wait(5)
            try:
                return call(
                    conn,
                    identity(org),
                    "hire",
                    operation_id=str(key),
                    expected_revision=1,
                    employee_id="fourth",
                    role_id="character_actor",
                    supervisor_id="volume",
                    team_id="actors",
                )
            except Denied as exc:
                return str(exc)

    with ThreadPoolExecutor(2) as pool:
        a, b = list(pool.map(hire, [1, 1 if same_key else 2]))
    if same_key:
        assert a == b and a["revision"] == 2
    else:
        assert "STALE_ORG_REVISION" in (a, b)
    with connect(url) as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM aw_employees WHERE org_id=%s AND employee_id='fourth'",
                (org,),
            ).fetchone()["n"]
            == 1
        )
