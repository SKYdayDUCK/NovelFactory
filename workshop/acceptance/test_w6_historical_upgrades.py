"""Migrate original W6 releases and rehearsals without losing reservations or preset provenance."""

import json
import os
from pathlib import Path
import subprocess
import sys

import psycopg
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.engine import make_url

from test_w1_historical_upgrade import WORKSHOP, run
from test_w2_historical_upgrades import columns, snapshot
from test_w4_historical_upgrades import release


HISTORIES = [
    ("w5c", "w005c", "b30e76978830bec4c596c04194d749fd01f67402"),
    ("w006", "w006", "b05ea32aa898d48536467cd2dc82c5ae0f443a32"),
    ("w007", "w007", "657a4e1e2460b55757c99a4b21c5ce55ba0b1419"),
    ("w008", "w008", "ac579e0b89b55e613d32750ce1c07210e0b07022"),
    ("w008a", "w008", "41adb61a4d80a21b59ff66e3bf1fd96c8933c538"),
    ("rehearsal_early", "w009_rehearsal", "d3322de37f8975481b0f200ba97e27d545b85c59"),
    ("rehearsal_late", "w009_rehearsal", "538a6750f8e17df8e4b408ce6589d741dfe671d4"),
    ("w009", "w009", "34e4e7658b8a6dba7055a6cab1c1a55971176bba"),
]


@pytest.mark.parametrize(("case", "revision", "sha"), HISTORIES, ids=[h[0] for h in HISTORIES])
def test_released_presets_and_inflight_receipts(case, revision, sha, tmp_path, record_property):
    fresh = os.environ["AW_DATABASE_URL"]
    dsn = (
        make_url(fresh)
        .set(database=os.environ["AW_W6_UPGRADE_DATABASE_PREFIX"] + case)
        .render_as_string(hide_password=False)
    )
    with psycopg.connect(dsn) as conn:
        assert conn.info.server_version // 10000 == 17
        assert conn.execute("SELECT to_regclass('aw_alembic_version')").fetchone() == (None,)

    def exercise(package, action):
        env = {
            **os.environ,
            "AW_DATABASE_URL": dsn,
            "AW_FRESH_DATABASE_URL": fresh,
            "PYTHONPATH": os.pathsep.join(str(package / p) for p in ("src", "tests")),
        }
        answer = subprocess.check_output(
            [
                sys.executable,
                WORKSHOP / "acceptance/w6_upgrade_probe.py",
                action,
                tmp_path / "operation.json",
            ],
            env=env,
            cwd=package,
            text=True,
        )
        return json.loads(answer) if answer.strip() else None

    old = release(tmp_path / "installed", sha)
    run(old, dsn, "alembic", "upgrade", revision if case == "w007" else "head")
    exercise(old, "seed")
    with psycopg.connect(dsn) as conn:
        assert conn.execute("SELECT version_num FROM aw_alembic_version").fetchall() == [
            (revision,)
        ]
        fields = columns(conn)
        before = snapshot(conn, fields)
        assert (
            before["aw_remote_receipts"][0]
            == before["aw_cost_observations"][0]
            == before["aw_budget_holds"][0]
            == 1
        )
        if case != "w5c":
            assert before["aw_preset_members"][0] > 0
        conn.execute("REVOKE DELETE ON aw_remote_receipts FROM CURRENT_USER")
        acl_query = "SELECT oid,relacl::text,has_table_privilege(oid,'DELETE') FROM pg_class WHERE oid='aw_remote_receipts'::regclass"
        acl = conn.execute(acl_query).fetchone()
        assert acl[1] and not acl[2]

    head = ScriptDirectory.from_config(Config(str(WORKSHOP / "alembic.ini"))).get_current_head()
    run(WORKSHOP, dsn, "alembic", "upgrade", "head")
    run(WORKSHOP, dsn, "alembic", "upgrade", "head")
    with psycopg.connect(dsn) as conn:
        assert conn.execute("SELECT version_num FROM aw_alembic_version").fetchall() == [(head,)]
        assert conn.execute(acl_query).fetchone() == acl
        assert before == snapshot(conn, fields)
        if "display_name" not in fields["aw_employees"]:
            for table in ("aw_employees", "aw_employment_versions"):
                assert conn.execute(
                    f"SELECT count(*) FROM {table} WHERE display_name<>employee_id OR slot_id<>employee_id"
                ).fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM aw_preset_members").fetchone()[0] > 0
    metadata = exercise(WORKSHOP, "schema")
    resumed = exercise(WORKSHOP, "restart")
    result = {
        "original_sha": sha,
        "starting_revision": revision,
        "target": head,
        "preserved_old_tables": before,
        "same_receipt_oid_and_acl": True,
        "catalog": metadata,
        "resumed": resumed,
        "peer": "controlled deterministic NF receipt fixture",
        "external_provider_calls": 0,
    }
    record_property("w6_upgrade", json.dumps(result, sort_keys=True))
    if output := os.environ.get("AW_W6_UPGRADE_EVIDENCE_DIR"):
        folder = Path(output)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / (case + ".json")).write_text(json.dumps(result, indent=2) + "\n")
