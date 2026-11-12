"""Upgrade four deployed W4/W5 histories with real receipts, frozen Skills and durable wakeups."""
import json
import os
from pathlib import Path
import subprocess
import sys

import psycopg
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from test_w1_historical_upgrade import WORKSHOP, run
from test_w2_historical_upgrades import columns, snapshot
from test_w4_historical_upgrades import release


@pytest.mark.parametrize(("revision", "commit", "variable"), [
    ("w004c", "6c711adf794842bdf4469ab51e89093d37761bcc", "AW_W5_FROM_W4C_DATABASE_URL"),
    ("w005", "609e92278476c60a893658ef53b6ff15f659b153", "AW_W5_FROM_W5_DATABASE_URL"),
    ("w005a", "d308e1ce1986a33e4781be97fba8a862b89dd76f", "AW_W5_FROM_W5A_DATABASE_URL"),
    ("w005b", "bfb74f681435b12a385eb54e596c658e64629276", "AW_W5_FROM_W5B_DATABASE_URL"),
], ids=["signed-w4", "original-w5", "w5a", "w5b"])
def test_historical_ledger_and_wake_authority(revision, commit, variable, tmp_path, record_property):
    dsn = os.environ[variable]
    with psycopg.connect(dsn) as conn:
        assert conn.info.server_version // 10000 == 17
        assert conn.execute("SELECT to_regclass('aw_alembic_version')").fetchone() == (None,)
    old = release(tmp_path / "released", commit)
    binding = tmp_path / "private-manager-binding"

    def probe(package, action):
        env = {**os.environ, "AW_DATABASE_URL": dsn, "AW_FRESH_DATABASE_URL": os.environ["AW_DATABASE_URL"],
               "PYTHONPATH": os.pathsep.join(str(package / p) for p in ("src", "tests"))}
        result = subprocess.check_output([sys.executable, WORKSHOP / "acceptance/w5_upgrade_probe.py",
                                          action, revision, binding], cwd=package, env=env, text=True)
        return json.loads(result) if result.strip() else None

    run(old, dsn, "alembic", "upgrade", "head")
    run(old, dsn, "pytest", "-q", "tests/test_reviews_skills.py::test_skill_old_hash_remains_history_after_revocation",
        "tests/test_reviews_skills.py::test_paid_review_cannot_spend_without_hold_or_ignore_unknown")
    probe(old, "seed")
    acl = """SELECT oid,relacl::text,has_table_privilege(oid,'DELETE') FROM pg_class
             WHERE oid='aw_reviews'::regclass"""
    modes = """SELECT c.relname,t.tgname,t.tgenabled FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
               WHERE c.relnamespace=current_schema()::regnamespace AND NOT t.tgisinternal ORDER BY 1,2"""
    with psycopg.connect(dsn) as conn:
        assert conn.execute("SELECT version_num FROM aw_alembic_version").fetchall() == [(revision,)]
        if revision == "w005a":
            conn.execute("ALTER TABLE aw_employment_versions ENABLE ALWAYS TRIGGER aw_history")
        old_modes = conn.execute(modes).fetchall()
        conn.execute("REVOKE DELETE ON aw_reviews FROM CURRENT_USER")
        grant = conn.execute(acl).fetchone()
        assert grant[1] and not grant[2]
        fields = columns(conn)
        original = snapshot(conn, fields)
        for table in ("aw_reviews", "aw_review_evidence", "aw_skill_snapshots", "aw_remote_receipts", "aw_budget_holds"):
            assert original[table][0] > 0
        if revision != "w004c":
            assert original["aw_resume_requests"][0] == 1 and original["aw_change_log"][0] > 0

    scripts = ScriptDirectory.from_config(Config(str(WORKSHOP / "alembic.ini")))
    head = scripts.get_current_head()
    assert set(scripts.get_revision("w005c").down_revision) == {"w004c", "w005b"}
    run(WORKSHOP, dsn, "alembic", "upgrade", "head")
    run(WORKSHOP, dsn, "alembic", "upgrade", "head")
    with psycopg.connect(dsn) as conn:
        assert conn.execute("SELECT version_num FROM aw_alembic_version").fetchall() == [(head,)]
        assert conn.execute(acl).fetchone() == grant
        assert set(old_modes).issubset(conn.execute(modes).fetchall())
        assert snapshot(conn, fields) == original
    catalog = probe(WORKSHOP, "schema")
    restarted = [probe(WORKSHOP, "consume")]
    expected = "DONE" if revision == "w005b" else "IDLE"
    assert restarted[0] == {"state": expected, "peer_calls": ["nf_resume_project"] if expected == "DONE" else []}
    with psycopg.connect(dsn) as conn:
        prior = conn.execute("""SELECT state,attempts FROM aw_resume_requests
                                WHERE operation_id='queued-before-maintenance'""").fetchall()
        assert prior == {"w004c": [], "w005": [("REVOKED", 0)], "w005a": [("REVOKED", 0)],
                         "w005b": [("DONE", 1)]}[revision]
    assert probe(WORKSHOP, "queue")["state"] == "PENDING"
    restarted.extend([probe(WORKSHOP, "consume"), probe(WORKSHOP, "consume")])
    assert restarted[1:] == [{"state": "DONE", "peer_calls": ["nf_resume_project"]}, {"state": "IDLE", "peer_calls": []}]

    receipt = {"source_sha": commit, "old_revision": revision, "new_revision": head, "preserved_tables": original,
               "same_review_oid_and_acl": True, "preserved_trigger_modes": old_modes, "catalog": catalog, "old_wakes": prior, "restarted_workers": restarted,
               "peer": "deterministic local fixture", "external_provider_calls": 0}
    record_property("w5_upgrade", json.dumps(receipt, sort_keys=True))
    if output := os.getenv("AW_W5_UPGRADE_EVIDENCE_DIR"):
        directory = Path(output)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / (revision + ".json")).write_text(json.dumps(receipt, indent=2) + "\n")
