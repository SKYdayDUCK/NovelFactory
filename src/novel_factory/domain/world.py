"""Deterministic replay of an immutable selected history.

This reducer never fetches data, checks transport identities, changes a database,
or invokes a model. Applications must establish authoritative selection and
coverage. Retcon rebuilds from genesis plus the replacement selection; it never
applies a corrective event to an already materialized balance.
"""
from __future__ import annotations

from collections import OrderedDict, defaultdict
from copy import deepcopy
from decimal import Decimal, localcontext
import json
import threading
from uuid import UUID, uuid5

from novel_factory.contracts.registry import plain, validate_contract
from .canonical import canonical_bytes
from .errors import DomainError
from .propositions import decode_proposition
from .temporal import contains, point_key, validate_interval, validate_temporal_constraints

INTERPRETER_VERSION = 'nf-reducer-1'
PROJECTION_VERSION = 'nf-projector-1'
PREDICATES = {
    'legal_owner': ('ENTITY', True), 'possessed_by': ('ENTITY', True),
    'custody': ('ENTITY', True), 'located_in': ('ENTITY', True),
    'contains': ('ENTITY', False), 'member_of': ('ENTITY', False),
    'controlled_by': ('ENTITY', True), 'vital_state': ('STRING', True),
    'ability_state': ('STRING', True), 'on_duty': ('BOOLEAN', True),
    'blocked_by': ('ENTITY', False),
    # Carrier identity is the subject. Content and attributed readings point
    # to immutable artifact versions. An attributed reading is not its truth.
    'message_content_ref': ('STRING', True), 'attributed_reading_ref': ('STRING', True),
    'message_authentic': ('BOOLEAN', True),
}
ALIASES = {'owner': 'legal_owner', 'holder': 'possessed_by', 'active_holder': 'possessed_by',
           'last_confirmed_owner': 'legal_owner', 'location': 'located_in'}
TERMINAL_LIFECYCLES = {'DESTROYED', 'CONSUMED', 'RETIRED'}


class _InitialSnapshotMemo:
    """Bounded pure-computation memo, containing only immutable byte strings.

    This is not a revision or authorization cache. The full input bytes and
    interpreter identity select a previously validated genesis computation.
    Every caller receives its own decoded snapshot; no mutable world is shared.
    """
    def __init__(self, max_entries=8, max_bytes=16 * 1024 * 1024):
        self.max_entries, self.max_bytes = max_entries, max_bytes
        self._entries = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            value = self._entries.get(key)
            if value is not None:
                self._entries.move_to_end(key)
            return value

    def put(self, key, value):
        size = len(key[-1]) + len(value)
        if self.max_entries < 1 or size > self.max_bytes:
            return
        with self._lock:
            old = self._entries.pop(key, None)
            if old is not None:
                self._bytes -= len(key[-1]) + len(old)
            self._entries[key] = value
            self._bytes += size
            while len(self._entries) > self.max_entries or self._bytes > self.max_bytes:
                old_key, old_value = self._entries.popitem(last=False)
                self._bytes -= len(old_key[-1]) + len(old_value)


_initial_snapshot_memo = _InitialSnapshotMemo()


def _initial_snapshot(genesis):
    # replay has already applied the original strict canonical validation.
    # Preserve dictionary order in the memo identity AND output: genesis entity
    # order is reflected in lifecycle_history, so sorted-key serialization alone
    # is not a sufficient key for this existing pure computation.
    encoded = json.dumps(genesis, ensure_ascii=False, separators=(',', ':'),
                         allow_nan=False).encode('utf-8')
    key = (INTERPRETER_VERSION, PROJECTION_VERSION, tuple(sorted(PREDICATES.items())),
           tuple(sorted(TERMINAL_LIFECYCLES)), encoded)
    cached = _initial_snapshot_memo.get(key)
    if cached is not None:
        return json.loads(cached)
    snapshot = _empty_snapshot(genesis)
    encoded_snapshot = json.dumps(snapshot, ensure_ascii=False, separators=(',', ':'),
                                  allow_nan=False).encode('utf-8')
    _initial_snapshot_memo.put(key, encoded_snapshot)
    return snapshot


def resource_key(account_id: str, resource_id: str, unit: str) -> str:
    if '|' in unit:
        raise DomainError('INVALID_ARGUMENT', 'Resource unit cannot contain reserved separators')
    return f'{account_id}|{resource_id}|{unit}'


def _decimal(value: str) -> Decimal:
    validate_contract('Decimal', value)
    if len(value) > 80:
        raise DomainError('INVALID_ARGUMENT', 'Resource precision exceeds the registered kernel limit')
    return Decimal(value)


def decimal_string(value: Decimal) -> str:
    text = format(value, 'f')
    if '.' in text:
        text = text.rstrip('0').rstrip('.')
    return '0' if text in ('', '-0') else text


def _event_ref(event: dict) -> dict:
    return {key: event[key] for key in ('occurrence_id', 'event_version_id')}


def _relation_version(event: dict, relation_id: str, operation: str) -> str:
    return str(uuid5(UUID(event['event_version_id']), f'relation:{relation_id}:{operation}'))


def _active(snapshot: dict, subject: str, predicate: str, at: dict,
            plane: str = 'WORLD', actor_id: str | None = None) -> list[dict]:
    return [r for r in snapshot['relations']
            if r['subject_id'] == subject and r['predicate'] == predicate
            and r['plane'] == plane and r['actor_id'] == actor_id
            and contains(r['valid_time'], at)]


def _entity(snapshot: dict, identifier: str, at: dict | None = None):
    entity = snapshot['entities'].get(identifier)
    if entity is None:
        raise DomainError('HARD_RULE_VIOLATION', 'Effect refers to an unregistered entity')
    if at is not None and point_key(entity['created_at']) > point_key(at):
        raise DomainError('HARD_RULE_VIOLATION', 'Entity does not exist at the effect time')
    return entity


def _validate_relation(snapshot: dict, subject: str, predicate: str, value: dict, at: dict):
    _entity(snapshot, subject, at)
    if predicate not in PREDICATES:
        raise DomainError('INVALID_ARGUMENT', 'Unregistered predicate')
    validate_contract('RelationValue', value, '2.1')
    if value['kind'] != PREDICATES[predicate][0]:
        raise DomainError('HARD_RULE_VIOLATION', 'Predicate value type is incompatible')
    if value['kind'] == 'ENTITY':
        _entity(snapshot, value['entity_id'], at)
    if predicate in {'legal_owner', 'possessed_by', 'custody'}:
        kind = snapshot['entities'][subject]['entity_kind']
        if kind not in {'ITEM', 'CONTAINER', 'RESOURCE', 'BATCH', 'KNOWLEDGE_CARRIER'}:
            raise DomainError('HARD_RULE_VIOLATION', 'Possession predicate needs an ownable entity')
    if predicate in {'possessed_by', 'custody'} and lifecycle_at(snapshot, subject, at) in TERMINAL_LIFECYCLES:
        raise DomainError('HARD_RULE_VIOLATION', 'Inactive items cannot be actively held')


def _check_graph(snapshot: dict, at: dict):
    # Physical location/containment cannot contain itself. An affiliation graph
    # is not a physical graph and is deliberately excluded.
    edges = defaultdict(set)
    for rel in snapshot['relations']:
        if rel['plane'] != 'WORLD' or not contains(rel['valid_time'], at):
            continue
        if rel['predicate'] == 'located_in':
            edges[rel['subject_id']].add(rel['object_value']['entity_id'])
        elif rel['predicate'] == 'contains':
            edges[rel['object_value']['entity_id']].add(rel['subject_id'])
    visiting, visited = set(), set()
    def visit(node):
        if node in visiting:
            raise DomainError('HARD_RULE_VIOLATION', 'Physical containment contains a cycle')
        if node in visited:
            return
        visiting.add(node)
        for child in edges[node]:
            visit(child)
        visiting.remove(node)
        visited.add(node)
    for node in list(edges):
        visit(node)


def _check_cardinality(snapshot: dict, at: dict):
    counts = defaultdict(int)
    for rel in snapshot['relations']:
        if contains(rel['valid_time'], at):
            key = rel['subject_id'], rel['predicate'], rel['plane'], rel['actor_id']
            counts[key] += 1
            if PREDICATES.get(rel['predicate'], (None, False))[1] and counts[key] > 1:
                raise DomainError('HARD_RULE_VIOLATION', 'Exclusive relation has multiple active values')
    _check_graph(snapshot, at)


def _empty_snapshot(genesis: dict) -> dict:
    value = plain(genesis)
    allowed = {'entities', 'relations', 'resources', 'cognitions', 'lifecycle', 'rules'}
    if set(value) - allowed:
        raise DomainError('INVALID_ARGUMENT', 'Unknown genesis field')
    snapshot = {'entities': deepcopy(value.get('entities', {})), 'relations': [],
                'resources': {}, 'resource_history': [], 'cognitions': [],
                'lifecycle': {}, 'lifecycle_history': [], 'state_overrides': [],
                'events': [], 'projection_version': PROJECTION_VERSION}
    for identifier, entity in snapshot['entities'].items():
        validate_contract('UUID', identifier)
        if entity.get('entity_id') != identifier:
            raise DomainError('INVALID_ARGUMENT', 'Entity identity map is inconsistent')
        if entity.get('entity_kind') not in {'ITEM', 'CHARACTER', 'REGION', 'ORGANIZATION',
                                            'RESOURCE', 'ACCOUNT', 'CONTAINER', 'BATCH',
                                            'KNOWLEDGE', 'KNOWLEDGE_CARRIER', 'PROCESS'}:
            raise DomainError('INVALID_ARGUMENT', 'Unregistered entity kind')
        if not isinstance(entity.get('name'), str) or not entity['name']:
            raise DomainError('INVALID_ARGUMENT', 'Entity name is required')
        validate_contract('StoryPoint', entity['created_at'])
        if entity.get('scope_id') is not None:
            validate_contract('UUID', entity['scope_id'])
        lifecycle = value.get('lifecycle', {}).get(identifier, 'ACTIVE')
        if lifecycle not in {'ACTIVE', *TERMINAL_LIFECYCLES}:
            raise DomainError('INVALID_ARGUMENT', 'Invalid initial lifecycle')
        snapshot['lifecycle'][identifier] = lifecycle
        snapshot['lifecycle_history'].append({'entity_id': identifier, 'lifecycle': lifecycle,
                                             'at': deepcopy(entity['created_at']), 'event_ref': None})
    if set(value.get('lifecycle', {})) - set(snapshot['entities']):
        raise DomainError('HARD_RULE_VIOLATION', 'Lifecycle identity is unregistered')
    relation_ids = set()
    for relation in value.get('relations', []):
        rel = deepcopy(relation)
        # Internal relation entries become TemporalRelationRecord only when the
        # application pins their world_ref and projection_generation.
        expected = {'relation_id', 'relation_version_id', 'subject_id', 'predicate',
                    'object_value', 'valid_time', 'plane', 'actor_id', 'origin', 'evidence_refs'}
        if set(rel) != expected:
            raise DomainError('INVALID_ARGUMENT', 'Initial relation fields are incomplete or unknown')
        for key in ('relation_id', 'relation_version_id', 'subject_id'):
            validate_contract('UUID', rel[key])
        validate_contract('RelationOrigin', rel['origin'], '2.1')
        if rel['origin']['kind'] != 'RECORD':
            raise DomainError('HARD_RULE_VIOLATION', 'Initial relation must cite a setup record')
        validate_interval(rel['valid_time'], open_end=True)
        if rel['relation_id'] in relation_ids:
            raise DomainError('DUPLICATE_EFFECT', 'Initial relation identity is duplicated')
        relation_ids.add(rel['relation_id'])
        if rel['plane'] not in {'WORLD', 'BELIEF'}:
            raise DomainError('INVALID_ARGUMENT', 'Invalid initial relation plane')
        if (rel['plane'] == 'BELIEF') != (rel['actor_id'] is not None):
            raise DomainError('INVALID_ARGUMENT', 'Belief relation must bind its actor')
        if rel['actor_id'] is not None:
            _entity(snapshot, rel['actor_id'], rel['valid_time']['start'])
        for evidence in rel['evidence_refs']:
            validate_contract('EvidenceRef', evidence)
        _validate_relation(snapshot, rel['subject_id'], rel['predicate'], rel['object_value'], rel['valid_time']['start'])
        rel['source_event_refs'] = []
        snapshot['relations'].append(rel)
    for resource in value.get('resources', []):
        if set(resource) != {'account_id', 'resource_id', 'quantity', 'unit'}:
            raise DomainError('INVALID_ARGUMENT', 'Invalid initial resource entry')
        _entity(snapshot, resource['account_id'])
        _entity(snapshot, resource['resource_id'])
        key = resource_key(resource['account_id'], resource['resource_id'], resource['unit'])
        quantity = _decimal(resource['quantity'])
        if quantity < 0 or key in snapshot['resources']:
            raise DomainError('RESOURCE_CONFLICT', 'Invalid initial resource balance')
        at = max((snapshot['entities'][resource[k]]['created_at'] for k in ('account_id', 'resource_id')), key=point_key)
        snapshot['resources'][key] = decimal_string(quantity)
        snapshot['resource_history'].append({**deepcopy(resource), 'quantity': decimal_string(quantity),
                                             'at': deepcopy(at), 'event_ref': None})
    for cognition in value.get('cognitions', []):
        item = deepcopy(cognition)
        _validate_cognition(snapshot, item)
        snapshot['cognitions'].append(item)
    boundaries = {point_key(r['valid_time']['start']) for r in snapshot['relations']}
    boundaries |= {point_key(r['valid_time']['end']) for r in snapshot['relations'] if r['valid_time']['end'] is not None}
    for tick, phase in sorted(boundaries):
        _check_cardinality(snapshot, {'tick': tick, 'phase': phase})
    return snapshot


def _validate_cognition(snapshot: dict, item: dict):
    required = {'record_id', 'actor_id', 'record_kind', 'statement', 'acquired_at', 'evidence_refs'}
    allowed = required | {'subject_id', 'predicate', 'object_value', 'subject_time', 'event_ref', 'epistemic_mode'}
    if not required <= set(item) or set(item) - allowed:
        raise DomainError('INVALID_ARGUMENT', 'Invalid cognition fields')
    validate_contract('UUID', item['record_id'])
    _entity(snapshot, item['actor_id'], item['acquired_at'])
    if item['record_kind'] not in {'OBSERVATION', 'BELIEF', 'PRIVATE_DECISION'}:
        raise DomainError('INVALID_ARGUMENT', 'Invalid cognition type')
    if not isinstance(item['statement'], str) or not item['statement']:
        raise DomainError('INVALID_ARGUMENT', 'Cognition statement is required')
    for evidence in item['evidence_refs']:
        validate_contract('EvidenceRef', evidence)
    proposition = decode_proposition(item['statement'])
    typed = {'subject_id', 'predicate', 'object_value'}
    if typed & set(item) and not typed <= set(item):
        raise DomainError('INVALID_ARGUMENT', 'Typed cognition is incomplete')
    if typed <= set(item):
        supplied = {key: item[key] for key in typed}
        if 'subject_time' in item:
            supplied['subject_time'] = item['subject_time']
        validate_contract('CognitiveProposition', supplied, 'kernel')
        if proposition is not None and proposition != supplied:
            raise DomainError('INVALID_ARGUMENT', 'Cognition encoding contradicts its typed payload')
        proposition = supplied
    if proposition is not None:
        _entity(snapshot, proposition['subject_id'])
        if proposition['predicate'] not in PREDICATES:
            raise DomainError('INVALID_ARGUMENT', 'Unregistered cognitive predicate')
        # A belief may be false; validate its type, never assert it matches WORLD.
        if proposition['object_value']['kind'] != PREDICATES[proposition['predicate']][0]:
            raise DomainError('INVALID_ARGUMENT', 'Cognitive predicate type is incompatible')
        if proposition['object_value']['kind'] == 'ENTITY':
            _entity(snapshot, proposition['object_value']['entity_id'])
        if proposition.get('subject_time', {}).get('kind') == 'INTERVAL':
            validate_interval(proposition['subject_time']['interval'])
        item.update(proposition)


def _close(relation: dict, at: dict, event: dict):
    if point_key(at) <= point_key(relation['valid_time']['start']):
        raise DomainError('HARD_RULE_VIOLATION', 'Relation closure must follow its opening boundary')
    relation['valid_time']['end'] = deepcopy(at)
    relation['relation_version_id'] = _relation_version(event, relation['relation_id'], 'CLOSE')
    reference = _event_ref(event)
    if reference not in relation['source_event_refs']:
        relation['source_event_refs'].append(reference)


def _add_relation(snapshot: dict, effect: dict, at: dict, event: dict, relation_id: str):
    _validate_relation(snapshot, effect['subject_id'], effect['predicate'], effect['object_value'], at)
    snapshot['relations'].append({
        'relation_id': relation_id, 'relation_version_id': _relation_version(event, relation_id, 'OPEN'),
        'subject_id': effect['subject_id'], 'predicate': effect['predicate'],
        'object_value': deepcopy(effect['object_value']),
        'valid_time': {'start': deepcopy(at), 'end': None, 'bounds': '[)'},
        'plane': 'WORLD', 'actor_id': None, 'origin': {'kind': 'EVENT', 'event_ref': _event_ref(event)},
        'evidence_refs': [], 'source_event_refs': [_event_ref(event)],
    })


def _slots(event: dict) -> set[tuple]:
    slots = set()
    for effect in event['effects']:
        kind = effect['kind']
        if kind in {'RELATION_SET', 'RELATION_TRANSITION'}:
            slots.add(('relation', effect['subject_id'], effect['predicate']))
        elif kind == 'RESOURCE_DELTA':
            slots.add(('resource', effect['account_id'], effect['resource_id'], effect['unit']))
        elif kind == 'LIFECYCLE_SET':
            slots.add(('lifecycle', effect['entity_id']))
            slots.add(('relation', effect['entity_id'], 'possessed_by'))
            slots.add(('relation', effect['entity_id'], 'custody'))
        elif kind == 'COGNITIVE_RECORD_ADD':
            proposition = decode_proposition(effect['statement'])
            if proposition:
                slots.add(('belief', effect['actor_id'], proposition['subject_id'], proposition['predicate']))
    return slots


def replay(genesis: dict, events: list[dict], at=None) -> dict:
    """Rebuild a selected event set once, optionally through a story point.

All inputs are copied and schema-validated. There is no permission or coverage
claim in this output. Bounded occurrences with resolved relation boundaries are
supported; effects without resolved boundaries are conservatively rejected.
"""
    data = plain(genesis)
    canonical_bytes(data)
    selected = []
    seen_effects, seen_groups = set(), set()
    for raw in events:
        raw = plain(raw)
        version = raw.get('schema_version')
        if version not in {'2.0', '2.1'}:
            raise DomainError('UNSUPPORTED_SCHEMA', 'Unsupported event schema version')
        event = validate_contract('AcceptedEventR1' if version == '2.1' else 'AcceptedEvent', raw, version)
        canonical_bytes(event)
        seen_in_event = set()
        relation_kinds = defaultdict(set)
        for effect in event['effects']:
            key = event['occurrence_id'], effect['effect_key']
            group = event['effect_group_id'], effect['effect_key']
            if key in seen_effects or group in seen_groups:
                raise DomainError('DUPLICATE_EFFECT', 'Repeated selected effect identity')
            seen_effects.add(key)
            seen_groups.add(group)
            if effect['kind'] in {'RELATION_SET', 'RELATION_TRANSITION'}:
                slot = effect['subject_id'], effect['predicate']
                relation_kinds[slot].add(effect['kind'])
                if effect['kind'] == 'RELATION_SET' and slot in seen_in_event:
                    raise DomainError('DUPLICATE_EFFECT', 'One event sets the same relation twice')
                seen_in_event.add(slot)
        if any(len(kinds) > 1 for kinds in relation_kinds.values()):
            raise DomainError('DUPLICATE_EFFECT', 'Legacy and R1 effects target the same relation slot')
        selected.append(event)
    order = validate_temporal_constraints(selected)
    topo = {key: index for index, key in enumerate(order)}
    snapshot = _initial_snapshot(data)
    limit = point_key(at) if at is not None else None
    operations = []
    for event in selected:
        occurrence = event['occurrence_time']
        if occurrence['kind'] != 'EXACT':
            if any(effect['kind'] != 'RELATION_TRANSITION' for effect in event['effects']):
                raise DomainError('TIME_AMBIGUOUS', 'State effects need a proved effective boundary')
            # RelationTransition.at is explicitly resolved; the occurrence can
            # remain bounded. A future boundary may be authorized by its rule.
            if occurrence['kind'] == 'BOUNDED' and any(point_key(e['at']) < point_key(occurrence['earliest']) for e in event['effects']):
                raise DomainError('HARD_RULE_VIOLATION', 'Effect precedes every possible occurrence')
        default_at = occurrence.get('at')
        by_time = defaultdict(list)
        for effect in event['effects']:
            effective = effect['at'] if effect['kind'] == 'RELATION_TRANSITION' else default_at
            if default_at is not None and point_key(effective) < point_key(default_at):
                raise DomainError('HARD_RULE_VIOLATION', 'Effect precedes its event')
            by_time[point_key(effective)].append(effect)
        for time, effects in by_time.items():
            operations.append((time, topo[event['occurrence_id']], event, effects))
        snapshot['events'].append({'event_ref': _event_ref(event), 'occurrence_time': deepcopy(occurrence),
                                   'participants': deepcopy(event['participants'])})
    # Stable ordering of independent events does not establish causality.
    operations.sort(key=lambda row: (row[0], row[1]))
    simultaneous = {}
    for time, _, event, effects in operations:
        if limit is not None and time > limit:
            continue
        event_slots = _slots({**event, 'effects': effects})
        previous = simultaneous.setdefault(time, {})
        for slot in event_slots:
            if slot in previous and previous[slot] != event['occurrence_id']:
                raise DomainError('HARD_RULE_VIOLATION', 'Overlapping simultaneous effects need one adjudicated event')
            previous[slot] = event['occurrence_id']
        moment = {'tick': time[0], 'phase': time[1]}
        _apply_group(snapshot, event, effects, moment)
    boundaries = {point_key(r['valid_time']['start']) for r in snapshot['relations']}
    boundaries |= {point_key(r['valid_time']['end']) for r in snapshot['relations'] if r['valid_time']['end'] is not None}
    for tick, phase in sorted(boundaries):
        _check_cardinality(snapshot, {'tick': tick, 'phase': phase})
    # The complete history is retained; `at` is a state cut, not a new history.
    if at is not None:
        snapshot['lifecycle'] = {key: lifecycle_at(snapshot, key, at) for key in snapshot['entities']}
    return snapshot


def _apply_group(snapshot: dict, event: dict, effects: list[dict], at: dict):
    for participant in event['participants']:
        _entity(snapshot, participant, at)
    # Resource changes belonging to one event are validated as one vector; no
    # transient negative account caused solely by ordering is materialized.
    deltas, totals = defaultdict(Decimal), defaultdict(Decimal)
    details = {}
    with localcontext() as context:
        context.prec = 200
        for effect in effects:
            if effect['kind'] != 'RESOURCE_DELTA':
                continue
            _entity(snapshot, effect['account_id'], at)
            _entity(snapshot, effect['resource_id'], at)
            quantity = _decimal(effect['quantity'])
            key = resource_key(effect['account_id'], effect['resource_id'], effect['unit'])
            deltas[key] += quantity
            totals[(effect['resource_id'], effect['unit'])] += quantity
            details[key] = effect
        if any(value != 0 for value in totals.values()):
            raise DomainError('HARD_RULE_VIOLATION', 'Registered RESOURCE_DELTA template conserves each resource and unit')
        updated = {}
        for key, delta in deltas.items():
            quantity = _decimal(snapshot['resources'].get(key, '0')) + delta
            if quantity < 0:
                raise DomainError('RESOURCE_CONFLICT', 'Insufficient resource balance')
            updated[key] = decimal_string(quantity)
        for key, quantity in updated.items():
            snapshot['resources'][key] = quantity
            effect = details[key]
            snapshot['resource_history'].append({'account_id': effect['account_id'], 'resource_id': effect['resource_id'],
                                                 'unit': effect['unit'], 'quantity': quantity, 'at': deepcopy(at),
                                                 'event_ref': _event_ref(event)})
    # Explicit closure precedes opening within the same atomic transition.
    for effect in effects:
        if effect['kind'] == 'RELATION_TRANSITION' and effect['operation'] == 'CLOSE':
            matches = [r for r in snapshot['relations'] if r['relation_id'] == effect['relation_id']]
            if len(matches) != 1:
                raise DomainError('HARD_RULE_VIOLATION', 'Closing relation is not selected')
            relation = matches[0]
            if (relation['relation_version_id'] != effect['expected_relation_version_id']
                    or relation['subject_id'] != effect['subject_id']
                    or relation['predicate'] != effect['predicate']
                    or relation['object_value'] != effect['object_value']
                    or relation['plane'] != 'WORLD' or relation['valid_time']['end'] is not None):
                raise DomainError('DEPENDENCY_CONFLICT', 'Closing relation does not match the selected version')
            _close(relation, at, event)
    for effect in effects:
        kind = effect['kind']
        if kind == 'RESOURCE_DELTA' or kind == 'RELATION_TRANSITION' and effect['operation'] == 'CLOSE':
            continue
        if kind == 'RELATION_TRANSITION':
            if any(r['relation_id'] == effect['relation_id'] for r in snapshot['relations']):
                raise DomainError('DUPLICATE_EFFECT', 'OPEN cannot reuse a relation identity')
            _add_relation(snapshot, effect, at, event, effect['relation_id'])
        elif kind == 'RELATION_SET':
            if effect['predicate'] not in PREDICATES or PREDICATES[effect['predicate']][0] != 'ENTITY':
                raise DomainError('INVALID_ARGUMENT', 'Legacy relation needs a registered entity-valued predicate')
            _entity(snapshot, effect['subject_id'], at)
            for relation in _active(snapshot, effect['subject_id'], effect['predicate'], at):
                _close(relation, at, event)
            if effect['value_status'] == 'VALUE':
                value = {'kind': 'ENTITY', 'entity_id': effect['object_id']}
                relation_id = str(uuid5(UUID(event['occurrence_id']), 'relation:' + effect['effect_key']))
                if any(r['relation_id'] == relation_id for r in snapshot['relations']):
                    raise DomainError('DUPLICATE_EFFECT', 'Repeated legacy relation identity')
                _add_relation(snapshot, {**effect, 'object_value': value}, at, event, relation_id)
            else:
                snapshot['state_overrides'].append({'subject_id': effect['subject_id'], 'predicate': effect['predicate'],
                                                    'value_status': effect['value_status'], 'at': deepcopy(at),
                                                    'event_ref': _event_ref(event)})
        elif kind == 'LIFECYCLE_SET':
            subject = effect['entity_id']
            _entity(snapshot, subject, at)
            current = lifecycle_at(snapshot, subject, at)
            if current in TERMINAL_LIFECYCLES:
                raise DomainError('HARD_RULE_VIOLATION', 'Terminal lifecycle cannot be reapplied or resurrected')
            snapshot['lifecycle'][subject] = effect['lifecycle']
            snapshot['lifecycle_history'].append({'entity_id': subject, 'lifecycle': effect['lifecycle'],
                                                  'at': deepcopy(at), 'event_ref': _event_ref(event)})
            if effect['lifecycle'] in TERMINAL_LIFECYCLES:
                for predicate in ('possessed_by', 'custody'):
                    for relation in _active(snapshot, subject, predicate, at):
                        _close(relation, at, event)
        elif kind == 'COGNITIVE_RECORD_ADD':
            if effect['actor_id'] not in event['participants']:
                raise DomainError('HARD_RULE_VIOLATION', 'Cognition actor must participate in its source event')
            if not effect['evidence_refs']:
                raise DomainError('INVALID_EVIDENCE', 'Runtime cognition requires source evidence')
            item = {'record_id': str(uuid5(UUID(event['occurrence_id']), 'cognition:' + effect['effect_key'])),
                    'actor_id': effect['actor_id'], 'record_kind': effect['record_kind'],
                    'epistemic_mode': effect['epistemic_mode'], 'statement': effect['statement'],
                    'acquired_at': deepcopy(at), 'evidence_refs': deepcopy(effect['evidence_refs']),
                    'event_ref': _event_ref(event)}
            if any(c['record_id'] == item['record_id'] for c in snapshot['cognitions']):
                raise DomainError('DUPLICATE_EFFECT', 'Cognition identity is repeated')
            _validate_cognition(snapshot, item)
            snapshot['cognitions'].append(item)
        else:
            raise DomainError('INVALID_ARGUMENT', 'Unsupported effect kind')
    _check_cardinality(snapshot, at)


def lifecycle_at(snapshot: dict, entity_id: str, at=None) -> str | None:
    history = [h for h in snapshot['lifecycle_history'] if h['entity_id'] == entity_id
               and (at is None or point_key(h['at']) <= point_key(at))]
    return max(enumerate(history), key=lambda pair: (point_key(pair[1]['at']), pair[0]))[1]['lifecycle'] if history else None


def resource_at(snapshot: dict, account_id: str, resource_id: str, unit: str, at=None) -> dict:
    # Zero is the closed-world default for an existing account/resource pair,
    # never evidence that an arbitrary or not-yet-created identity exists.
    validate_contract('UUID', account_id)
    validate_contract('UUID', resource_id)
    if not isinstance(unit, str) or not unit:
        raise DomainError('INVALID_ARGUMENT', 'A resource balance requires a unit')
    resource_key(account_id, resource_id, unit)
    _entity(snapshot, account_id, at)
    _entity(snapshot, resource_id, at)
    history = [h for h in snapshot['resource_history']
               if h['account_id'] == account_id and h['resource_id'] == resource_id and h['unit'] == unit
               and (at is None or point_key(h['at']) <= point_key(at))]
    selected = max(enumerate(history), key=lambda pair: (point_key(pair[1]['at']), pair[0]))[1] if history else None
    return {'kind': 'RESOURCE_BALANCE', 'account_id': account_id, 'resource_id': resource_id,
            'quantity': selected['quantity'] if selected else '0', 'unit': unit}


def state_at(snapshot: dict, subject_id: str, predicate: str, at=None,
             actor_id: str | None = None, known_at=None) -> dict:
    """Return a typed *internal* answer. Applications add coverage and grants.

Missing belief and missing hidden entity both remain UNRESOLVED; this function
never turns a world relation into actor knowledge.
"""
    answer = {'value_status': 'UNRESOLVED', 'data': None, 'evidence_refs': [], 'source_event_refs': []}
    entity = snapshot['entities'].get(subject_id)
    if entity is None:
        return answer
    moment = plain(at) if at is not None else None
    canonical_predicate = ALIASES.get(predicate, predicate)
    if actor_id is not None:
        if known_at is None:
            raise DomainError('INVALID_ARGUMENT', 'Belief queries must pin known_at')
        beliefs = [c for c in snapshot['cognitions']
                   if c['actor_id'] == actor_id and c['record_kind'] == 'BELIEF'
                   and c.get('subject_id') == subject_id and c.get('predicate') == canonical_predicate
                   and point_key(c['acquired_at']) <= point_key(known_at)]
        if moment is not None:
            def about_target(cognition):
                target = cognition.get('subject_time')
                if target is None:
                    # A later observation of the current state supplies no
                    # retrospective evidence about an earlier story point.
                    return point_key(cognition['acquired_at']) <= point_key(moment)
                if target['kind'] == 'POINT':
                    return point_key(target['at']) == point_key(moment)
                return contains(target['interval'], moment)
            beliefs = [c for c in beliefs if about_target(c)]
        # An observation records receipt; it is not silently upgraded to belief.
        if beliefs:
            latest = max(beliefs, key=lambda c: point_key(c['acquired_at']))
            answer.update(value_status='VALUE', data=deepcopy(latest['object_value']),
                          evidence_refs=deepcopy(latest['evidence_refs']))
            # Do not disclose the objective source event merely because this
            # cognitive record exists; only its already permitted evidence.
            return answer
        if moment is not None:
            relations = _active(snapshot, subject_id, canonical_predicate, moment, 'BELIEF', actor_id)
            relations = [r for r in relations if point_key(r['valid_time']['start']) <= point_key(known_at)]
            if relations:
                answer.update(value_status='VALUE', data=deepcopy(relations[-1]['object_value']),
                              evidence_refs=deepcopy(relations[-1]['evidence_refs']))
        return answer
    if moment is not None and point_key(entity['created_at']) > point_key(moment):
        answer['value_status'] = 'NONE'
        return answer
    if predicate.startswith('resource:'):
        parts = predicate.split(':', 2)
        if len(parts) != 3 or not parts[2]:
            raise DomainError('INVALID_ARGUMENT', 'Resource predicate requires an identity and unit')
        _, resource, unit = parts
        validate_contract('UUID', resource)
        resource_entity = snapshot['entities'].get(resource)
        if resource_entity is None:
            return answer
        if moment is not None and point_key(resource_entity['created_at']) > point_key(moment):
            answer['value_status'] = 'NONE'
            return answer
        answer.update(value_status='VALUE', data=resource_at(snapshot, subject_id, resource, unit, moment))
        return answer
    lifecycle = lifecycle_at(snapshot, subject_id, moment)
    if predicate == 'lifecycle':
        if lifecycle is not None:
            answer.update(value_status='VALUE', data={'kind': 'STRING', 'value': lifecycle})
        return answer
    if predicate == 'active_use':
        if lifecycle in TERMINAL_LIFECYCLES:
            answer['value_status'] = 'NOT_APPLICABLE'
        elif lifecycle == 'ACTIVE':
            answer.update(value_status='VALUE', data={'kind': 'BOOLEAN', 'value': True})
        return answer
    if canonical_predicate in {'possessed_by', 'custody'} and lifecycle in TERMINAL_LIFECYCLES:
        answer['value_status'] = 'NONE'
        return answer
    if canonical_predicate not in PREDICATES:
        return answer
    relations = [r for r in snapshot['relations']
                 if r['subject_id'] == subject_id and r['predicate'] == canonical_predicate
                 and r['plane'] == 'WORLD'
                 and ((r['valid_time']['end'] is None) if moment is None else contains(r['valid_time'], moment))]
    if relations:
        if PREDICATES[canonical_predicate][1]:
            answer.update(value_status='VALUE', data=deepcopy(relations[0]['object_value']))
        else:
            # Multi-valued relations are returned as internal records; query
            # adapters pin world_ref and strip implementation metadata.
            answer.update(value_status='VALUE', data=deepcopy(relations))
        for relation in relations:
            answer['evidence_refs'].extend(deepcopy(relation['evidence_refs']))
            for reference in relation['source_event_refs']:
                if reference not in answer['source_event_refs']:
                    answer['source_event_refs'].append(deepcopy(reference))
        return answer
    overrides = [o for o in snapshot['state_overrides']
                 if o['subject_id'] == subject_id and o['predicate'] == canonical_predicate
                 and (moment is None or point_key(o['at']) <= point_key(moment))]
    if overrides:
        override = max(overrides, key=lambda o: point_key(o['at']))
        answer.update(value_status=override['value_status'], source_event_refs=[deepcopy(override['event_ref'])])
    return answer
