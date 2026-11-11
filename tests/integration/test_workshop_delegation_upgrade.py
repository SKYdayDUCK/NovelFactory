"""Upgrade the signed W2 NF database through the delegation migration."""
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text

from novel_factory.application.authorization import Principal
from novel_factory.application.delegation import install
from novel_factory.application.kernel import Kernel
from novel_factory.infrastructure.objects import ObjectStore
from novel_factory.infrastructure.recovery import database_snapshot
from tests.fixtures import historical_release
from tests.integration.test_s9_upgrade import assert_downgrade_preserves_database

release_database = historical_release.release_database

pytestmark = [pytest.mark.postgres, pytest.mark.integration]
W2 = "f2c584ec8e738e09d31b05ddd5c032bb2b8f96b1"
PREVIOUS = "0016_p55_observed_scopes"
BRIDGE = "0017_nf_workshop_delegation"


def test_released_world_survives_bridge_upgrade_and_guarded_downgrade(
    release_database, tmp_path, record_testsuite_property,
):
    db, env = release_database
    root = Path(__file__).resolve().parents[2]
    old = historical_release.run_release(root, tmp_path, W2, """
import json, os
from novel_factory.fixtures import load_fixture, point, run_command
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.objects import ObjectStore
db = Database()
try:
    k = Kernel(db, ObjectStore(os.environ["NF_OBJECT_ROOT"]))
    world = load_fixture(k)
    owner = world["principal"]
    result = run_command(k, owner, world["world_ref"], "DESTROY_ITEM",
                         {"item_id": world["ids"]["sword"], "at": point(40)})
    print(json.dumps({"world_ref": result["world_ref"],
                      "owner": {"grant_id": owner.grant_id, "principal_id": owner.principal_id}}))
finally:
    db.close()
""", env)

    def snapshot(revision):
        with db.uow(write=False) as repo:
            assert repo.conn.scalar(text("SELECT version_num FROM alembic_version")) == revision
            return database_snapshot(repo.conn)

    def migrate(action, revision):
        subprocess.run([sys.executable, "-m", "alembic", action, revision],
                       cwd=root, env=env, check=True, capture_output=True)

    before = snapshot(PREVIOUS)
    assert all(before[t]["rows"] for t in ("nf_events", "nf_tasks", "nf_outbox"))
    migrate("upgrade", BRIDGE)
    after = snapshot(BRIDGE)
    new_tables = {"nf_delegation_installations", "nf_delegation_slots", "nf_delegation_receipts"}
    assert after.keys() == before.keys() | new_tables
    assert all(after[t]["rows"] == 0 for t in new_tables)
    for table in before.keys() - {"alembic_version"}:
        assert after[table] == before[table], table

    migrate("downgrade", PREVIOUS)
    assert snapshot(PREVIOUS) == before
    migrate("upgrade", BRIDGE)
    assert snapshot(BRIDGE) == after
    k = Kernel(db, ObjectStore(tmp_path / "objects"))
    wr = old["world_ref"]
    with db.uow(write=False) as repo:
        assert k.head(repo, wr["work_id"], wr["branch_id"]) == wr
    install(k, Principal(**old["owner"]), nf_instance_id=str(uuid4()), org_id=str(uuid4()),
            work_id=wr["work_id"], branch_id=wr["branch_id"],
            expires_at=datetime.now(UTC) + timedelta(hours=1))
    assert_downgrade_preserves_database(db, env, root, BRIDGE, PREVIOUS,
                                       "Retain delegation bindings and receipts before downgrade")
    record_testsuite_property("bridge.upgrade_from_commit", W2)
    record_testsuite_property("bridge.previous_table_count", len(before))
