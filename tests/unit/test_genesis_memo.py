"""Pure genesis reuse must not share state, alter input order, or accept bad data."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from uuid import NAMESPACE_URL, uuid5

import pytest

from novel_factory.domain import world
from novel_factory.domain.errors import DomainError


def uid(name):
    return str(uuid5(NAMESPACE_URL, 'nf-genesis-memo:' + name))


def genesis():
    return {'entities': {uid(name): {'entity_id': uid(name), 'entity_kind': kind,
            'name': name, 'created_at': {'tick': 0, 'phase': 0}}
            for name, kind in [('source', 'ACCOUNT'), ('target', 'ACCOUNT'), ('grain', 'RESOURCE')]},
        'relations': [], 'resources': [{'account_id': uid('source'), 'resource_id': uid('grain'),
                                       'quantity': '100', 'unit': 'bag'}],
        'cognitions': [], 'lifecycle': {}}


def transfer_event():
    def artifact(name):
        return {'artifact_id': uid(name), 'artifact_revision_id': uid(name + ':v'),
                'sha256': 'a' * 64}
    return {'schema_version': '2.0', 'occurrence_id': uid('transfer'),
        'event_version_id': uid('transfer:v'), 'effect_group_id': uid('transfer:g'),
        'representation_id': uid('transfer:r'), 'participants': [uid('source'), uid('target')],
        'causal_parents': [], 'ruleset_ref': artifact('rules'), 'adjudication_ref': artifact('closure'),
        'engine_version': 'nf-reducer-1', 'occurrence_time': {'kind': 'EXACT', 'at': {'tick': 1, 'phase': 0}},
        'effects': [{'kind': 'RESOURCE_DELTA', 'effect_key': name, 'account_id': uid(name),
                     'resource_id': uid('grain'), 'quantity': delta, 'unit': 'bag'}
                    for name, delta in [('source', '-1'), ('target', '1')]]}


@pytest.fixture(autouse=True)
def isolated_memo(monkeypatch):
    cache = world._InitialSnapshotMemo()
    monkeypatch.setattr(world, '_initial_snapshot_memo', cache)
    return cache


def test_memo_reuses_only_initial_computation_and_keeps_complete_event_checks(monkeypatch):
    source = genesis()
    event = transfer_event()
    original = world._empty_snapshot
    validations = []
    def counted(value):
        validations.append(deepcopy(value))
        return original(value)
    monkeypatch.setattr(world, '_empty_snapshot', counted)
    first = world.replay(source, [event])
    second = world.replay(source, [event])
    assert first == second and len(validations) == 1
    assert world.resource_at(second, uid('source'), uid('grain'), 'bag')['quantity'] == '99'
    assert world.resource_at(world.replay(source, []), uid('source'), uid('grain'), 'bag')['quantity'] == '100'
    with pytest.raises(DomainError, match='Repeated selected effect identity'):
        world.replay(source, [event, event])
    invalid = deepcopy(event)
    invalid['effects'][0]['quantity'] = '-101'
    invalid['effects'][1]['quantity'] = '101'
    with pytest.raises(DomainError):
        world.replay(source, [invalid])


def test_input_output_mutation_and_dictionary_order_cannot_reuse_another_state():
    source = genesis()
    expected = world._empty_snapshot(deepcopy(source))
    first = world.replay(source, [])
    first['entities'][uid('source')]['name'] = 'caller mutation'
    first['resource_history'].clear()
    assert world.replay(source, []) == expected
    source['resources'][0]['quantity'] = '17'
    changed = world.replay(source, [])
    assert changed == world._empty_snapshot(deepcopy(source))
    assert world.resource_at(changed, uid('source'), uid('grain'), 'bag')['quantity'] == '17'
    reversed_source = deepcopy(source)
    reversed_source['entities'] = dict(reversed(list(source['entities'].items())))
    reversed_result = world.replay(reversed_source, [])
    assert reversed_result == world._empty_snapshot(reversed_source)
    assert reversed_result['lifecycle_history'] != changed['lifecycle_history']


@pytest.mark.parametrize('change', ['float', 'custom', 'missing_name', 'unknown_field', 'bad_uuid'])
def test_warm_memo_does_not_sanitize_or_accept_invalid_genesis(change, isolated_memo):
    source = genesis()
    world.replay(source, [])
    if change == 'float':
        source['entities'][uid('source')]['created_at']['tick'] = 0.0
    elif change == 'custom':
        source['entities'][uid('source')]['name'] = object()
    elif change == 'missing_name':
        source['entities'][uid('source')]['name'] = ''
    elif change == 'unknown_field':
        source['unexpected'] = True
    else:
        source['entities']['invalid'] = source['entities'].pop(uid('source'))
    before = len(isolated_memo._entries)
    with pytest.raises(DomainError):
        world.replay(source, [])
    assert len(isolated_memo._entries) == before


def test_cache_is_bounded_by_input_and_output_bytes_and_entry_count():
    cache = world._InitialSnapshotMemo(max_entries=2, max_bytes=12)
    a, b, c = [('version', bytes([i]) * 3) for i in range(3)]
    cache.put(a, b'123')
    cache.put(b, b'456')
    assert cache._bytes == 12 and cache.get(a) == b'123'
    cache.put(c, b'789')
    assert cache.get(b) is None and cache.get(a) == b'123'
    cache.put(('version', b'x' * 13), b'')
    assert cache._bytes == 12 and len(cache._entries) == 2
    cache.put(a, b'1234567')
    assert cache._bytes == 10 and cache.get(c) is None


def test_concurrent_replays_return_independent_snapshots():
    source = genesis()
    expected = world.replay(source, [])
    def caller(index):
        result = world.replay(source, [])
        assert result == expected
        result['entities'][uid('source')]['name'] = str(index)
        return result
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(caller, range(12)))
    assert [x['entities'][uid('source')]['name'] for x in results] == [str(i) for i in range(12)]
    assert world.replay(source, []) == expected


def test_interpreter_identity_is_part_of_memo_key(monkeypatch, isolated_memo):
    source = genesis()
    world.replay(source, [])
    assert len(isolated_memo._entries) == 1
    monkeypatch.setattr(world, 'INTERPRETER_VERSION', 'test-distinct-interpreter')
    world.replay(source, [])
    assert len(isolated_memo._entries) == 2
