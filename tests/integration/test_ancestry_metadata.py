"""Scoped lineage remains exact without loading whole immutable projections."""
from uuid import uuid4

import pytest
from sqlalchemy import event

from novel_factory.fixtures import load_fixture, point, run_command

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def test_ancestry_uses_scoped_parent_chain_and_never_selects_projection(kernel_env):
    kernel = kernel_env['kernel']
    world = load_fixture(kernel, 'white_bridge')
    old = world['world_ref']
    ids = world['ids']
    receipt = run_command(kernel, world['principal'], old, 'RESOURCE_TRANSFER', {
        'from_account': ids['warehouse'], 'to_account': ids['relief'],
        'resource_id': ids['grain'], 'quantity': '1', 'unit': 'bag', 'at': point(2)})
    new = receipt['world_ref']
    foreign = load_fixture(kernel, 'white_bridge')['world_ref']
    statements = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    event.listen(kernel.db.engine, 'before_cursor_execute', capture)
    try:
        with kernel.db.uow(write=False) as repo:
            assert repo.ancestor(old, new)
            assert repo.ancestor(new, new)
            assert not repo.ancestor(new, old)
            assert not repo.ancestor(old, foreign)
            assert not repo.ancestor({**old, 'world_revision_id': str(uuid4())}, new)
            forged_old = {**old, 'work_id': foreign['work_id']}
            forged_new = {**new, 'work_id': foreign['work_id']}
            assert not repo.ancestor(forged_old, forged_new)
            forged_old = {**old, 'branch_id': foreign['branch_id']}
            forged_new = {**new, 'branch_id': foreign['branch_id']}
            assert not repo.ancestor(forged_old, forged_new)
    finally:
        event.remove(kernel.db.engine, 'before_cursor_execute', capture)
    assert len(statements) == 6  # cross-world mismatch performs no SQL
    assert all('snapshot' not in statement.lower() for statement in statements)
