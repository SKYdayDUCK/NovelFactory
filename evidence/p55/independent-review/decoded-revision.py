import pytest
from novel_factory.domain.errors import DomainError
from test_audit_artifact_security import scenario


def test_legacy_decoded_storage_detects_later_damage():
    kernel, _, _, _, world, rows, *_ = scenario()
    with kernel.db.uow(write=False) as repo:
        first = kernel.revision(repo, world)
        first["snapshot"]["local-only"] = True
        assert kernel.revision(repo, world)["snapshot"] == {}
    rows["revisions"][0]["snapshot"] = {"changed": True}
    with kernel.db.uow(write=False) as repo, pytest.raises(DomainError) as failure:
        kernel.revision(repo, world)
    assert failure.value.code == "PROJECTION_CORRUPT"
