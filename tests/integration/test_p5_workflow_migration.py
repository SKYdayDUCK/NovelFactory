"""P5 additive upgrades and empty-extension rollback preserve pre-P5 data."""
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from novel_factory.infrastructure.recovery import database_snapshot

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


@pytest.mark.parametrize("revision", ["0004", "head"])
def test_workflow_schema_round_trip(rel_env_for_migration, revision):
    target = rel_env_for_migration
    root = Path(__file__).resolve().parents[2]
    env = {**os.environ, "NF_DATABASE_URL": target}
    def migrate(command, revision):
        subprocess.run([sys.executable, "-m", "alembic", command, revision], env=env,
                       cwd=root, capture_output=True, check=True)
    migrate("upgrade", "0003")
    engine = create_engine(target)
    try:
        with engine.begin() as conn:
            # A durable notification already delivered before P5.
            conn.execute(text("""INSERT INTO nf_outbox (outbox_id,topic,aggregate_type,
                aggregate_id,payload,dedupe_key,state) VALUES (:id,'task.changed',
                'task','legacy','{}','legacy-proof','DELIVERED')"""), {"id": uuid4()})
            original = database_snapshot(conn)
        migrate("upgrade", revision)
        with engine.connect() as conn:
            upgraded = database_snapshot(conn)
            added = set(upgraded) - set(original)
            assert {"nf_workflow_waits", "nf_workflow_edges"} <= added
            if revision == "0004":
                assert added == {"nf_workflow_waits", "nf_workflow_edges"}
            else:
                assert {"nf_supervisor_jobs", "nf_host_turns", "nf_resume_grants"} <= added
            assert all(upgraded[k] == v for k, v in original.items() if k != "alembic_version")
        migrate("downgrade", "0003")
        with engine.connect() as conn:
            assert database_snapshot(conn) == original
    finally:
        engine.dispose()


@pytest.fixture
def rel_env_for_migration(kernel_env):
    name = "nf_wf_upgrade_" + uuid4().hex[:12]
    with kernel_env["admin"].connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    target = make_url(kernel_env["url"]).set(database=name).render_as_string(hide_password=False)
    try:
        yield target
    finally:
        with kernel_env["admin"].connect() as conn:
            conn.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
