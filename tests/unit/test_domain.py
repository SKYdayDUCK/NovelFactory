from copy import deepcopy
import importlib
import json
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest

from novel_factory.domain.canonical import canonical_bytes, fingerprint, strict_json_loads
from novel_factory.domain.errors import DomainError
from novel_factory.domain.propositions import encode_proposition, decode_proposition
from novel_factory.domain.temporal import point_key, validate_interval, validate_temporal_constraints
from novel_factory.domain.types import StoryPoint, WorldRef
from novel_factory.domain.world import replay, state_at, resource_at, lifecycle_at


def uid(name):
    return str(uuid5(NAMESPACE_URL, 'nf-unit:' + name))


def point(day, phase=0):
    return {'tick': day, 'phase': phase}


def artifact(name):
    return {'artifact_id': uid(name), 'artifact_revision_id': uid(name + ':version'), 'sha256': 'a' * 64}


def world_ref():
    return {'work_id': uid('work'), 'branch_id': uid('branch'), 'world_revision_id': uid('revision')}


def genesis():
    kinds = {'sword': 'ITEM', 'smith': 'CHARACTER', 'P': 'CHARACTER', 'F': 'CHARACTER',
             'V': 'CHARACTER', 'caravan': 'ACCOUNT', 'store': 'ACCOUNT', 'grain': 'RESOURCE',
             'north': 'REGION', 'south': 'REGION'}
    return {'entities': {uid(k): {'entity_id': uid(k), 'entity_kind': v, 'name': k,
                                  'created_at': point(0)} for k, v in kinds.items()},
            'relations': [], 'resources': [{'account_id': uid('caravan'), 'resource_id': uid('grain'),
                                            'quantity': '100', 'unit': 'bag'}], 'cognitions': [], 'lifecycle': {}}


def event(name, day, effects, version=None, phase=0):
    return {'schema_version': '2.1', 'occurrence_id': uid(name),
            'event_version_id': uid(version or name + ':v1'), 'effect_group_id': uid(name + ':group'),
            'representation_id': uid(name + ':representation'),
            'participants': [uid('sword'), uid('P'), uid('F'), uid('V'), uid('smith')],
            'causal_parents': [], 'ruleset_ref': artifact('rules'), 'adjudication_ref': artifact('ruling'),
            'effects': effects, 'engine_version': 'nf-kernel-1', 'occurrence_time': {'kind': 'EXACT', 'at': point(day, phase)}}


def relation(key, predicate, target, subject='sword'):
    return {'kind': 'RELATION_SET', 'effect_key': key, 'subject_id': uid(subject),
            'predicate': predicate, 'object_id': uid(target) if target else None,
            'value_status': 'VALUE' if target else 'NONE'}


def cognition(key, target, source, record_kind='BELIEF'):
    return {'kind': 'COGNITIVE_RECORD_ADD', 'effect_key': key, 'actor_id': uid('P'),
            'statement': encode_proposition(uid('sword'), 'possessed_by', {'kind': 'ENTITY', 'entity_id': uid(target)}),
            'epistemic_mode': 'OBSERVED' if record_kind == 'OBSERVATION' else 'INFERRED',
            'evidence_refs': [{'kind': 'EVENT', 'world_ref': world_ref(),
                               'event_ref': {'occurrence_id': uid(source), 'event_version_id': uid(source + ':v1')}}],
            'record_kind': record_kind}


def sword_history():
    return [
        event('manufacture', 1, [relation('owner', 'legal_owner', 'smith'), relation('holder', 'possessed_by', 'smith')]),
        event('gift', 10, [relation('owner', 'legal_owner', 'P'), relation('holder', 'possessed_by', 'P')]),
        event('loan', 20, [relation('holder', 'possessed_by', 'F'), cognition('saw', 'F', 'loan', 'OBSERVATION'), cognition('believes', 'F', 'loan')]),
        event('theft', 30, [relation('holder', 'possessed_by', 'V'), relation('where', 'located_in', 'north')]),
        event('destroy', 40, [{'kind': 'LIFECYCLE_SET', 'effect_key': 'destroy', 'entity_id': uid('sword'), 'lifecycle': 'DESTROYED'}]),
    ]


@pytest.mark.parametrize('day,owner,holder', [(5, 'smith', 'smith'), (15, 'P', 'P'), (25, 'P', 'F'), (35, 'P', 'V')])
def test_r1_a_history_owner_and_holder(day, owner, holder):
    snapshot = replay(genesis(), sword_history())
    assert state_at(snapshot, uid('sword'), 'owner', point(day))['data']['entity_id'] == uid(owner)
    assert state_at(snapshot, uid('sword'), 'active_holder', point(day))['data']['entity_id'] == uid(holder)
    assert state_at(snapshot, uid('sword'), 'lifecycle', point(day))['data']['value'] == 'ACTIVE'


def test_r1_a_destruction_retains_identity_and_last_owner():
    snapshot = replay(genesis(), sword_history())
    assert uid('sword') in snapshot['entities']
    assert state_at(snapshot, uid('sword'), 'active_holder', point(41))['value_status'] == 'NONE'
    assert state_at(snapshot, uid('sword'), 'active_use', point(41))['value_status'] == 'NOT_APPLICABLE'
    assert state_at(snapshot, uid('sword'), 'last_confirmed_owner', point(41))['data']['entity_id'] == uid('P')
    assert state_at(snapshot, uid('sword'), 'active_use', point(25))['data']['value'] is True


def test_r1_b_replace_loan_rebuilds_without_reapplying():
    original = sword_history()
    modified = deepcopy(original)
    modified[2]['event_version_id'] = uid('loan:v2')
    modified[2]['effects'].append(relation('owner', 'legal_owner', 'F'))
    old = replay(genesis(), original)
    new = replay(genesis(), modified)
    assert state_at(old, uid('sword'), 'owner', point(25))['data']['entity_id'] == uid('P')
    assert state_at(new, uid('sword'), 'owner', point(25))['data']['entity_id'] == uid('F')
    assert state_at(new, uid('sword'), 'last_confirmed_owner', point(41))['data']['entity_id'] == uid('F')
    assert len(old['events']) == len(new['events']) == 5
    assert original[2]['event_version_id'] == uid('loan:v1')


def test_r1_e_belief_does_not_read_world_or_upgrade_observation():
    selected = sword_history()[:4]
    selected.append(event('report', 36, [cognition('heard', 'V', 'report', 'OBSERVATION')]))
    snapshot = replay(genesis(), selected)
    assert state_at(snapshot, uid('sword'), 'holder', point(35), uid('P'), point(35))['data']['entity_id'] == uid('F')
    assert state_at(snapshot, uid('sword'), 'holder', point(37), uid('P'), point(37))['data']['entity_id'] == uid('F')
    assert state_at(snapshot, uid('sword'), 'holder', point(19), uid('P'), point(19))['value_status'] == 'UNRESOLVED'
    selected.append(event('believe_report', 37, [cognition('believed', 'V', 'report')]))
    updated = replay(genesis(), selected)
    assert state_at(updated, uid('sword'), 'holder', point(37), uid('P'), point(37))['data']['entity_id'] == uid('V')
    assert state_at(updated, uid('sword'), 'holder', point(35), uid('P'), point(35))['data']['entity_id'] == uid('F')


def test_missing_and_unobserved_are_equally_unresolved_for_actor():
    snapshot = replay(genesis(), sword_history())
    assert state_at(snapshot, uid('absent'), 'holder', point(19), uid('P'), point(19)) == state_at(snapshot, uid('sword'), 'holder', point(19), uid('P'), point(19))


@pytest.mark.parametrize('mutation', ['same_occurrence', 'same_effect_key', 'same_effect_group'])
def test_duplicate_world_effects_rejected(mutation):
    events = sword_history()
    if mutation == 'same_occurrence':
        events.append(deepcopy(events[1]))
    elif mutation == 'same_effect_key':
        events[1]['effects'].append(deepcopy(events[1]['effects'][0]))
    else:
        events[2]['effect_group_id'] = events[1]['effect_group_id']
    with pytest.raises(DomainError) as raised:
        replay(genesis(), events)
    assert raised.value.code == 'DUPLICATE_EFFECT'


def test_destroyed_item_cannot_be_transferred_or_resurrected():
    events = sword_history()
    events.append(event('late_transfer', 41, [relation('holder', 'possessed_by', 'F')]))
    with pytest.raises(DomainError, match='Inactive'):
        replay(genesis(), events)
    events[-1] = event('resurrect', 41, [{'kind': 'LIFECYCLE_SET', 'effect_key': 'active', 'entity_id': uid('sword'), 'lifecycle': 'ACTIVE'}])
    with pytest.raises(DomainError, match='resurrected'):
        replay(genesis(), events)


def transfer(quantity='30'):
    return event('transport', 30, [
        {'kind': 'RESOURCE_DELTA', 'effect_key': 'debit', 'resource_id': uid('grain'), 'quantity': '-' + quantity, 'unit': 'bag', 'account_id': uid('caravan')},
        {'kind': 'RESOURCE_DELTA', 'effect_key': 'credit', 'resource_id': uid('grain'), 'quantity': quantity, 'unit': 'bag', 'account_id': uid('store')},
    ])


def test_resource_transfer_is_atomic_conserved_and_historical():
    snapshot = replay(genesis(), [transfer()])
    assert resource_at(snapshot, uid('caravan'), uid('grain'), 'bag', point(29))['quantity'] == '100'
    assert resource_at(snapshot, uid('caravan'), uid('grain'), 'bag', point(31))['quantity'] == '70'
    assert state_at(snapshot, uid('store'), f'resource:{uid("grain")}:bag', point(31))['data']['quantity'] == '30'


@pytest.mark.parametrize('problem', ['negative', 'unbalanced', 'float', 'wrong_unit'])
def test_resource_invariants(problem):
    item = transfer('101' if problem == 'negative' else '30')
    if problem == 'unbalanced':
        item['effects'][1]['quantity'] = '29'
    if problem == 'float':
        item['effects'][1]['quantity'] = 30.0
    if problem == 'wrong_unit':
        item['effects'][1]['unit'] = 'tonne'
    with pytest.raises(DomainError):
        replay(genesis(), [item])


def transition(key, relation_id, operation, value, day, expected=None, subject='P', predicate='on_duty'):
    return {'kind': 'RELATION_TRANSITION', 'effect_key': key, 'relation_id': relation_id,
            'operation': operation, 'subject_id': uid(subject), 'predicate': predicate,
            'object_value': value, 'at': point(day), 'expected_relation_version_id': expected}


def test_r1_close_open_checked_against_selected_version():
    rid = uid('duty-1')
    opened = event('duty', 5, [transition('open', rid, 'OPEN', {'kind': 'BOOLEAN', 'value': True}, 5)])
    base = replay(genesis(), [opened])
    version = base['relations'][0]['relation_version_id']
    changed = event('off_duty', 10, [transition('close', rid, 'CLOSE', {'kind': 'BOOLEAN', 'value': True}, 10, version),
                                     transition('open', uid('duty-2'), 'OPEN', {'kind': 'BOOLEAN', 'value': False}, 10)])
    snapshot = replay(genesis(), [opened, changed])
    assert state_at(snapshot, uid('P'), 'on_duty', point(9))['data']['value'] is True
    assert state_at(snapshot, uid('P'), 'on_duty', point(10))['data']['value'] is False
    changed['effects'][0]['expected_relation_version_id'] = uid('stale')
    with pytest.raises(DomainError) as raised:
        replay(genesis(), [opened, changed])
    assert raised.value.code == 'DEPENDENCY_CONFLICT'


def test_relation_identity_cannot_be_reopened():
    rid = uid('duty-1')
    event1 = event('duty', 5, [transition('open', rid, 'OPEN', {'kind': 'BOOLEAN', 'value': True}, 5)])
    event2 = event('again', 10, [transition('open', rid, 'OPEN', {'kind': 'BOOLEAN', 'value': True}, 10)])
    with pytest.raises(DomainError) as raised:
        replay(genesis(), [event1, event2])
    assert raised.value.code == 'DUPLICATE_EFFECT'


def test_exclusive_relation_and_physical_cycle_rejected():
    a = event('duty_a', 5, [transition('a', uid('a'), 'OPEN', {'kind': 'BOOLEAN', 'value': True}, 5)])
    b = event('duty_b', 6, [transition('b', uid('b'), 'OPEN', {'kind': 'BOOLEAN', 'value': False}, 6)])
    with pytest.raises(DomainError, match='multiple'):
        replay(genesis(), [a, b])
    cyclic = event('cycle', 7, [relation('x', 'located_in', 'south', 'north'), relation('y', 'located_in', 'north', 'south')])
    with pytest.raises(DomainError, match='cycle'):
        replay(genesis(), [cyclic])


def test_same_tick_different_phases_preserve_boundary():
    item = sword_history()[0]
    item['occurrence_time']['at'] = point(1, 1)
    newer = event('later', 1, [relation('holder', 'possessed_by', 'P')], phase=2)
    snapshot = replay(genesis(), [item, newer])
    assert state_at(snapshot, uid('sword'), 'holder', point(1, 1))['data']['entity_id'] == uid('smith')
    assert state_at(snapshot, uid('sword'), 'holder', point(1, 2))['data']['entity_id'] == uid('P')


def test_simultaneous_conflicting_events_need_one_joint_result():
    first = sword_history()[0]
    second = event('race', 1, [relation('holder', 'possessed_by', 'P')])
    with pytest.raises(DomainError, match='simultaneous'):
        replay(genesis(), [first, second])


def test_t12_acyclic_but_impossible_bounded_time_rejected():
    a = event('A', 22, [])
    a['occurrence_time'] = {'kind': 'BOUNDED', 'earliest': point(22), 'latest': point(25), 'constraint_refs': []}
    b = event('B', 20, [])
    b['causal_parents'] = [{'occurrence_id': a['occurrence_id'], 'event_version_id': a['event_version_id']}]
    with pytest.raises(DomainError, match='bounds'):
        validate_temporal_constraints([a, b])
    b['occurrence_time']['at'] = point(26)
    assert validate_temporal_constraints([a, b]) == [a['occurrence_id'], b['occurrence_id']]


def test_ambiguous_effect_never_guesses_an_occurrence_endpoint():
    item = sword_history()[0]
    item['occurrence_time'] = {'kind': 'BOUNDED', 'earliest': point(1), 'latest': point(3), 'constraint_refs': []}
    with pytest.raises(DomainError) as raised:
        replay(genesis(), [item], point(2))
    assert raised.value.code == 'TIME_AMBIGUOUS'


def test_bounded_occurrence_allows_explicit_resolved_effect_boundary():
    item = event('bounded-duty', 5, [transition('open', uid('duty'), 'OPEN', {'kind': 'BOOLEAN', 'value': True}, 8)])
    item['occurrence_time'] = {'kind': 'BOUNDED', 'earliest': point(4), 'latest': point(6), 'constraint_refs': []}
    snapshot = replay(genesis(), [item])
    assert state_at(snapshot, uid('P'), 'on_duty', point(7))['value_status'] == 'UNRESOLVED'
    assert state_at(snapshot, uid('P'), 'on_duty', point(8))['data']['value'] is True


def test_time_value_objects_reject_bool_float_extra_and_outside_range():
    assert StoryPoint(tick=1, phase=2).model_dump() == point(1, 2)
    assert WorldRef.model_validate(world_ref()).model_dump() == world_ref()
    for point_bad in [{'tick': True, 'phase': 0}, {'tick': 1.5, 'phase': 0}, {'tick': 2**53, 'phase': 0}, {'tick': 0, 'phase': -1}, {'tick': 1, 'phase': 0, 'day': 1}]:
        with pytest.raises(ValueError):
            StoryPoint.model_validate(point_bad)
    with pytest.raises(DomainError):
        validate_interval({'start': point(1), 'end': point(1), 'bounds': '[)'})


def test_proposition_codec_never_treats_prose_as_world_fact():
    encoded = encode_proposition(uid('sword'), 'possessed_by', {'kind': 'ENTITY', 'entity_id': uid('F')})
    assert decode_proposition(encoded)['predicate'] == 'possessed_by'
    assert decode_proposition('I believe the sword is gone') is None
    with pytest.raises(DomainError):
        decode_proposition('NF-PROP-1:{"subject_id":"fake"}')


def test_canonical_vectors_and_rejections():
    root = Path(__file__).resolve().parents[2]
    for vector in json.loads((root / 'contracts/canonicalization_vectors.json').read_text())['vectors']:
        assert canonical_bytes(vector['input']).decode() == vector['canonical_utf8']
        assert fingerprint(vector['input']) == vector['sha256']
    for item in [{'quantity': 0.1}, {'n': 2**53}, {'text': '\ud800'}, {1: 'key'}]:
        with pytest.raises(DomainError):
            canonical_bytes(item)
    for text in ['{"a":1,"a":2}', '{"n":NaN}', '{"n":1.2}']:
        with pytest.raises(DomainError):
            strict_json_loads(text)


def test_domain_does_not_import_persistence_transport_or_filesystem():
    import ast
    root = Path(__file__).resolve().parents[2] / 'src/novel_factory/domain'
    forbidden = {'sqlalchemy', 'psycopg', 'fastapi', 'httpx', 'pathlib', 'os', 'requests', 'socket'}
    for path in root.glob('*.py'):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or '']
            else:
                continue
            assert all(name.split('.')[0] not in forbidden for name in names), (path, names)
            assert all(not name.startswith(('novel_factory.infrastructure', 'novel_factory.interfaces')) for name in names)


def test_knowledge_cut_separates_acquired_time_from_retrospective_subject_time():
    selected = sword_history()[:4]
    selected.append(event('late_knowledge', 50, [cognition('new_present', 'V', 'theft')]))
    snapshot = replay(genesis(), selected)
    # Knowing the present holder at D50 cannot rewrite the actor's D25 target.
    assert state_at(snapshot, uid('sword'), 'holder', point(25), uid('P'), point(50))['data']['entity_id'] == uid('F')
    retrospective = cognition('past_correction', 'V', 'theft')
    retrospective['statement'] = encode_proposition(uid('sword'), 'possessed_by', {'kind':'ENTITY','entity_id':uid('V')},
                                                    {'kind':'POINT','at':point(31)})
    selected.append(event('retrospective_knowledge', 51, [retrospective]))
    updated = replay(genesis(), selected)
    assert state_at(updated, uid('sword'), 'holder', point(31), uid('P'), point(51))['data']['entity_id'] == uid('V')
    assert state_at(updated, uid('sword'), 'holder', point(31), uid('P'), point(35))['data']['entity_id'] == uid('F')


def test_resource_zero_requires_existing_identities_at_the_requested_time():
    initial = genesis()
    initial['entities'][uid('grain')]['created_at'] = point(10)
    snapshot = replay(initial, [])
    unknown = f'resource:{uid("unknown-resource")}:bag'
    known = f'resource:{uid("grain")}:bag'
    assert state_at(snapshot, uid('store'), unknown, point(20))['value_status'] == 'UNRESOLVED'
    assert state_at(snapshot, uid('store'), known, point(5))['value_status'] == 'NONE'
    assert state_at(snapshot, uid('store'), known, point(10))['data']['quantity'] == '0'
    assert resource_at(snapshot, uid('caravan'), uid('grain'), 'bag', point(10))['quantity'] == '100'
    with pytest.raises(DomainError):
        resource_at(snapshot, uid('store'), uid('unknown-resource'), 'bag', point(20))
    with pytest.raises(DomainError):
        resource_at(snapshot, uid('store'), uid('grain'), 'bag', point(5))
    with pytest.raises(DomainError):
        resource_at(snapshot, uid('unknown-account'), uid('grain'), 'bag', point(20))


@pytest.mark.parametrize('predicate', ['resource:', 'resource:not-a-uuid:bag', f'resource:{uid("grain")}:'])
def test_malformed_resource_predicate_fails_with_domain_error(predicate):
    snapshot = replay(genesis(), [])
    with pytest.raises(DomainError) as error:
        state_at(snapshot, uid('store'), predicate, point(20))
    assert error.value.code == 'INVALID_ARGUMENT'
