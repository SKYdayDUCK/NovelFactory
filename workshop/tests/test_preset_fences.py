import os
from uuid import uuid4

import pytest
from agent_workshop import messaging, presets
from agent_workshop.admin import enroll
from agent_workshop.auth import Denied, authenticate, connect
from agent_workshop.delegation import delegate
from agent_workshop.organization import TOOLS, bootstrap, employee
from agent_workshop.provision import allow_slot


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


def switch(conn, manager, target, key):
    view = presets.preview(conn, manager, target)
    return presets.apply_preset(conn, manager, target, key, view["revision"], view["preview_sha"])


def test_preview_cas_switch_across_four_presets_and_restore_tenure(team):
    dsn, org, tokens = team
    with connect(dsn) as c:
        manager = authenticate(c, tokens["manager"])
        p = presets.preview(c, manager, "Minimal")
        assert set(p["suspend"]) == {"review-lead", "reviewer", "writer"}
        original = bootstrap(c, org, "Standard", installation_id=manager.installation_id)
        first = switch(c, manager, "Minimal", "1")
        assert bootstrap(c, org, "Standard", installation_id=manager.installation_id) == original
        assert (
            presets.apply_preset(c, manager, "Minimal", "1", p["revision"], p["preview_sha"])
            == first
        )
        assert employee(c, org, "reviewer")["status"] == "SUSPENDED"
        with pytest.raises(Denied, match="STALE_PREVIEW"):
            presets.apply_preset(c, manager, "Minimal", "old", p["revision"], p["preview_sha"])
        switch(c, manager, "Long-form", "2")
        assert employee(c, org, "reviewer")["employment_revision"] == 3
        switch(c, manager, "Research-Audit", "3")
        assert employee(c, org, "memory")["status"] == "SUSPENDED"
        assert employee(c, org, "world")["status"] == "ACTIVE"
        switch(c, manager, "Standard", "4")
        assert employee(c, org, "world")["status"] == "SUSPENDED"


def test_block_park_with_custom_active_report_and_honor_manual_suspend(team):
    dsn, _org, tokens = team
    with connect(dsn) as c:
        m = authenticate(c, tokens["manager"])
        TOOLS["aw_hire"][2](
            c,
            m,
            expected_revision=1,
            operation_id="r",
            employee_id="custom-reader",
            role_id="reviewer",
            supervisor_id="review-lead",
            team_id="audit",
        )
        view = presets.preview(c, m, "Minimal")
        assert "custom-reader" in view["blocked"]
        with pytest.raises(Denied, match="PRESET_CONFLICT"):
            presets.apply_preset(c, m, "Minimal", "switch", 2, view["preview_sha"])
        TOOLS["aw_suspend"][2](
            c, m, expected_revision=2, operation_id="s", employee_id="custom-reader"
        )
        TOOLS["aw_suspend"][2](c, m, expected_revision=3, operation_id="s2", employee_id="reviewer")
        switch(c, m, "Minimal", "switch2")
        # A previously manually suspended worker cannot be silently reactivated as a preset side effect.
        assert "reviewer" in presets.preview(c, m, "Standard")["blocked"]


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
        assert (
            c.execute("SELECT count(*) AS n FROM aw_outbox WHERE org_id=%s", (org,)).fetchone()["n"]
            == 1
        )


def test_trusted_roster_and_unknown_delegation_ceiling(team):
    dsn, org, tokens = team
    with connect(dsn) as c:
        m = authenticate(c, tokens["manager"])
        director = authenticate(c, tokens["volume"])
        count = c.execute(
            "SELECT count(*) AS n FROM aw_employees WHERE org_id=%s", (org,)
        ).fetchone()["n"]
        presets.configure_limits(c, org, count, 1, {"character_actor": ["actors"]})
        args = {
            "expected_revision": 1,
            "operation_id": "extra",
            "employee_id": "extra",
            "role_id": "character_actor",
            "supervisor_id": "volume",
            "team_id": "actors",
        }
        with pytest.raises(Denied, match="ROSTER_LIMIT_EXCEEDED"):
            TOOLS["aw_hire"][2](c, m, **args)
        presets.configure_limits(c, org, count + 1, 1, {"character_actor": ["actors"]})
        with pytest.raises(Denied, match="ROSTER_SCOPE_DENIED"):
            TOOLS["aw_hire"][2](c, m, **{**args, "team_id": "secrets"})
        nf = c.execute(
            "SELECT nf_instance_id FROM aw_installations WHERE installation_id=%s",
            (m.installation_id,),
        ).fetchone()["nf_instance_id"]
        c.execute("INSERT INTO aw_budget_accounts VALUES (%s,100,'USD')", (org,))
        policy = {
            "nf_instance_id": nf,
            "work_id": str(uuid4()),
            "branch_id": str(uuid4()),
            "actor_binding_ref": str(uuid4()),
            "role_id": "character_actor",
            "task_template_id": "character-action",
            "scope_ref": "scope",
            "generation": 0,
            "grant_epoch": 1,
            "max_amount": "1",
            "currency": "USD",
            "token_limit": 100,
        }
        for slot in ("a", "b"):
            allow_slot(c, org, m.installation_id, slot, "actor-1", policy)
        a = delegate(c, director, "reserve", "a", "1", 100)["operation_id"]
        c.execute("UPDATE aw_delegations SET state='RECONCILING' WHERE operation_id=%s", (a,))
        with pytest.raises(Denied, match="ACTIVE_DELEGATION_LIMIT"):
            delegate(c, director, "excess", "b", "1", 100)
        switch(c, m, "Minimal", "switch")
        assert c.execute(
            "SELECT state,status FROM aw_delegations JOIN aw_budget_holds USING(org_id,operation_id) WHERE operation_id=%s",
            (a,),
        ).fetchone() == {"state": "RECONCILING", "status": "HELD"}


@pytest.mark.parametrize(
    ("target", "slot", "change"),
    [("Standard", "reviewer", "reactivate"), ("Long-form", "memory", "add")],
)
def test_preset_reports_manually_occupied_slot_before_apply(team, target, slot, change):
    dsn, org, tokens = team
    with connect(dsn) as conn:
        manager = authenticate(conn, tokens["manager"])
        switch(conn, manager, "Minimal", "park-reviews")
        TOOLS["aw_hire"][2](
            conn, manager, expected_revision=2, operation_id="manual-staffing",
            employee_id="cover", role_id="character_actor", supervisor_id="volume",
            team_id="actors", slot_id=slot, display_name="Temporary cover",
        )
        preview = presets.preview(conn, manager, target)
        assert slot in preview[change]
        assert slot in preview["blocked"]
        with pytest.raises(Denied, match="PRESET_CONFLICT"):
            presets.apply_preset(conn, manager, target, "switch-with-cover", 3, preview["preview_sha"])
        cover = employee(conn, org, "cover")
        assert (cover["status"], cover["slot_id"], cover["employment_revision"]) == ("ACTIVE", slot, 1)
        assert presets.preview(conn, manager, target) == preview

        TOOLS["aw_suspend"][2](
            conn, manager, expected_revision=3, operation_id="end-cover", employee_id="cover",
        )
        available = presets.preview(conn, manager, target)
        assert slot not in available["blocked"]
        result = presets.apply_preset(conn, manager, target, "switch-after-cover", 4, available["preview_sha"])
        assert result["revision"] == 5
        assert employee(conn, org, slot)["status"] == "ACTIVE"
        assert employee(conn, org, "cover")["status"] == "SUSPENDED"


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
        command.upgrade(Config("alembic.ini"), "head")
        with connect(scoped) as conn:
            assert conn.execute("SELECT owner_installation_id FROM aw_organizations").fetchone()["owner_installation_id"] is None
            assert not conn.execute("SELECT org_member FROM aw_bindings").fetchone()["org_member"]
            claim_organization(conn, org, old["installation_id"])
            actor = authenticate(conn, enroll(conn, old["installation_id"], org, "actor-1", 1, 1))
            assert messaging.mailbox(conn, actor)["items"] == [{"sequence": 1, "status": "UNAVAILABLE"}]
            with pytest.raises(Denied, match="SENDER_BINDING_UNVERIFIED"):
                messaging.body_get(conn, actor, old["message_id"])
            assert conn.execute("SELECT count(*) AS n FROM aw_messages WHERE message_id=%s", (old["message_id"],)).fetchone()["n"] == 1
            assert old["member_count"] == 8
            retained = conn.execute(
                """SELECT p.employee_id, p.managed_revision, e.employment_revision
                   FROM aw_preset_members p JOIN aw_employees e USING (org_id,employee_id)
                   WHERE p.org_id=%s AND NOT p.parked ORDER BY p.employee_id""", (org,)
            ).fetchall()
            assert len(retained) == old["member_count"]
            assert all(row["managed_revision"] == row["employment_revision"] == 1 for row in retained)
    finally:
        with connect(base) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))


@pytest.mark.parametrize("parked", [False, True], ids=["new-slot", "parked-slot"])
def test_preset_expansion_observes_supervisors_current_role(team, parked):
    dsn, org, tokens = team
    with connect(dsn) as conn:
        manager = authenticate(conn, tokens["manager"])
        revision = 1
        if parked:
            switch(conn, manager, "Research-Audit", "create-stats")
            revision = switch(conn, manager, "Standard", "park-stats")["revision"]
        previous_stats = employee(conn, org, "stats")
        transfer = TOOLS["aw_transfer"][2]
        transfer(
            conn, manager, expected_revision=revision, operation_id="move-reviewer",
            employee_id="reviewer", role_id="writer", supervisor_id="volume", team_id="production",
        )
        transfer(
            conn, manager, expected_revision=revision + 1, operation_id="move-lead",
            employee_id="review-lead", role_id="maintenance", supervisor_id="manager", team_id="ops",
        )

        preview = presets.preview(conn, manager, "Research-Audit")
        assert "stats" in preview["blocked"]
        with pytest.raises(Denied, match="PRESET_CONFLICT"):
            presets.apply_preset(conn, manager, "Research-Audit", "expand", revision + 2,
                                preview["preview_sha"])
        assert employee(conn, org, "stats") == previous_stats
        assert presets.preview(conn, manager, "Research-Audit") == preview

        transfer(
            conn, manager, expected_revision=revision + 2, operation_id="restore-lead",
            employee_id="review-lead", role_id="review_lead", supervisor_id="manager", team_id="audit",
        )
        assert switch(conn, manager, "Research-Audit", "expand-with-lead")["revision"] == revision + 4
        assert employee(conn, org, "reviewer")["role_id"] == "writer"
        assert employee(conn, org, "stats")["supervisor_id"] == "review-lead"
