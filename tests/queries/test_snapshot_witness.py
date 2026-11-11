"""A warm read can reuse evidence of a digest without trusting mutable Python state."""
import pytest
from sqlalchemy import text

from novel_factory.application import kernel as kernel_module
from novel_factory.domain.errors import DomainError
from .conftest import seed

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def test_repeated_projection_validates_content_once_without_sharing_mutable_objects(query_env, monkeypatch):
    kernel = query_env["kernel"]
    wr = seed(query_env)["world_ref"]
    witness = []
    digest = kernel_module.fingerprint

    def capture(value):
        if isinstance(value, dict) and "entities" in value:
            witness.append(1)
        return digest(value)

    monkeypatch.setattr(kernel_module, "fingerprint", capture)
    with kernel.db.uow(write=False) as repo:
        first = kernel.revision(repo, wr)
        first["snapshot"].clear()
        next_read = kernel.revision(repo, wr)
        assert next_read["snapshot"]["entities"]
    # Seeding may already have qualified this exact projection.
    assert len(witness) <= 1


@pytest.mark.parametrize("change", ["snapshot", "digest"])
def test_repaired_or_damaged_storage_rechecks_the_live_witness(query_env, change):
    kernel = query_env["kernel"]
    wr = seed(query_env)["world_ref"]
    with kernel.db.uow(write=False) as repo:
        kernel.revision(repo, wr)

    # Test-only database owner emulates damaged or manually restored storage.
    # Production immutable triggers remain enabled outside this transaction.
    with kernel.db.uow() as repo:
        repo.conn.execute(text("SET LOCAL session_replication_role = replica"))
        column = "snapshot='{}'::jsonb" if change == "snapshot" else "state_hash=repeat('0',64)"
        repo.conn.execute(text("UPDATE nf_revisions SET " + column + " WHERE world_revision_id=:id"),
                          {"id": wr["world_revision_id"]})
    with kernel.db.uow(write=False) as repo, pytest.raises(DomainError) as failure:
        kernel.revision(repo, wr)
    assert failure.value.code == "PROJECTION_CORRUPT"
