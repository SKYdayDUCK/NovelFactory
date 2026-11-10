"""Selected-history batching keeps full replay, original order and atomic rollback."""
from copy import deepcopy

import pytest
from sqlalchemy import event

from novel_factory.fixtures import load_fixture, point, prepare_command, run_command
from novel_factory.domain.world import resource_at

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def transfer(ids, tick):
    return {'from_account': ids['warehouse'], 'to_account': ids['relief'],
            'resource_id': ids['grain'], 'quantity': '1', 'unit': 'bag', 'at': point(tick)}


def test_batched_history_and_mapping_roll_back_and_retry_as_one_effect(kernel_env):
    kernel = kernel_env['kernel']
    fixture = load_fixture(kernel, 'white_bridge')
    principal, wr, ids = fixture['principal'], fixture['world_ref'], fixture['ids']
    for tick in (2, 3, 4):
        wr = run_command(kernel, principal, wr, 'RESOURCE_TRANSFER', transfer(ids, tick))['world_ref']
    prepared = prepare_command(kernel, principal, wr, 'RESOURCE_TRANSFER', transfer(ids, 5))
    with kernel.db.uow(write=False) as repo:
        before = kernel.revision(repo, wr)
        mappings = repo.many('revision_events', {'work_id': wr['work_id']})
        event_rows = repo.many('events', {'work_id': wr['work_id']})
    def fail(stage):
        if stage == 'before_commit':
            raise RuntimeError('injected rollback after history mapping')
    with pytest.raises(RuntimeError, match='injected rollback'):
        kernel.accept(principal, prepared['request'], fault=fail)
    with kernel.db.uow(write=False) as repo:
        assert kernel.head(repo, wr['work_id']) == wr
        assert kernel.revision(repo, wr) == before
        assert repo.many('revision_events', {'work_id': wr['work_id']}) == mappings
        assert repo.many('events', {'work_id': wr['work_id']}) == event_rows
    statements = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.lower())
    event.listen(kernel.db.engine, 'before_cursor_execute', capture)
    try:
        receipt = kernel.accept(principal, prepared['request'])
    finally:
        event.remove(kernel.db.engine, 'before_cursor_execute', capture)
    assert sum('from nf_events' in sql and ' in (' in sql for sql in statements) == 1
    assert sum('insert into nf_revision_events' in sql for sql in statements) == 1
    assert kernel.accept(principal, prepared['request']) == receipt
    with kernel.db.uow(write=False) as repo:
        current = kernel.revision(repo, receipt['world_ref'])
        selected_rows = repo.many('revision_events', {'world_revision_id': current['world_revision_id']})
        assert len(selected_rows) == len(current['selection']) == 4
        assert len({row['event_version_id'] for row in selected_rows}) == 4
        assert kernel.revision(repo, wr)['snapshot'] == before['snapshot']
    assert resource_at(current['snapshot'], ids['warehouse'], ids['grain'], 'bag')['quantity'] == '96'
    assert resource_at(current['snapshot'], ids['relief'], ids['grain'], 'bag')['quantity'] == '4'


def test_prepare_reuses_equal_base_only_within_the_same_sampling_transaction(kernel_env):
    kernel = kernel_env['kernel']
    fixture = load_fixture(kernel, 'white_bridge')
    wr, principal, ids = fixture['world_ref'], fixture['principal'], fixture['ids']
    prepared = prepare_command(kernel, principal, wr, 'RESOURCE_TRANSFER', transfer(ids, 2))
    request = deepcopy(prepared['prepare_request'])
    request['idempotency_key'] += ':same-base'
    statements = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.lower())
    event.listen(kernel.db.engine, 'before_cursor_execute', capture)
    try:
        certificate = kernel.prepare(principal, request)
    finally:
        event.remove(kernel.db.engine, 'before_cursor_execute', capture)
    assert sum('from nf_revisions' in sql and 'nf_revisions.snapshot' in sql for sql in statements) == 1
    acceptance = deepcopy(prepared['request'])
    acceptance['acceptance_certificate_ref'] = certificate
    receipt = kernel.accept(principal, acceptance)
    assert receipt['original_base_world_ref'] == receipt['validation_world_ref'] == wr
