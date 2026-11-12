"""Upgrade a populated, signed W3 release and check its W4 relational boundaries."""

import json
import os
import subprocess
import tarfile
from uuid import uuid4

from alembic.config import Config
from alembic.script import ScriptDirectory
import psycopg
import pytest

from test_w1_historical_upgrade import WORKSHOP, run
from test_w2_historical_upgrades import columns, snapshot


RELEASED_W3 = "e6763d844f023d7c6a13060dbaebca0416fe3099"


def test_signed_w3_ledgers_and_review_constraints(tmp_path, record_property):
    dsn = os.environ["AW_W4_FROM_W3_DATABASE_URL"]
    with psycopg.connect(dsn) as conn:
        assert conn.info.server_version // 10000 == 17
        assert conn.execute("SELECT to_regclass('aw_alembic_version')").fetchone() == (None,)

    archive = tmp_path / "w3.tar"
    subprocess.run(["git", "archive", "--format=tar", "--output", archive, RELEASED_W3, "workshop"],
                   cwd=WORKSHOP.parent, check=True)
    with tarfile.open(archive) as files:
        files.extractall(tmp_path, filter="data")
    old = tmp_path / "workshop"
    run(old, dsn, "alembic", "upgrade", "head")
    # The signed service creates both organizations, its original budget reservations
    # and observations. The peer API in these two archived tests is deterministic.
    run(old, dsn, "pytest", "-q",
        "tests/test_delegation.py::test_lost_reply_unknown_then_final_once",
        "tests/test_delegation.py::test_crash_before_first_send_and_frozen_unsent_intent")
    with psycopg.connect(dsn) as conn:
        assert conn.execute("SELECT version_num FROM aw_alembic_version").fetchall() == [("w003b",)]
        old_columns = columns(conn)
        before = snapshot(conn, old_columns)
        assert before["aw_delegations"][0] == before["aw_budget_holds"][0] == 2
        assert before["aw_remote_receipts"][0] == 1
        assert before["aw_cost_observations"][0] >= 2
        assert conn.execute("SELECT status FROM aw_budget_holds ORDER BY status").fetchall() == [
            ("HELD",), ("SETTLED",)]
        assert conn.execute("SELECT to_regclass('aw_reviews'),to_regclass('aw_skill_bindings')").fetchone() == (None, None)

    scripts = ScriptDirectory.from_config(Config(str(WORKSHOP / "alembic.ini")))
    head = scripts.get_current_head()
    assert head and "w004b" in {revision.revision for revision in scripts.walk_revisions(head=head)}
    assert set(scripts.get_revision("w004b").down_revision) == {"w003b", "w004a"}
    run(WORKSHOP, dsn, "alembic", "upgrade", "head")
    run(WORKSHOP, dsn, "alembic", "upgrade", "head")
    with psycopg.connect(dsn) as conn:
        assert conn.execute("SELECT version_num FROM aw_alembic_version").fetchall() == [(head,)]
        assert snapshot(conn, old_columns) == before
        assert conn.execute("SELECT bool_and(NOT review_required) FROM aw_delegation_slots").fetchone() == (True,)
        assert conn.execute("SELECT count(*) FROM aw_reviews").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM aw_skill_bindings").fetchone() == (0,)

        # Populate one real binding in an existing organization, then require both
        # cross-organization and dangling-key writes to fail in PostgreSQL itself.
        left, right = [row[0] for row in conn.execute("SELECT org_id FROM aw_organizations ORDER BY org_id")]
        binding = uuid4()
        conn.execute("INSERT INTO aw_skill_sources VALUES (%s,'voice','v1',%s,'creative','PROSE')", (left, "1" * 64))
        conn.execute("""INSERT INTO aw_skill_bindings
            (org_id,binding_id,skill_id,source_revision,content_sha,scope_ref,policy,owner,owner_revision,mode,expires_at)
            VALUES (%s,%s,'voice','v1',%s,'organization','PUBLIC_SHARED','manager',1,'PROSE',now()+interval '1 hour')
            """, (left, binding, "1" * 64))
        with pytest.raises(psycopg.errors.ForeignKeyViolation) as wrong_org, conn.transaction():
            conn.execute("""INSERT INTO aw_skill_snapshots
                (org_id,employee_id,employment_revision,task_ref,skill_id,binding_id,epoch,content_sha)
                VALUES (%s,'volume',1,%s,'voice',%s,1,%s)""", (right, uuid4(), binding, "1" * 64))
        with pytest.raises(psycopg.errors.ForeignKeyViolation) as missing_review, conn.transaction():
            conn.execute("""INSERT INTO aw_review_evidence(org_id,review_id,seq,authority,reason,evidence_ref)
                VALUES (%s,%s,1,'reviewer','VOICE','orphan')""", (left, uuid4()))
        record_property("postgres_version", conn.info.server_version)
        record_property("rejected_foreign_keys", json.dumps([
            wrong_org.value.diag.constraint_name, missing_review.value.diag.constraint_name]))
    record_property("released_w3_sha", RELEASED_W3)
    record_property("starting_revision", "w003b")
    record_property("ending_revision", head)
    record_property("preserved_table_counts_and_sha256", json.dumps(before, sort_keys=True))
