"""Versioned read receipts and an indexed join against committed effects.

This service holds neither world facts nor model-generated truth. A case is
advice about a particular immutable task input. It cannot change that input or
authorize its acceptance. All functions use the caller's PostgreSQL transaction.
"""
import json
from collections import defaultdict
from uuid import UUID, uuid5

from sqlalchemy import text

from novel_factory.domain.canonical import fingerprint
from novel_factory.domain.errors import DomainError
from novel_factory.domain.semantic_keys import change_keys, proposition_key, slot
from novel_factory.domain.propositions import decode_proposition
from novel_factory.domain.temporal import point_key
from novel_factory.domain.world import ALIASES

VERSION = 'vcdc-1'


def _wr(row):
    return {key: str(row[key]) for key in ('work_id', 'branch_id', 'world_revision_id')}


def _unique(items):
    return list({fingerprint(item): item for item in items}.values())


def record_change(repo, revision, proposal=None):
    """Runs inside acceptance, after revision insertion and before HEAD CAS."""
    events = proposal['payload']['event_candidates'] if proposal else []
    repair = bool(proposal and proposal['template_id'] == 'REPAIR_EVENT')
    keys = change_keys(events, proposal['footprint'], repair) if proposal else ['interpretation']
    details = {
        'evidence_refs': [{'kind': 'EVENT', 'world_ref': _wr(revision),
                           'event_ref': {k: event[k] for k in ('occurrence_id', 'event_version_id')}}
                          for event in events],
        'story_points': [e['occurrence_time'].get('at') for e in events],
        'repair': repair,
    }
    repo.conn.execute(text("""
        INSERT INTO nf_semantic_changes
        (world_revision_id,work_id,branch_id,branch_seq,source_task_id,keys,details)
        VALUES (:world_revision_id,:work_id,:branch_id,:seq,:source,CAST(:keys AS text[]),CAST(:proof AS jsonb))
    """), {**_wr(revision), 'seq': revision['branch_seq'], 'source': proposal and proposal['task_id'],
           'keys': [*keys, 'uncertain'], 'proof': json.dumps(details)})


def record_observation(repo, task_id, generation, revision, keys, details, *, head_id=None):
    """Caller fences its task and branch. Late old-cut reads discover prior changes.

    Both registration and world acceptance lock the branch before locking a task.
    An ordinary read acquires SHARE, allowing unrelated readers concurrently.
    """
    wr = _wr(revision)
    keys = sorted(set(keys) | {'history', 'interpretation'})
    oid = str(uuid5(UUID(task_id), fingerprint([generation, wr, keys, details])))
    inserted = repo.conn.scalar(text("""
        INSERT INTO nf_semantic_reads
        (observation_id,task_id,generation,work_id,branch_id,world_revision_id,branch_seq,keys,details)
        VALUES (:id,:task,:gen,:work_id,:branch_id,:world_revision_id,:seq,CAST(:keys AS text[]),CAST(:proof AS jsonb))
        ON CONFLICT(observation_id) DO NOTHING RETURNING observation_id
    """), {**wr, 'id': oid, 'task': task_id, 'gen': generation, 'seq': revision['branch_seq'],
           'keys': keys, 'proof': json.dumps(details)})
    if inserted and head_id != wr['world_revision_id']:
        _detect(repo, 'r.observation_id=:id', {'id': oid})
    return oid


def record_query(repo, packet, request, payload, origin, head_id, *, context_ref=None):
    revision, cut = packet['revision'], request['knowledge_cut']
    keys, proofs, uncertain_keys = set(), [], set()
    query = request['query']
    if query['kind'] == 'STATE':
        for item in payload['state_items']:
            answer, subject = item['answer'], item['subject_id']
            # Compiler emits only known facts. Its undifferentiated omission
            # note exposes no predicate value. A direct query exposes all
            # results including an empty or unresolved predicate range.
            if origin == 'CONTEXT' and answer['knowledge_status'] != 'KNOWN':
                continue
            key = slot(subject, item['predicate'])
            if cut['plane'] == 'BELIEF':
                key = 'belief:' + cut['actor_id'] + ':' + key
            keys.add(key)
            if item['predicate'] == 'message_content_ref' and cut['plane'] in {'WORLD', 'SIMULATION'}:
                # Reading a letter alone proves neither that the author relies
                # on its authenticity nor that the interpretation is irrelevant.
                uncertain_keys.update(slot(subject, p) for p in ('message_authentic', 'attributed_reading_ref'))
            if cut['plane'] != 'BELIEF' and origin == 'QUERY' and answer['evaluation_status'] == 'PENDING':
                entity = revision['snapshot']['entities'].get(subject, {})
                keys.add('guard:PROCESS_COVERAGE:' + entity.get('scope_id', subject))
            proofs.extend(answer['evidence_refs'])
    elif cut['plane'] == 'PLAN':
        for plan in packet['plans']['plans']:
            if set(query['subject_ids']).intersection(plan['subject_ids'] + plan['requires_active']):
                keys.update(slot(subject, 'lifecycle') for subject in plan['requires_active'])
        proofs.extend({'kind': 'ARTIFACT', 'artifact_ref': ref} for ref in packet['read_cut']['source_manifest_refs'])
    elif cut['plane'] == 'BELIEF':
        keys.add('cognition:' + cut['actor_id'])
    elif query['kind'] == 'NEIGHBORHOOD':
        # Traversal frontier is not fully returned on a partial page. Index a
        # predicate-wide phantom range instead of guessing the unvisited nodes.
        keys.update('range:' + ALIASES.get(p, p) for p in query['predicates'])
        keys.update('guard:PROCESS_COVERAGE:' + s for s in query['seed_ids'])
    else:
        keys.update('timeline:' + s for s in query['subject_ids'])
        keys.update('guard:PROCESS_COVERAGE:' + s for s in query['subject_ids'])
    details = {'origin': origin, 'knowledge_cut': cut, 'story_time': request['subject_time'],
               'query': query, 'dependency_handle': request.get('dependency_handle'), 'interpretation_ref': revision['interpretation_ref'],
               'role_id': packet['grant']['role_id'], 'agent_actor_id': packet['grant']['actor_id'],
               'input_artifacts': packet['spec']['input_artifacts'], 'evidence_refs': _unique(proofs),
               'result_sha256': fingerprint(payload), 'context_ref': context_ref}
    if uncertain_keys:
        details['uncertain_keys'] = sorted(uncertain_keys - keys)
    message_predicates = {'message_content_ref', 'attributed_reading_ref', 'message_authentic'}
    if (query['kind'] == 'STATE' and cut['plane'] in {'WORLD', 'SIMULATION'}
            and point_key(request['subject_time']['at']) < point_key(packet['spec']['story_time'])
            and not set(query['predicates']).intersection(message_predicates)
            and all(item['answer']['evaluation_status'] == 'EVALUATED' for item in payload['state_items'])):
        # A closed historical point stays true when a normal subsequent event
        # changes current state. An explicit continuing assumption has its own
        # observation and is deliberately not restricted to this point.
        details['historical_through'] = request['subject_time']['at']
    return record_observation(repo, request['task_id'], request['generation'], revision, keys | uncertain_keys,
                              details, head_id=head_id)


def record_proposal(repo, revision, proposal_id, task_id, generation, footprint, proposal_ref, at, head_id):
    writes = ['guard:' + key for key in footprint['writes']]
    keys = writes + ['guard:' + key for key in footprint['reads']]
    details = {'origin': 'PROPOSAL', 'proposal_id': proposal_id, 'evidence_refs': [proposal_ref],
               'write_effects': writes, 'story_time': at}
    return record_observation(repo, task_id, generation, revision, keys, details, head_id=head_id)


def record_plan(repo, revision, task, binding, plan, head_id):
    ids = set(binding['node_ids'])
    while True:
        predecessors = {edge['source_node_id'] for edge in plan['dependencies']
                        if edge['target_node_id'] in ids and edge['kind'] == 'HARD_PREREQUISITE'}
        if predecessors <= ids:
            break
        ids.update(predecessors)
    keys = set()
    for node in plan['nodes']:
        if node['node_id'] not in ids:
            continue
        for condition in node['hard_preconditions']:
            kind = condition['kind']
            if kind == 'RESOURCE_AT_LEAST':
                keys.add(slot(condition['account_id'], 'resource:' + condition['resource_id'] + ':' + condition['unit']))
            elif kind in {'ENTITY_ACTIVE', 'STATE_EQUALS'}:
                keys.add(slot(condition['subject_id'], condition.get('predicate', 'lifecycle')))
    return record_observation(repo, task['task_id'], task['generation'], revision, keys,
                              {'origin': 'PLAN_BINDING', 'plan_node_refs': sorted(ids),
                               'plan_ref': binding['plan_ref'], 'evidence_refs': [binding['plan_ref']]}, head_id=head_id)


_MATCH = """
    FROM nf_semantic_changes c JOIN nf_semantic_reads r
      ON r.branch_id=c.branch_id AND r.work_id=c.work_id AND r.branch_seq<c.branch_seq
      AND r.keys && c.keys AND c.source_task_id IS DISTINCT FROM r.task_id
    JOIN nf_tasks t ON t.task_id=r.task_id AND t.generation=r.generation
    WHERE t.state NOT IN ('CANCELLED','FAILED')
      AND NOT ((coalesce(r.details->'knowledge_cut'->>'plane','')='BELIEF' OR r.details ? 'historical_through')
        AND NOT ('history'=ANY(c.keys) OR 'interpretation'=ANY(c.keys))
        AND jsonb_array_length(c.details->'story_points')>0
        AND NOT EXISTS (SELECT 1 FROM jsonb_array_elements(c.details->'story_points') AS e(p)
             WHERE e.p='null'::jsonb OR
              ((e.p->>'tick')::bigint,(e.p->>'phase')::bigint) <=
              ((coalesce(r.details->'historical_through',r.details->'knowledge_cut'->'known_at')->>'tick')::bigint,
               (coalesce(r.details->'historical_through',r.details->'knowledge_cut'->'known_at')->>'phase')::bigint)))
"""


def check_task_dependencies(repo, task_id, generation):
    """Final short-txn barrier. No case worker, broker, or fresh cache required."""
    changed = repo.conn.scalar(text("SELECT EXISTS(SELECT 1 " + _MATCH +
        " AND r.task_id=:task AND r.generation=:gen)"), {'task': task_id, 'gen': generation})
    if changed:
        raise DomainError('DEPENDENCY_CONFLICT', 'This task input requires revalidation.')
    # An authorized read on another branch can support comparison, but it
    # cannot be silently transported into a formal effect on this branch.
    divergent = repo.conn.scalar(text("""
        SELECT EXISTS(SELECT FROM nf_semantic_reads r JOIN nf_tasks t USING(task_id)
                       WHERE r.task_id=:task AND r.generation=:gen AND r.branch_id<>t.branch_id)
    """), {'task': task_id, 'gen': generation})
    if not divergent:
        # Existing input generations may contain legacy queries with no
        # recorded footprint. A newly submitted proposal cannot certify those
        # historical reads. Require the original HEAD for such a context.
        divergent = repo.conn.scalar(text("""
            SELECT EXISTS(SELECT FROM nf_contexts ctx JOIN nf_branches b
              ON b.branch_id=CAST(ctx.read_cut->'world_ref'->>'branch_id' AS uuid)
              WHERE ctx.task_id=:task AND ctx.generation=:gen
                AND b.head_revision_id<>CAST(ctx.read_cut->'world_ref'->>'world_revision_id' AS uuid)
                AND NOT EXISTS(SELECT FROM nf_semantic_reads s
                    WHERE s.task_id=ctx.task_id AND s.generation=ctx.generation
                      AND s.details->>'origin'='CONTEXT'))
        """), {'task': task_id, 'gen': generation})
    if divergent:
        raise DomainError('DEPENDENCY_CONFLICT', 'A branch-specific task input requires review.')


def _classification(rows, scopes):
    origins = {r['observation']['origin'] for r in rows}
    writes = {k for r in rows for k in r['observation'].get('write_effects', ())}
    if 'history' in scopes:
        return 'HISTORY_REPAIR_REQUIRED', 'ESCALATE_REVIEW'
    if 'PLAN_BINDING' in origins:
        return 'STALE_PRECONDITION', 'REPLAN_WINDOW'
    if any(k.startswith(('guard:RESOURCE_CONSTRAINT:', 'guard:CONFLICT_GROUP:')) for k in scopes):
        return 'SHARED_CONSTRAINT_CONFLICT', 'JOINT_ADJUDICATION'
    if writes.intersection(scopes):
        return 'JOINT_ADJUDICATION_REQUIRED', 'JOINT_ADJUDICATION'
    if all(scopes_for_row <= set(row['observation'].get('uncertain_keys', []))
           for row in rows if (scopes_for_row := set(row['keys']).intersection(row['read_keys']))):
        return 'SEMANTIC_UNCERTAIN', 'ESCALATE_REVIEW'
    if any(r['observation'].get('uncertain') for r in rows):
        return 'SEMANTIC_UNCERTAIN', 'ESCALATE_REVIEW'
    if rows[0]['state'] in {'ACCEPTED', 'RESULT_STORED', 'VALIDATING', 'WAITING_REVIEW'}:
        return 'STALE_PRECONDITION', 'RECOMPUTE_DEPENDENTS'
    return 'STALE_PRECONDITION', 'REVALIDATE'


def _detect(repo, condition, params):
    from .audit_log import LogService

    rows = repo.conn.execute(text("""
        SELECT c.*,r.observation_id,r.details AS observation,r.keys AS read_keys,r.task_id,r.generation,
               r.world_revision_id AS read_revision,t.state
        """ + _MATCH + " AND " + condition +
        """ AND NOT EXISTS (SELECT FROM nf_receipts a JOIN nf_proposals p ON a.proposal_id=p.proposal_id
                            WHERE a.command_type='ACCEPT' AND p.task_id=r.task_id AND p.generation=r.generation)
            ORDER BY r.task_id,r.generation,c.world_revision_id,r.observation_id"""), params).mappings()
    groups = defaultdict(list)
    for row in rows:
        groups[(str(row['task_id']), row['generation'], str(row['world_revision_id']))].append(row)
    inserted = []
    for (task, generation, rid), matches in groups.items():
        first = matches[0]
        wr = _wr(first)
        case = str(uuid5(UUID(task), f'{VERSION}:{generation}:{rid}'))
        scopes = sorted({key for row in matches for key in set(row['keys']).intersection(row['read_keys'])})
        category, strategy = _classification(matches, scopes)
        src = str(first['source_task_id']) if first['source_task_id'] else None
        workflows = repo.conn.scalars(text("""
            SELECT DISTINCT coalesce(trace.trace_id,t.task_id) FROM nf_tasks t
            LEFT JOIN nf_trace_tasks trace USING(task_id) WHERE t.task_id=ANY(CAST(:ids AS uuid[]))
        """), {'ids': [x for x in (task, src) if x]}).all()
        details = {
            'affected_task_ids': [task], 'source_workflow_ids': sorted(map(str, workflows)),
            'source_world_refs': _unique([wr, *[{**wr, 'world_revision_id': str(r['read_revision'])} for r in matches]]),
            'dependency_manifest_refs': [str(r['observation_id']) for r in matches],
            'evidence_refs': _unique(first['details']['evidence_refs'] +
                                     [e for r in matches for e in r['observation'].get('evidence_refs', [])]),
            'scopes': scopes, 'resolution_strategy': strategy, 'conflict_type': category,
            'story_time_constraints': [*first['details']['story_points'],
                                       *[r['observation']['story_time'] for r in matches if r['observation'].get('story_time')]],
            'decision_version': VERSION, 'detection_method': 'INDEXED_OBSERVED_EFFECT_INTERSECTION',
        }
        made = repo.conn.scalar(text("""
            INSERT INTO nf_conflict_cases
            (case_id,work_id,branch_id,task_id,generation,source_task_id,world_revision_id,conflict_type,strategy,details)
            VALUES (:case,:work_id,:branch_id,:task,:gen,:src,:world_revision_id,:kind,:strategy,CAST(:proof AS jsonb))
            ON CONFLICT DO NOTHING RETURNING case_id
        """), {**wr, 'case': case, 'task': task, 'gen': generation, 'src': src,
               'kind': category, 'strategy': strategy, 'proof': json.dumps(details)})
        if made:
            LogService.conflict(repo.conn, 'detected', case_id=case, receipt_id=case, task_id=task,
                                generation=generation, outcome=category, details=details)
            repo.outbox('semantic.conflict.detected', 'conflict', case,
                        {'case_id': case, 'work_id': wr['work_id'], 'branch_id': wr['branch_id'],
                         'task_id': task, 'generation': generation, 'world_ref': wr}, 'semantic-case:' + case)
            inserted.append(case)
    return inserted


def detect_revision(repo, world_ref):
    return _detect(repo, 'c.world_revision_id=:world_revision_id AND c.branch_id=:branch_id AND c.work_id=:work_id',
                   world_ref)


def add_assumption(kernel, principal, request):
    """Attach a supplemental claim to a successful, already authorized query.

    There is no client-supplied write set, coverage flag, other task ID or
    alternate world/actor in the observation. Ambiguous prose stays tied to
    this receipt, never to a guessed synonym or another person's evidence.
    """
    from novel_factory.contracts.registry import validate_contract
    from .queries import WorldQueryService

    request = validate_contract('SemanticAssumption', request, 'kernel')
    task, gen = request['task_id'], request['generation']
    with kernel.db.uow() as repo:
        service = WorldQueryService(kernel)
        service._task(repo, principal, task, gen)
        receipt = repo.conn.execute(text("""
            SELECT * FROM nf_semantic_reads WHERE task_id=:task AND generation=:gen
            AND details->>'origin'='QUERY' AND details->>'dependency_handle'=:handle
            ORDER BY observation_id LIMIT 1
        """), {'task': task, 'gen': gen, 'handle': request['dependency_handle']}).mappings().first()
        if not receipt:
            raise DomainError('VIEW_DENIED', 'No authorized observation is available.')
        wr = _wr(receipt)
        kernel.authorize(repo, principal, wr['work_id'], 'READ', wr['branch_id'])
        head = repo.one('branches', {'branch_id': wr['branch_id']}, lock='share')
        current = repo.one('tasks', {'task_id': task}, lock='update')
        service._task(repo, principal, task, gen)
        if current['state'] not in {'RUNNING', 'READY'}:
            raise DomainError('TASK_NOT_ACCEPTABLE', 'Task inputs have already been consumed.')
        proof = receipt['details']
        cut = proof['knowledge_cut']
        source = str(receipt['observation_id'])
        statement = request['statement']
        parsed = decode_proposition(statement)
        keys = set(receipt['keys'])
        if parsed:
            key = slot(parsed['subject_id'], parsed['predicate'])
            if cut['plane'] == 'BELIEF':
                key = 'belief:' + cut['actor_id'] + ':' + key
            if key not in keys:
                raise DomainError('VIEW_DENIED', 'Typed claim is outside the authorized query.')
        elif cut['plane'] != 'BELIEF':
            keys.add('uncertain')
        details = {
            'origin': 'ASSUMPTION', 'source_observation_id': source, 'dependency_kind': request['dependency_kind'],
            'knowledge_cut': cut, 'story_time': proof['story_time'], 'statement': statement,
            'proposition_key': proposition_key(statement, source), 'uncertain': parsed is None,
            'evidence_refs': proof['evidence_refs'], 'interpretation_ref': proof['interpretation_ref'],
        }
        observation = record_observation(repo, task, gen, receipt, keys, details, head_id=head['head_revision_id'])
        return {'observation_id': observation, 'proposition_key': details['proposition_key'], 'uncertain': parsed is None}
