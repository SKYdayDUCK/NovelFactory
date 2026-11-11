"""Fixed-cut queries and context compilation over the same authoritative kernel.

Database snapshots are held only for bounded sampling. Traversal, formatting,
file persistence and token budgeting run after the read transaction is closed.
Every delivery (including a cache hit) rechecks the live grant and task fence.
"""
from __future__ import annotations

from base64 import urlsafe_b64decode, urlsafe_b64encode
from collections import OrderedDict, deque
from copy import deepcopy
import threading
from uuid import UUID, uuid4, uuid5

from novel_factory.contracts.registry import validate_contract
from novel_factory.domain.canonical import canonical_bytes, fingerprint, strict_json_loads
from novel_factory.domain.errors import DomainError
from novel_factory.domain.temporal import point_key, contains, validate_interval
from novel_factory.domain.world import (state_at, lifecycle_at, PREDICATES, ALIASES, PROJECTION_VERSION)
from .query_policy import check_profile
from .semantic_dependencies import record_query


def _id():
    return str(uuid4())


def _require(condition, code, message):
    if not condition:
        raise DomainError(code, message)


def _dedupe(values):
    seen, result = set(), []
    for value in values:
        key = fingerprint(value)
        if key not in seen:
            result.append(deepcopy(value)); seen.add(key)
    return result


def _wr(revision):
    return {key: revision[key] for key in ('work_id', 'branch_id', 'world_revision_id')}


class WorldQueryService:
    def __init__(self, kernel, *, cache_size=256, max_scan=10000, max_response_bytes=2_000_000):
        self.kernel = kernel
        self.cache_size = cache_size
        self.max_scan = max_scan
        self.max_response_bytes = max_response_bytes
        self._cache = OrderedDict()
        self._cache_lock = threading.RLock()
        self.cache_hits = self.cache_misses = 0

    def _task(self, repo, principal, task_id, generation, capability='READ', *, lock=None):
        task = repo.one('tasks', {'task_id': task_id}, lock=lock)
        _require(task, 'VIEW_DENIED', 'Task is unavailable')
        grant = self.kernel.authorize(repo, principal, task['work_id'], capability, task['branch_id'])
        _require(task['principal_id'] == principal.principal_id and task['grant_id'] == principal.grant_id,
                 'VIEW_DENIED', 'Task is unavailable')
        _require(task['generation'] == generation, 'STALE_GENERATION', 'Task generation is no longer current')
        _require(task['state'] in {'READY', 'RUNNING', 'RESULT_STORED', 'VALIDATING', 'WAITING_REVIEW', 'ACCEPTED'},
                 'TASK_NOT_ACCEPTABLE', 'Task has no current read qualification')
        now = repo.now()
        _require(task['deadline'] > now, 'DEADLINE_EXCEEDED', 'Task deadline has passed')
        if task['state'] == 'RUNNING':
            _require(task['lease_expiry'] is not None and task['lease_expiry'] > now,
                     'LEASE_EXPIRED', 'Task execution lease has expired')
        record = repo.one('task_inputs', {'task_id': task_id, 'generation': generation})
        _require(record, 'TASK_NOT_ACCEPTABLE', 'Task input is unavailable')
        spec = record['spec']
        work = repo.one('works', {'work_id': task['work_id']})
        _require(spec['policy_version'] == work['policy_version'], 'TASK_REBIND_REQUIRED', 'Task policy is no longer current')
        _require(fingerprint(spec) == record['input_hash'], 'TASK_NOT_ACCEPTABLE', 'Task input integrity failed')
        return task, grant, spec

    def _selector(self, repo, principal, read_contract, task, cursor=None):
        selector = read_contract['revision']
        if cursor:
            wr = cursor['world_ref']
            if selector['mode'] == 'FIXED':
                _require(selector['world_ref'] == wr, 'INVALID_ARGUMENT', 'Cursor belongs to another revision')
            else:
                target = selector['branch_selector']
                _require(target['work_id'] == wr['work_id'], 'INVALID_ARGUMENT', 'Cursor belongs to another work')
                if target['kind'] == 'EXPLICIT':
                    _require(target['branch_id'] == wr['branch_id'], 'INVALID_ARGUMENT', 'Cursor belongs to another branch')
                else:
                    current = self.kernel.head(repo, target['work_id'])
                    _require(current['branch_id'] == wr['branch_id'], 'CANON_MOVED', 'Canon changed since the first page')
        elif selector['mode'] == 'FIXED':
            wr = selector['world_ref']
        else:
            target = selector['branch_selector']
            _require(target['work_id'] == task['work_id'], 'VIEW_DENIED', 'World is outside the task grant')
            wr = self.kernel.head(repo, target['work_id'], target.get('branch_id'))
        _require(wr['work_id'] == task['work_id'], 'VIEW_DENIED', 'World is outside the task grant')
        self.kernel.authorize(repo, principal, wr['work_id'], 'READ', wr['branch_id'])
        if selector['mode'] == 'READ_YOUR_WRITES':
            receipt = self.kernel.artifact(repo, selector['min_receipt_ref'], wr['work_id'])
            proof = repo.one('receipts', {'work_id': wr['work_id'], 'principal_id': principal.principal_id,
                                          'command_type': 'ACCEPT', 'payload': receipt['payload']})
            _require(proof, 'INVALID_ARGUMENT', 'Read-your-writes needs the exact stored receipt of this principal')
            minimum = receipt['payload'].get('world_ref')
            _require(minimum is not None, 'INVALID_ARGUMENT', 'Receipt has no world revision')
            if minimum['branch_id'] != wr['branch_id']:
                raise DomainError('CANON_MOVED', 'The selected branch does not contain this write')
            _require(repo.ancestor(minimum, wr), 'CONSISTENCY_UNAVAILABLE', 'The selected revision does not include this write')
        return wr

    def _cursor(self, principal, request):
        cursor = request.get('cursor')
        if cursor is None:
            return None
        _require(len(cursor) <= 8192, 'INVALID_ARGUMENT', 'Cursor exceeds its size limit')
        try:
            value = strict_json_loads(urlsafe_b64decode(cursor.encode('ascii')))
        except Exception as exc:
            raise DomainError('INVALID_ARGUMENT', 'Invalid query cursor') from exc
        _require(set(value) == {'world_ref', 'offset', 'signature'}, 'INVALID_ARGUMENT', 'Invalid cursor fields')
        validate_contract('WorldRef', value['world_ref'])
        _require(type(value['offset']) is int and 0 <= value['offset'] <= self.max_scan,
                 'INVALID_ARGUMENT', 'Cursor offset is outside the scan budget')
        _require(value['signature'] == self._query_signature(principal, request), 'INVALID_ARGUMENT', 'Cursor belongs to another query')
        # A cursor is an untrusted selector, never an authorization token. Every
        # referenced work, branch, task, plane and time is checked again.
        return value

    def _query_signature(self, principal, request):
        return fingerprint({'principal': principal.principal_id, 'grant': principal.grant_id,
                            **{key: request[key] for key in ('task_id', 'generation', 'knowledge_cut', 'subject_time', 'query', 'page_size')}})

    def _next_cursor(self, principal, request, wr, offset):
        return urlsafe_b64encode(canonical_bytes({'world_ref': wr, 'offset': offset,
                                                 'signature': self._query_signature(principal, request)})).decode('ascii')

    def _sample(self, principal, request, *, compile_context=False, cursor=None):
        with self.kernel.db.uow(write=False, repeatable_read=True) as repo:
            task, grant, spec = self._task(repo, principal, request['task_id'], request['generation'],
                                          'COMPILE' if compile_context else 'READ')
            wr = self._selector(repo, principal, request['read_contract'], task, cursor)
            revision = self.kernel.revision(repo, wr)
            plane = request['knowledge_cut']['plane']
            if plane not in {'WORLD', 'BELIEF', 'PLAN', 'SIMULATION'}:
                raise DomainError('UNSUPPORTED_SCHEMA', 'This knowledge plane is not implemented in P1')
            check_profile(grant, plane, recipe=request.get('recipe_id'))
            if plane == 'BELIEF':
                cut = request['knowledge_cut']
                if grant['actor_id'] is not None:
                    _require(cut['actor_id'] == grant['actor_id'], 'VIEW_DENIED', 'Actor view is outside this grant')
                _require(point_key(cut['known_at']) <= point_key(grant['max_story_time']) and
                         point_key(cut['known_at']) <= point_key(spec['story_time']),
                         'VIEW_DENIED', 'Knowledge time is outside the task grant')
            branch = repo.one('branches', {'work_id': wr['work_id'], 'branch_id': wr['branch_id']})
            if branch['kind'] == 'SIMULATION':
                _require(plane in {'SIMULATION', 'BELIEF', 'PLAN'}, 'VIEW_DENIED', 'Simulation facts require an explicit simulation view')
            if plane == 'SIMULATION':
                _require(branch['kind'] in {'SIMULATION', 'CANDIDATE'}, 'VIEW_DENIED', 'Simulation view requires an authorized candidate branch')
            if compile_context:
                _require(wr == spec['world_ref'], 'TASK_REBIND_REQUIRED', 'Resolved world differs from immutable task input')
            # STATE, graph, belief, plan and context compilation use the
            # already materialized immutable revision projection. Only a FACT
            # timeline requires the selected event bodies. QueryBatch gets the
            # same fixed cut and needs bodies iff at least one item needs them.
            _require(len(revision['selection']) <= self.max_scan, 'CAPACITY_LIMIT',
                     'Selected history exceeds the P1 query scan budget')
            queries = [request['query']] if 'query' in request else [item['query'] for item in request.get('items', [])]
            needs_events = (not compile_context and plane in {'WORLD', 'SIMULATION'} and
                            any(q['kind'] == 'TIMELINE' and 'FACT' in q['entry_kinds'] for q in queries))
            events = []
            if needs_events:
                selected = revision['selection'].values()
                event_ids = [v['event_version_id'] if isinstance(v, dict) else v for v in selected]
                if event_ids:
                    events = repo.many('events', {'work_id': wr['work_id'], 'event_version_id': event_ids})
            plans = None
            refs = []
            if plane == 'PLAN':
                ref = request['knowledge_cut']['plan_manifest_ref']
                artifact = self.kernel.artifact(repo, ref, wr['work_id'], kind='PLAN_MANIFEST')
                plans = validate_contract('PlanManifest', artifact['payload'], 'kernel')
                _require(len({plan['plan_id'] for plan in plans['plans']}) == len(plans['plans']),
                         'INVALID_ARGUMENT', 'Plan identities must be unique within a manifest')
                for plan in plans['plans']:
                    self.kernel.artifact(repo, plan['plan_ref'], wr['work_id'])
                refs.append(ref)
            manifest = None
            if compile_context:
                ref = request['input_manifest_ref']
                a = self.kernel.artifact(repo, ref, wr['work_id'], kind='INPUT_MANIFEST')
                _require(ref in spec['input_artifacts'], 'VIEW_DENIED', 'Input manifest is not bound to this task generation')
                manifest = validate_contract('InputManifest', a['payload'], 'kernel')
                refs.append(ref)
            indexes = repo.many('index_watermarks', {'branch_id': wr['branch_id']})
            read_cut = {'world_ref': wr, 'knowledge_cut': deepcopy(request['knowledge_cut']),
                        'policy_version': spec['policy_version'], 'permission_epoch': grant['epoch'],
                        'resolved_at': repo.now().isoformat(), 'source_manifest_refs': refs,
                        'read_cut_id': _id(), 'interpretation_ref': revision['interpretation_ref']}
            validate_contract('ResolvedReadCut', read_cut, '2.1')
            return {'task': task, 'grant': grant, 'spec': spec, 'revision': revision, 'branch': branch,
                    'read_cut': read_cut, 'events': [row['payload'] for row in events],
                    'plans': plans, 'manifest': manifest, 'indexes': indexes}

    def _delivery_check(self, principal, request, packet, observations):
        with self.kernel.db.uow() as repo:
            wr = packet['read_cut']['world_ref']
            self.kernel.authorize(repo, principal, wr['work_id'], 'READ', wr['branch_id'])
            head = repo.one('branches', {'work_id': wr['work_id'], 'branch_id': wr['branch_id']}, lock='share')
            # Fence concurrent result acceptance while the delivered query
            # becomes a mandatory dependency of this exact generation.
            _, grant, _ = self._task(repo, principal, request['task_id'], request['generation'], lock='share')
            _require(grant['epoch'] == packet['grant']['epoch'], 'VIEW_DENIED', 'Authorization changed before delivery')
            for child, payload in observations:
                record_query(repo, packet, child, payload, 'QUERY', head['head_revision_id'])


    def _time_check(self, packet, subject_time):
        if subject_time['kind'] == 'POINT':
            endpoint = subject_time['at']
        else:
            validate_interval(subject_time['interval'])
            endpoint = subject_time['interval']['end']
        _require(point_key(endpoint) <= point_key(packet['grant']['max_story_time']),
                 'VIEW_DENIED', 'Story time is outside this grant')
        if packet['grant']['role_id'] == 'character_actor':
            _require(point_key(endpoint) <= point_key(packet['spec']['story_time']),
                     'VIEW_DENIED', 'Story time is outside this task')

    def _covered(self, packet, subject_id, subject_time):
        entity = packet['revision']['snapshot']['entities'].get(subject_id)
        if entity is None:
            return False
        scope = entity.get('scope_id', subject_id)
        frontier = packet['revision']['frontiers'].get(scope)
        if frontier is None:
            return False
        if subject_time['kind'] == 'POINT':
            return point_key(subject_time['at']) < point_key(frontier)
        return point_key(subject_time['interval']['end']) <= point_key(frontier)

    def _handle(self, packet, request):
        digest = fingerprint({'world_ref': packet['read_cut']['world_ref'],
                              'permission_epoch': packet['grant']['epoch'], 'principal': packet['grant']['principal_id'],
                              'knowledge_cut': request['knowledge_cut'], 'subject_time': request['subject_time'], 'query': request['query']})
        return str(uuid5(UUID(packet['read_cut']['world_ref']['world_revision_id']), digest))

    def _public_relation(self, relation, packet):
        result = {key: deepcopy(value) for key, value in relation.items() if key != 'source_event_refs'}
        result.update(world_ref=packet['read_cut']['world_ref'], projection_generation=PROJECTION_VERSION)
        for ref in relation.get('source_event_refs', []):
            result['evidence_refs'].append({'kind': 'EVENT', 'world_ref': packet['read_cut']['world_ref'], 'event_ref': ref})
        result['evidence_refs'] = _dedupe(result['evidence_refs'])
        validate_contract('TemporalRelationRecord', result, '2.1')
        return result

    def _state(self, packet, request, handle):
        _require(request['subject_time']['kind'] == 'POINT', 'INVALID_ARGUMENT', 'P1 STATE requires an exact story point')
        _require(request['knowledge_cut']['plane'] != 'PLAN', 'INVALID_ARGUMENT', 'Plans are queried as temporal entries, not world state')
        at = request['subject_time']['at']; cut = request['knowledge_cut']; plane = cut['plane']
        snapshot = packet['revision']['snapshot']; items = []
        for subject in request['query']['subject_ids']:
            for predicate in request['query']['predicates']:
                value = state_at(snapshot, subject, predicate, at, cut.get('actor_id'), cut.get('known_at'))
                covered = self._covered(packet, subject, request['subject_time'])
                # Actor answers never reveal hidden process frontiers, even if
                # the underlying objective world has a pending scope.
                actor = plane == 'BELIEF'
                known = value['value_status'] != 'UNRESOLVED'
                if not actor and not covered:
                    if request['read_contract']['process_policy'] == 'REQUIRE_EVALUATED':
                        raise DomainError('PROCESS_NOT_EVALUATED', 'Requested scope is not evaluated in this fixed revision', True, 'WAIT')
                    value = {'value_status': 'UNRESOLVED', 'data': None, 'evidence_refs': [], 'source_event_refs': []}
                    known = False
                evidence = list(value['evidence_refs'])
                if not actor:
                    evidence += [{'kind': 'EVENT', 'world_ref': packet['read_cut']['world_ref'], 'event_ref': ref}
                                 for ref in value['source_event_refs']]
                data = value['data']
                if isinstance(data, list):
                    data = [self._public_relation(relation, packet) for relation in data]
                answer = {'world_ref': packet['read_cut']['world_ref'], 'story_time': at, 'plane': plane,
                          'truth_status': 'DETERMINED' if known else 'UNDETERMINED',
                          'evaluation_status': 'EVALUATED' if actor or covered else 'PENDING',
                          'knowledge_status': 'KNOWN' if known else 'UNKNOWN',
                          'freshness': 'CURRENT', 'coverage': 'COMPLETE' if (known and (actor or covered)) else ('UNDECLARED' if actor else 'PARTIAL'),
                          'value_status': value['value_status'], 'data': data,
                          'evidence_refs': _dedupe(evidence) if known else [], 'dependency_handle': handle}
                validate_contract('TypedStateAnswer', answer, '2.1')
                items.append({'subject_id': subject, 'predicate': predicate, 'answer': answer})
        return {'kind': 'STATE', 'state_items': items}

    @staticmethod
    def _time_matches(time, interval, subject_time):
        if interval is not None:
            if subject_time['kind'] == 'POINT':
                return contains(interval, subject_time['at'])
            wanted = subject_time['interval']
            return ((interval['end'] is None or point_key(wanted['start']) < point_key(interval['end']))
                    and point_key(interval['start']) < point_key(wanted['end']))
        if time is None or time['kind'] == 'UNSPECIFIED':
            return True  # Explicitly retained as unlocated, not a fabricated date.
        lo = time.get('at', time.get('earliest')); hi = time.get('at', time.get('latest'))
        if subject_time['kind'] == 'POINT':
            return point_key(lo) <= point_key(subject_time['at']) <= point_key(hi)
        wanted = subject_time['interval']
        return point_key(lo) < point_key(wanted['end']) and point_key(hi) >= point_key(wanted['start'])

    def _timeline(self, packet, request, handle, offset):
        cut = request['knowledge_cut']; plane = cut['plane']; query = request['query']
        subjects = set(query['subject_ids']); snapshot = packet['revision']['snapshot']; wr = packet['read_cut']['world_ref']
        entries = []
        if plane == 'PLAN':
            for plan in packet['plans']['plans']:
                if not subjects.intersection(plan['subject_ids'] + plan['requires_active']):
                    continue
                if not query.get('include_inactive', False) and plan['status'] in {'CANCELLED', 'SUPERSEDED'}:
                    continue
                if not self._time_matches(plan['target_time'], None, request['subject_time']):
                    continue
                invalid = [subject for subject in plan['requires_active'] if lifecycle_at(snapshot, subject) in {'DESTROYED', 'CONSUMED', 'RETIRED'}]
                summary = plan['summary']
                if invalid:
                    summary += '；当前世界版本中必需实例已终止，执行资格无效；此计划未产生世界效果。'
                else:
                    summary += '；计划尚未产生世界效果。'
                entries.append({'entry_id': plan['plan_id'], 'entry_kind': 'PLAN', 'world_ref': wr,
                                'source': {'kind': 'PLAN', 'plan_ref': plan['plan_ref'],
                                           'plan_manifest_ref': cut['plan_manifest_ref'], 'plan_status': plan['status'], 'fulfillment_event_refs': []},
                                'source_story_time': None, 'target_time': plan['target_time'], 'valid_time': None,
                                'subject_ids': plan['subject_ids'], 'safe_summary': summary,
                                'evidence_refs': [{'kind': 'ARTIFACT', 'artifact_ref': plan['plan_ref']}]})
        elif plane == 'BELIEF':
            for cognition in snapshot['cognitions']:
                if cognition['actor_id'] != cut['actor_id'] or point_key(cognition['acquired_at']) > point_key(cut['known_at']):
                    continue
                if cognition.get('subject_id') not in subjects and cut['actor_id'] not in subjects:
                    continue
                entry_kind = 'INTENT' if cognition['record_kind'] == 'PRIVATE_DECISION' else 'OBSERVATION'
                if entry_kind not in query['entry_kinds']:
                    continue
                source_time = {'kind': 'EXACT', 'at': cognition['acquired_at']}
                if not self._time_matches(source_time, None, request['subject_time']):
                    continue
                allowed = cognition['evidence_refs']
                event_sources = [e for e in allowed if e['kind'] == 'EVENT']
                artifact_sources = [e for e in allowed if e['kind'] in {'ARTIFACT', 'RULE'}]
                if event_sources:
                    source = {'kind': 'EVENT', 'event_ref': event_sources[0]['event_ref'], 'acceptance': 'ACCEPTED_IN_BRANCH'}
                elif artifact_sources:
                    source = {'kind': 'COGNITION', 'record_ref': artifact_sources[0]['artifact_ref'],
                              'actor_id': cut['actor_id'], 'cognitive_status': 'ACTIVE'}
                else:
                    continue  # No invented public artifact or evidence reference.
                entries.append({'entry_id': cognition['record_id'], 'entry_kind': entry_kind, 'world_ref': wr,
                                'source': source, 'source_story_time': source_time, 'target_time': None, 'valid_time': None,
                                'subject_ids': [cognition.get('subject_id', cut['actor_id'])],
                                'safe_summary': cognition['statement'], 'evidence_refs': deepcopy(allowed)})
        else:
            if 'FACT' in query['entry_kinds']:
                for event in packet['events']:
                    if not subjects.intersection(event['participants']):
                        continue
                    if query.get('time_axis') == 'TARGET':
                        continue
                    if not self._time_matches(event['occurrence_time'], None, request['subject_time']):
                        continue
                    reference = {key: event[key] for key in ('occurrence_id', 'event_version_id')}
                    entries.append({'entry_id': event['occurrence_id'], 'entry_kind': 'FACT', 'world_ref': wr,
                                    'source': {'kind': 'EVENT', 'event_ref': reference, 'acceptance': 'ACCEPTED_IN_BRANCH'},
                                    'source_story_time': event['occurrence_time'], 'target_time': None, 'valid_time': None,
                                    'subject_ids': event['participants'] or sorted(subjects),
                                    'safe_summary': '已接受事件；效果类型：' + ','.join(sorted({e['kind'] for e in event['effects']})) if event['effects'] else '已接受无状态效果的事件',
                                    'evidence_refs': [{'kind': 'EVENT', 'world_ref': wr, 'event_ref': reference}]})
            if 'STATE_ASSERTION' in query['entry_kinds']:
                for relation in snapshot['relations']:
                    if relation['subject_id'] not in subjects or relation['plane'] != 'WORLD' or relation['origin']['kind'] != 'RECORD':
                        continue
                    if not self._time_matches(None, relation['valid_time'], request['subject_time']):
                        continue
                    origin = relation['origin']
                    entries.append({'entry_id': relation['relation_id'], 'entry_kind': 'STATE_ASSERTION', 'world_ref': wr,
                                    'source': {**origin, 'record_kind': 'ASSERTION', 'acceptance': 'ACCEPTED_IN_BRANCH'},
                                    'source_story_time': None, 'target_time': None, 'valid_time': relation['valid_time'],
                                    'subject_ids': [relation['subject_id']],
                                    'safe_summary': relation['predicate'] + ':' + canonical_bytes(relation['object_value']).decode(),
                                    'evidence_refs': [{'kind': 'ARTIFACT', 'artifact_ref': origin['payload_ref']}]})
        def key(entry):
            time = entry['target_time'] if plane == 'PLAN' else entry['source_story_time']
            at = None if time is None else time.get('at', time.get('earliest'))
            return (0, point_key(at), entry['entry_id']) if at is not None else (1, (0, 0), entry['entry_id'])
        entries.sort(key=key)
        page = entries[offset:offset + request['page_size']]
        next_offset = offset + len(page)
        cursor = self._next_cursor(packet['principal'], request, wr, next_offset) if next_offset < len(entries) else None
        coverage = 'COMPLETE' if plane == 'PLAN' or (plane != 'BELIEF' and all(self._covered(packet, s, request['subject_time']) for s in subjects)) else 'UNDECLARED' if plane == 'BELIEF' else 'PARTIAL'
        if cursor:
            coverage = 'PARTIAL'
        for entry in page:
            validate_contract('TemporalEntry', entry, '2.1')
        if query['kind'] == 'PLAN_DEPENDENTS':
            return {'kind': 'PLAN_DEPENDENTS', 'plans': page, 'coverage': coverage, 'next_cursor': cursor}
        return {'kind': 'TIMELINE', 'entries': page, 'coverage': coverage,
                'unresolved_ranges': [request['subject_time']] if coverage == 'PARTIAL' and plane != 'BELIEF' else [], 'next_cursor': cursor}

    def _graph(self, packet, request, offset):
        _require(request['subject_time']['kind'] == 'POINT', 'INVALID_ARGUMENT', 'P1 graph paths require one exact story point')
        _require(request['knowledge_cut']['plane'] in {'WORLD', 'SIMULATION'}, 'VIEW_DENIED', 'Graph view is unavailable for this knowledge plane')
        query = request['query']; snapshot = packet['revision']['snapshot']; at = request['subject_time']['at']
        predicates = {ALIASES.get(p, p) for p in query['predicates']}
        _require(predicates <= PREDICATES.keys(), 'INVALID_ARGUMENT', 'Unknown graph predicate')
        relations = snapshot['relations']
        _require(len(relations) <= self.max_scan, 'CAPACITY_LIMIT', 'Graph scan budget exceeded')
        selected = [r for r in relations if r['plane'] == 'WORLD' and r['predicate'] in predicates
                    and r['object_value']['kind'] == 'ENTITY' and contains(r['valid_time'], at)]
        adjacency = {}
        for rel in selected:
            adjacency.setdefault(rel['subject_id'], []).append(rel)
        queue = deque((s, 0) for s in query['seed_ids']); visited = set(query['seed_ids']); edge_ids = set(); edges = []
        truncated = False; evaluated = True
        while queue:
            node, depth = queue.popleft()
            evaluated &= self._covered(packet, node, request['subject_time'])
            if depth == query['max_hops']:
                if adjacency.get(node):
                    truncated = True
                continue
            for rel in sorted(adjacency.get(node, []), key=lambda r: r['relation_id']):
                if rel['relation_id'] not in edge_ids:
                    if len(edges) >= query['max_edges']:
                        truncated = True; break
                    edges.append(rel); edge_ids.add(rel['relation_id'])
                target = rel['object_value']['entity_id']
                if target not in visited:
                    visited.add(target); queue.append((target, depth + 1))
            if len(edges) >= query['max_edges'] and queue:
                truncated = True; break
        if request['read_contract']['process_policy'] == 'REQUIRE_EVALUATED' and not evaluated:
            raise DomainError('PROCESS_NOT_EVALUATED', 'Graph scope is not evaluated in this revision', True, 'WAIT')
        # With incomplete process coverage return visible candidates, never a
        # proof that there is no other edge/path.
        page = edges[offset:offset + request['page_size']]
        next_offset = offset + len(page)
        cursor = self._next_cursor(packet['principal'], request, packet['read_cut']['world_ref'], next_offset) if next_offset < len(edges) else None
        return {'kind': 'NEIGHBORHOOD', 'relations': [self._public_relation(r, packet) for r in page],
                'coverage': 'COMPLETE' if evaluated and not truncated and cursor is None else 'PARTIAL',
                'truncated': truncated or cursor is not None, 'next_cursor': cursor}

    def _execute(self, packet, request, offset=0):
        self._time_check(packet, request['subject_time'])
        operation = request['query']['kind']; plane = request['knowledge_cut']['plane']
        check_profile(packet['grant'], plane, operation=operation)
        _require(not (operation == 'PLAN_DEPENDENTS' and plane != 'PLAN'), 'VIEW_DENIED', 'Plan dependencies require a plan view')
        if plane == 'PLAN' and operation == 'TIMELINE':
            _require(request['query']['entry_kinds'] == ['PLAN'], 'INVALID_ARGUMENT', 'Plan timeline must request only PLAN entries')
        if request['read_contract']['process_policy'] == 'WAIT_EXISTING_ADVANCE':
            raise DomainError('CONSISTENCY_UNAVAILABLE', 'P1 has no authorized background advance job for this wait policy')
        handle = self._handle(packet, request)
        if operation == 'STATE':
            _require(request['read_contract']['index_policy'] == 'AUTHORITATIVE', 'INVALID_ARGUMENT', 'Exact state cannot use stale search')
            return self._state(packet, request, handle), 'CURRENT', handle
        if request['read_contract']['index_policy'] == 'ALLOW_STALE_SEARCH':
            name = 'timeline' if operation == 'TIMELINE' else 'graph'
            watermark = next((i for i in packet['indexes'] if i['index_name'] == name), None)
            if watermark is not None and watermark['world_revision_id'] != packet['read_cut']['world_ref']['world_revision_id']:
                # No old row is relabelled as current authority. Clients asking
                # for derived-only candidates receive an explicit partial miss
                # and can retry with AUTHORITATIVE for a complete source read.
                payload = ({'kind': 'TIMELINE', 'entries': [], 'coverage': 'PARTIAL',
                            'unresolved_ranges': [request['subject_time']], 'next_cursor': None}
                           if operation == 'TIMELINE' else
                           {'kind': 'NEIGHBORHOOD', 'relations': [], 'coverage': 'PARTIAL', 'truncated': True, 'next_cursor': None})
                return payload, 'STALE', handle
        payload = self._graph(packet, request, offset) if operation == 'NEIGHBORHOOD' else self._timeline(packet, request, handle, offset)
        return payload, 'CURRENT', handle

    def query(self, principal, request):
        request = validate_contract('UnifiedQueryRequest', request, '2.1')
        cursor = self._cursor(principal, request)
        packet = self._sample(principal, request, cursor=cursor); packet['principal'] = principal
        key = fingerprint({'request': request, 'world_ref': packet['read_cut']['world_ref'],
                           'grant_id': principal.grant_id, 'epoch': packet['grant']['epoch'],
                           'policy': packet['read_cut']['policy_version'], 'interpretation': packet['read_cut']['interpretation_ref'],
                           'indexes': ([{k: row[k] for k in ('index_name', 'world_revision_id', 'generation')} for row in packet['indexes']]
                                       if request['query']['kind'] != 'STATE' and request['knowledge_cut']['plane'] != 'BELIEF' else [])})
        with self._cache_lock:
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key); self.cache_hits += 1
            else:
                self.cache_misses += 1
        if cached is None:
            payload, freshness, handle = self._execute(packet, request, cursor['offset'] if cursor else 0)
            cached = (payload, freshness, handle)
            with self._cache_lock:
                self._cache[key] = deepcopy(cached)
                while len(self._cache) > self.cache_size:
                    self._cache.popitem(last=False)
        payload, freshness, handle = deepcopy(cached)
        result = {'schema_version': '2.1', 'status': 'READY', 'request_id': _id(), 'read_cut': packet['read_cut'],
                  'payload': payload, 'freshness': freshness, 'dependency_handle': handle, 'consistency_status': 'SATISFIED'}
        validate_contract('UnifiedQueryResult', result, '2.1')
        _require(len(canonical_bytes(result)) <= self.max_response_bytes, 'CAPACITY_LIMIT', 'Query response budget exceeded')
        self._delivery_check(principal, request, packet, [({**request, 'dependency_handle': handle}, payload)])
        return result

    def batch(self, principal, request):
        request = validate_contract('QueryBatchRequest', request, '2.1')
        _require(len({item['item_id'] for item in request['items']}) == len(request['items']),
                 'INVALID_ARGUMENT', 'Batch item identities must be unique')
        cost = sum(len(item['query'].get('subject_ids', item['query'].get('seed_ids', []))) *
                   len(item['query'].get('predicates', [None])) for item in request['items'])
        _require(cost <= 4000, 'CAPACITY_LIMIT', 'Aggregate batch query budget exceeded')
        packet = self._sample(principal, request); packet['principal'] = principal
        results, observations = [], []
        for item in request['items']:
            child = {key: request[key] for key in ('schema_version', 'task_id', 'generation', 'read_contract', 'knowledge_cut')}
            child.update({key: item[key] for key in ('subject_time', 'query', 'page_size')}, cursor=None)
            try:
                validate_contract('UnifiedQueryRequest', child, '2.1')
                payload, freshness, handle = self._execute(packet, child)
                observations.append(({**child, 'dependency_handle': handle}, payload))
                results.append({'item_id': item['item_id'], 'status': 'SUCCESS', 'payload': payload,
                                'freshness': freshness, 'dependency_handle': handle})
            except DomainError as exc:
                results.append({'item_id': item['item_id'], 'status': 'ERROR', 'error': exc.as_dict(_id())})
        result = {'schema_version': '2.1', 'status': 'READY', 'request_id': _id(), 'read_cut': packet['read_cut'],
                  'items': results, 'all_items_succeeded': all(item['status'] == 'SUCCESS' for item in results)}
        validate_contract('QueryBatchResult', result, '2.1')
        _require(len(canonical_bytes(result)) <= self.max_response_bytes, 'CAPACITY_LIMIT', 'Batch response budget exceeded')
        self._delivery_check(principal, request, packet, observations)
        return result

    def context_compile(self, principal, request):
        request = validate_contract('ContextCompileRequest', request, '2.1')
        _require(request['read_contract']['index_policy'] == 'AUTHORITATIVE', 'INVALID_ARGUMENT', 'Context compilation requires authoritative input')
        _require(request['subject_time']['kind'] == 'POINT', 'INVALID_ARGUMENT', 'P1 context compilation pins one story point')
        command_hash = fingerprint(request)
        with self.kernel.db.uow(write=False) as repo:
            task, grant, _ = self._task(repo, principal, request['task_id'], request['generation'], 'COMPILE')
            previous = repo.one('receipts', {'work_id': task['work_id'], 'principal_id': principal.principal_id,
                                           'command_type': 'CONTEXT_COMPILE', 'idempotency_key': request['idempotency_key']})
            if previous:
                _require(previous['request_hash'] == command_hash, 'IDEMPOTENCY_CONFLICT', 'Idempotency key was used for different compilation content')
                self.kernel.authorize(repo, principal, task['work_id'], 'READ', previous['branch_id'])
                return deepcopy(previous['payload'])
        packet = self._sample(principal, request, compile_context=True); packet['principal'] = principal
        _require(request['subject_time']['at'] == packet['spec']['story_time'], 'TASK_REBIND_REQUIRED', 'Context story point differs from immutable task input')
        _require(packet['task']['state'] in {'RUNNING', 'RESULT_STORED', 'VALIDATING', 'WAITING_REVIEW'},
                 'TASK_NOT_ACCEPTABLE', 'Claim the task before binding a context')
        _require(request['recipe_id'] != 'ACTOR' or request['knowledge_cut']['plane'] == 'BELIEF',
                 'VIEW_DENIED', 'Actor recipes require an actor knowledge cut')
        manifest = packet['manifest']; cut = request['knowledge_cut']; at = request['subject_time']['at']
        query = ({'kind': 'TIMELINE', 'subject_ids': manifest['subject_ids'], 'entry_kinds': ['PLAN'],
                  'time_axis': 'TARGET', 'include_inactive': True} if cut['plane'] == 'PLAN' else
                 {'kind': 'STATE', 'subject_ids': manifest['subject_ids'], 'predicates': manifest['predicates']})
        child = {key: request[key] for key in ('schema_version', 'task_id', 'generation', 'read_contract', 'knowledge_cut', 'subject_time')}
        child.update(query=query, page_size=500, cursor=None)
        payload, _, _ = self._execute(packet, child)
        facts, permitted, limitations = [], [], []
        if payload['kind'] == 'STATE':
            for item in payload['state_items']:
                answer = item['answer']
                if answer['knowledge_status'] == 'KNOWN':
                    statement = canonical_bytes({'subject_id': item['subject_id'], 'predicate': item['predicate'],
                                                 'value_status': answer['value_status'], 'value': answer['data']}).decode()
                    facts.append({'statement': statement, 'status': 'BELIEF' if cut['plane'] == 'BELIEF' else 'FACT',
                                  'evidence_refs': answer['evidence_refs']})
                    permitted.extend(answer['evidence_refs'])
                else:
                    limitations.append('Requested material is not available as a determined value at this knowledge cut.')
        else:
            # Plan material stays in a PLAN section. Core FACT entries are not
            # used to assert that a future outcome has happened.
            for entry in payload['entries']:
                permitted.extend(entry['evidence_refs'])
            limitations.append('Plans are conditional editorial material; they do not apply world effects.')
        permitted = _dedupe(permitted)
        wr = packet['read_cut']['world_ref']; revision = packet['revision']
        subjects = set(manifest['subject_ids']); scopes = {'RULESET:world', 'SCOPE_REGISTRY:world'}
        for subject in subjects:
            scopes.add('ENTITY:' + subject)
            entity = revision['snapshot']['entities'].get(subject)
            scopes.add('PROCESS_COVERAGE:' + (entity.get('scope_id', subject) if entity else subject))
        for predicate in manifest['predicates']:
            scopes.add('PREDICATE_RANGE:' + ALIASES.get(predicate, predicate))
        if cut['plane'] == 'BELIEF':
            scopes.add('ENTITY:' + cut['actor_id'])
        queries = []
        # Every subject dependency refers to this same fixed-cut payload. Its
        # canonical digest is identical, so compute it once for the bundle.
        payload_hash = fingerprint(payload)
        for subject in sorted(subjects):
            scope = 'ENTITY:' + subject
            queries.append({'query_id': str(uuid5(UUID(request['task_id']), subject)),
                            'kind': 'POSITIVE' if subject in revision['snapshot']['entities'] else 'NEGATIVE',
                            'scope_key': scope, 'predicate_sha256': fingerprint({'subject_id': subject, 'predicates': manifest['predicates']}),
                            'result_sha256': payload_hash})
        dependency = {'world_ref': wr, 'policy_version': packet['read_cut']['policy_version'], 'rule_version': 'nf-world-rules-1',
                      'scope_versions': [{'scope_key': scope, 'version': revision['scope_versions'].get(scope, 0)} for scope in sorted(scopes)],
                      'artifact_refs': _dedupe(packet['read_cut']['source_manifest_refs'] + [revision['interpretation_ref']]),
                      'queries': queries, 'unresolved_scopes': []}
        validate_contract('DependencyManifest', dependency)
        dependency_hash = fingerprint(dependency); context_id = _id(); dependency_handle = _id()
        bundle = {'schema_version': '2.0', 'context_bundle_id': context_id, 'task_id': request['task_id'],
                  'generation': request['generation'], 'world_ref': wr, 'story_time': at,
                  'role_id': packet['grant']['role_id'], 'actor_id': cut.get('actor_id'),
                  'policy_version': packet['read_cut']['policy_version'], 'dependency_fingerprint': dependency_hash,
                  'dependency_handle': dependency_handle, 'permitted_evidence_refs': permitted,
                  'facts': facts, 'limitations': list(dict.fromkeys(limitations))}
        validate_contract('ContextBundle', bundle)
        section_kind = 'ACTOR_KNOWLEDGE' if cut['plane'] == 'BELIEF' else 'PLAN' if cut['plane'] == 'PLAN' else 'WORLD'
        sections = [
            {'section_kind': 'TASK', 'content': f"Task {request['task_id']} generation {request['generation']}; fixed story point {canonical_bytes(at).decode()}.", 'source_refs': [], 'required': True},
            {'section_kind': 'HARD_CONSTRAINTS', 'content': 'Use only the supplied fixed-cut material. Unknown values remain unknown. Beliefs and plans are not objective world facts. State changes require the formal acceptance service.', 'source_refs': [], 'required': True},
            {'section_kind': section_kind, 'content': canonical_bytes(facts if payload['kind'] == 'STATE' else payload['entries']).decode(),
             'source_refs': permitted, 'required': True},
        ]
        if limitations:
            sections.append({'section_kind': 'UNRESOLVED', 'content': '\n'.join(dict.fromkeys(limitations)), 'source_refs': [], 'required': True})
        # A provider-independent conservative bound. This is neither a claimed
        # provider tokenizer nor a measured external model bill.
        estimated = sum(len(section['content'].encode('utf-8')) for section in sections)
        budget = min(request['token_budget'], packet['spec']['budget']['token_budget'])
        _require(estimated <= budget, 'CONTEXT_INCOMPLETE', 'Required context exceeds the authorized budget; split the task or request a larger budget')
        recipe = {'recipe_id': request['recipe_id'], 'compiler_version': 'nf-context-1',
                  'source_contract': '2.1', 'required_sections': [s['section_kind'] for s in sections]}
        recipe_ref = self.kernel._blob(recipe)
        compiled = {'schema_version': '2.1', 'bundle': bundle, 'read_cut': packet['read_cut'], 'compiler_version': 'nf-context-1',
                    'recipe_revision_ref': recipe_ref, 'input_manifest_ref': request['input_manifest_ref'],
                    'auxiliary_knowledge_cuts': [], 'sections': sections, 'hard_requirements_status': 'COMPLETE_FOR_RECIPE',
                    'tokenizer_version': 'utf8-byte-upper-bound-1', 'estimated_tokens': estimated, 'omitted_optional_scopes': []}
        validate_contract('CompiledContext', compiled, '2.1')
        compiled_ref = self.kernel._blob(compiled)
        result = {'schema_version': '2.1', 'status': 'READY', 'compiled': compiled, 'compiled_artifact_ref': compiled_ref}
        validate_contract('ContextCompileResult', result, '2.1')
        _require(len(canonical_bytes(result)) <= self.max_response_bytes, 'CAPACITY_LIMIT', 'Compiled context exceeds response budget')
        with self.kernel.objects.lock(), self.kernel.db.uow() as repo:
            task, grant, spec = self._task(repo, principal, request['task_id'], request['generation'], 'COMPILE')
            _require(grant['epoch'] == packet['grant']['epoch'], 'VIEW_DENIED', 'Authorization changed during compilation')
            # Locks and fencing are repeated in the same transaction that adds
            # the immutable bundle and binds it to this exact task generation.
            branch = repo.one('branches', {'branch_id': wr['branch_id']}, lock='share')
            task = self.kernel.tasks.guard_task(repo.conn, request['task_id'], request['generation'], require_context=False)
            previous = repo.one('receipts', {'work_id': wr['work_id'], 'principal_id': principal.principal_id,
                                           'command_type': 'CONTEXT_COMPILE', 'idempotency_key': request['idempotency_key']})
            if previous:
                _require(previous['request_hash'] == command_hash, 'IDEMPOTENCY_CONFLICT', 'Idempotency key was used for different compilation content')
                return deepcopy(previous['payload'])
            _require(task['context_bundle_id'] is None, 'TASK_REBIND_REQUIRED', 'This generation already has an immutable context')
            _require(spec['world_ref'] == wr, 'TASK_REBIND_REQUIRED', 'Task input changed during compilation')
            self.kernel._register_artifact(repo, recipe_ref, wr['work_id'], recipe, 'CONTEXT_RECIPE', principal.principal_id)
            self.kernel._register_artifact(repo, compiled_ref, wr['work_id'], compiled, 'COMPILED_CONTEXT', principal.principal_id)
            repo.add('contexts', {'context_bundle_id': context_id, 'work_id': wr['work_id'], 'task_id': request['task_id'],
                                  'generation': request['generation'], 'principal_id': principal.principal_id, 'grant_id': principal.grant_id,
                                  'payload': bundle, 'dependency_manifest': dependency, 'artifact_ref': compiled_ref,
                                  'read_cut': packet['read_cut'], 'dependency_fingerprint': dependency_hash})
            self.kernel.tasks.bind_context_in_transaction(repo.conn, request['task_id'], request['generation'], None,
                context_bundle_id=context_id, dependency_fingerprint=dependency_hash, world_ref=wr)
            record_query(repo, packet, child, payload, 'CONTEXT', branch['head_revision_id'], context_ref=compiled_ref)
            repo.add_many('dependencies', [
                {'dependency_id': _id(), 'work_id': wr['work_id'], 'branch_id': wr['branch_id'],
                 'owner_type': 'context', 'owner_id': context_id, 'scope_key': scope,
                 'observed_version': revision['scope_versions'].get(scope, 0), 'dependency_kind': 'COMPUTED'}
                for scope in sorted(scopes)
            ])
            repo.add('receipts', {'work_id': wr['work_id'], 'principal_id': principal.principal_id, 'command_type': 'CONTEXT_COMPILE',
                                 'idempotency_key': request['idempotency_key'], 'request_hash': command_hash,
                                 'branch_id': wr['branch_id'], 'proposal_id': None, 'payload': result})
            repo.outbox('context.compiled', 'task', request['task_id'], {'task_id': request['task_id'], 'generation': request['generation'],
                        'context_bundle_id': context_id}, f'context:{request["task_id"]}:{request["generation"]}')
        return result
