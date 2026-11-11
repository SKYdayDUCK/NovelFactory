"""An accepted P1 world survives the additive P2 migration byte-for-byte."""
from pathlib import Path
import subprocess
import sys

import pytest
from tests.fixtures.historical_release import release_database, run_release

from novel_factory.application.kernel import Kernel
from novel_factory.application.authorization import Principal
from novel_factory.infrastructure.objects import ObjectStore
from novel_factory.infrastructure.recovery import database_snapshot


pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def create_p1_world(repo, directory, env):
    """Run the actual P1 release and return only its public IDs and original receipt."""
    p1 = "721f155c9cc912cddfdae559afe4f2516d0967f2"
    seed = """
import json
import os
from novel_factory.fixtures import load_fixture, point, run_command
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.objects import ObjectStore
db = Database()
try:
    kernel = Kernel(db, ObjectStore(os.environ['NF_OBJECT_ROOT']))
    world = load_fixture(kernel, 'white_bridge')
    wr, principal, ids = world['world_ref'], world['principal'], world['ids']
    accepted = run_command(kernel, principal, wr, 'RESOURCE_TRANSFER', {
        'from_account': ids['warehouse'], 'to_account': ids['relief'], 'resource_id': ids['grain'],
        'quantity': '12', 'unit': 'bag', 'at': point(10)})
    print(json.dumps({'principal': {'grant_id': principal.grant_id, 'principal_id': principal.principal_id},
                      'original_world_ref': wr, 'request': accepted['request'],
                      'world_ref': accepted['world_ref'], 'receipt': accepted['receipt']}))
finally:
    db.close()
"""
    return run_release(repo, directory, p1, seed, env)


def test_p1_accepted_world_is_preserved_by_p2_additive_migration(release_database, tmp_path):
    db, env = release_database
    root = Path(__file__).resolve().parents[2]
    kernel = Kernel(db, ObjectStore(tmp_path / "objects"))
    accepted = create_p1_world(root, tmp_path, env)
    principal = Principal(**accepted["principal"])
    with db.engine.connect() as conn:
        before = database_snapshot(conn)
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"],
                   cwd=root, env=env, check=True, capture_output=True)
    with db.engine.connect() as conn:
        after = database_snapshot(conn)
    for table, digest in before.items():
        if table != "alembic_version":
            assert after[table] == digest, table
    for table in ("nf_simulation_interactions", "nf_simulation_intents", "nf_simulation_outcomes"):
        assert after[table]["rows"] == 0
    assert kernel.accept(principal, accepted["request"]) == accepted["receipt"]
    with db.uow(write=False) as repo:
        assert kernel.head(repo, accepted["original_world_ref"]["work_id"]) == accepted["world_ref"]
