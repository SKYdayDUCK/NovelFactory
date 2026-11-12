"""Adversarial integrity cases for the experimental conditional projection transfer."""
from uuid import uuid4

import pytest
from sqlalchemy import MetaData, text

from experiments.db_execution.revision_witness import WitnessKernel
from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import load_fixture
from novel_factory.infrastructure.repository import KernelRepository


def test_live_witness_includes_actual_db_bytes_and_returns_private_objects(kernel_env):
    standard = kernel_env['kernel']
    world = load_fixture(standard)
    other = load_fixture(standard)
    probe = WitnessKernel(standard.db, standard.objects)
    wr = world['world_ref']

    def load(ref):
        with standard.db.uow(write=False) as r:
            return probe.revision(r, ref)

    a = load(wr)
    independently_decoded = load(wr)
    a['snapshot'].clear()
    assert independently_decoded['snapshot'] and independently_decoded == load(wr)
    assert load(other['world_ref'])['world_revision_id'] == other['world_ref']['world_revision_id']
    with pytest.raises(DomainError) as mismatch:
        load({**wr, 'work_id': str(uuid4())})
    assert mismatch.value.code == 'VIEW_DENIED'
    load(wr)
    # Inject storage damage in a connection-local copy. Keep the immutable
    # revision trigger and original row intact throughout the test.
    with standard.db.uow() as r:
        r.conn.execute(text("CREATE TEMP TABLE witness_probe ON COMMIT DROP AS "
                            "SELECT * FROM nf_revisions WHERE world_revision_id=:id"),
                       {'id': wr['world_revision_id']})
        copied = r.tables['revisions'].to_metadata(MetaData(), name='witness_probe')
        local = KernelRepository(r.conn, {**r.tables, 'revisions': copied})
        assert probe.revision(local, wr) == independently_decoded
        r.conn.execute(text("UPDATE witness_probe SET snapshot=CAST('{}' AS jsonb)"))
        with pytest.raises(DomainError) as corrupt:
            probe.revision(local, wr)
        assert corrupt.value.code == 'PROJECTION_CORRUPT'
