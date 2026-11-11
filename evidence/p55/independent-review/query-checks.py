from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import text

from novel_factory.domain.errors import DomainError
from .conftest import seed


def test_overlapping_readers_get_private_work_scoped_rows(query_env):
    kernel = query_env["kernel"]
    worlds = [seed(query_env) for _ in range(2)]
    barrier = Barrier(4, timeout=10)
    def reader(i):
        own = worlds[i % 2]
        expected = set(own["genesis"]["entities"])
        with kernel.db.uow(write=False) as repo:
            barrier.wait()
            for _ in range(10):
                actual = kernel.revision(repo, own["world_ref"])
                assert set(actual["snapshot"]["entities"]) == expected
                actual["snapshot"]["entities"].clear()
    with ThreadPoolExecutor(4) as executor:
        list(executor.map(reader, range(4)))
    with kernel.db.uow(write=False) as repo, pytest.raises(DomainError) as miss:
        kernel.revision(repo, {**worlds[0]["world_ref"],
                               "work_id": worlds[1]["world_ref"]["work_id"]})
    assert miss.value.code == "VIEW_DENIED"


def test_schedule_verification_still_runs_on_warm_snapshot(query_env):
    kernel = query_env["kernel"]
    wr = seed(query_env)["world_ref"]
    with kernel.db.uow(write=False) as repo:
        kernel.revision(repo, wr)
    # Fixture database owner simulates a mismatched restoration of schedule metadata.
    with kernel.db.uow() as repo:
        repo.conn.execute(text("SET LOCAL session_replication_role = replica"))
        repo.conn.execute(text("""UPDATE nf_revisions SET provenance =
            provenance || '{"simulation_base_manifest":"inconsistent"}'::jsonb
            WHERE world_revision_id=:id"""), {"id":wr["world_revision_id"]})
    with kernel.db.uow(write=False) as repo, pytest.raises(DomainError) as damage:
        kernel.revision(repo, wr)
    assert damage.value.code == "PROJECTION_CORRUPT"
