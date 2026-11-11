"""Versioned causal keys for the closed world interpreter.

A slot intentionally omits its value: transitions and previously empty slots
must meet reads of that predicate. Proposition identities retain the value.
Only an explicitly typed statement can share a proposition identity across sources.
"""
from .canonical import fingerprint
from .propositions import decode_proposition
from .world import ALIASES, TERMINAL_LIFECYCLES


def slot(subject, predicate):
    return 'fact:' + subject + ':' + ALIASES.get(predicate, predicate)


def proposition_key(statement, source_version):
    typed = decode_proposition(statement)
    if typed is None:
        return 'uncertain:' + fingerprint({'source_version': source_version, 'verbatim': statement})
    return 'proposition:' + fingerprint({'version': 1, **typed,
                                         'predicate': ALIASES.get(typed['predicate'], typed['predicate'])})


def change_keys(events, footprint, repair=False):
    keys = {'guard:' + scope for scope in footprint['bumps']}
    for event in events:
        keys.update('timeline:' + actor for actor in event['participants'])
        for effect in event['effects']:
            kind = effect['kind']
            if kind.startswith('RELATION_'):
                subject, predicate = effect['subject_id'], effect['predicate']
                keys.update((slot(subject, predicate), 'range:' + predicate, 'timeline:' + subject))
            elif kind == 'RESOURCE_DELTA':
                keys.add(slot(effect['account_id'], 'resource:' + effect['resource_id'] + ':' + effect['unit']))
            elif kind == 'LIFECYCLE_SET':
                subject = effect['entity_id']
                keys.update(slot(subject, p) for p in ('lifecycle', 'possessed_by', 'custody', 'active_use'))
                if effect['lifecycle'] in TERMINAL_LIFECYCLES:
                    keys.update(('range:possessed_by', 'range:custody', 'timeline:' + subject))
            elif kind == 'COGNITIVE_RECORD_ADD':
                actor = effect['actor_id']
                keys.add('cognition:' + actor)
                prop = decode_proposition(effect['statement'])
                if prop and effect['record_kind'] == 'BELIEF':
                    keys.add('belief:' + actor + ':' + slot(prop['subject_id'], prop['predicate']))
    if repair:
        keys.add('history')
    return sorted(keys)
