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
from novel_factory.domain.propositions import decode_proposition
from novel_factory.domain.semantic_keys import change_keys, proposition_key, slot
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
    if inserted and details.get('base_world_ref', wr)['branch_id'] != wr['branch_id']:
        base = details['base_world_ref']
        case = str(uuid5(UUID(task_id), f'{VERSION}:branch:{generation}:{base["world_revision_id"]}'))
        proof = {'decision_version': VERSION, 'detection_method': 'DIFFERENT_AUTHORIZED_BRANCH',
                 'source_world_refs': [wr, base], 'dependency_manifest_refs': [oid],
                 'affected_task_ids': [task_id], 'story_time_constraints': [details.get('story_time')],
                 'source_workflow_ids': [str(repo.conn.scalar(text(
                    'SELECT trace_id FROM nf_trace_tasks WHERE task_id=:t'), {'t':task_id}) or task_id)],
                 'scopes': ['BRANCH_SELECTION'], 'evidence_refs': [], 'conflict_type':'BRANCH_DIVERGENCE',
                 'resolution_strategy':'CANDIDATE_BRANCH'}
        _emit_case(repo, base, task_id, generation, None, case, proof)
    elif inserted and head_id != wr['world_revision_id']:
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
    else:
        keys.update('timeline:' + s for s in query['subject_ids'])
    details = {'origin': origin, 'knowledge_cut': cut, 'story_time': request['subject_time'],
               'base_world_ref':packet['spec']['world_ref'], 'query': query, 'dependency_handle': request.get('dependency_handle'), 'interpretation_ref': revision['interpretation_ref'],
               'role_id': packet['grant']['role_id'], 'agent_actor_id': packet['grant']['actor_id'],
               'input_artifacts': packet['spec']['input_artifacts'], 'evidence_refs': _unique(proofs),
               'result_sha256': fingerprint(payload), 'context_ref': context_ref}
    if cut['plane'] in {'WORLD', 'SIMULATION'} and query['kind'] in {'TIMELINE', 'NEIGHBORHOOD'}:
        scopes = request.get('coverage_scopes', ())
        keys.update('guard:PROCESS_COVERAGE:' + scope for scope in scopes)
        details['coverage_scopes'] = sorted(scopes)
    if uncertain_keys:
        details['uncertain_keys'] = sorted(uncertain_keys - keys)
    message_predicates = {'message_content_ref', 'attributed_reading_ref', 'message_authentic'}
    summary = packet['grant']['role_id'] == 'evidence_maintainer' and packet['spec']['task_kind'] == 'summary'
    if (query['kind'] == 'STATE' and cut['plane'] in {'WORLD', 'SIMULATION'}
            and (summary or point_key(request['subject_time']['at']) < point_key(packet['spec']['story_time']))
            and not set(query['predicates']).intersection(message_predicates)
            and all(item['answer']['evaluation_status'] == 'EVALUATED' for item in payload['state_items'])):
        # A closed historical point stays true when a normal subsequent event
        # changes current state. An explicit continuing assumption has its own
        # observation and is deliberately not restricted to this point.
        details['historical_through'] = request['subject_time']['at']
    return record_observation(repo, request['task_id'], request['generation'], revision, keys | uncertain_keys,
                              details, head_id=head_id)


def record_proposal(repo, revision, proposal_id, task_id, generation, footprint, proposal_ref, at, head_id, template):
    writes = ['guard:' + key for key in footprint['writes']]
    keys = writes + ['guard:' + key for key in footprint['reads']]
    details = {'origin': 'PROPOSAL', 'proposal_id': proposal_id, 'evidence_refs': [proposal_ref],
               'write_effects': writes, 'story_time': at}
    observation = record_observation(repo, task_id, generation, revision, keys, details, head_id=head_id)
    if at is not None and template != 'JOINT_INTERACTION':
        _coordinate_pending(repo, revision, task_id, generation, at, writes, observation)
    return observation


def _coordinate_pending(repo, revision, task_id, generation, at, writes, observation):
    # Bare control-plane commands carry their own closed adjudication. Enrolled
    # creative candidates may need a joint decision before either is accepted.
    enrolled = repo.conn.scalar(text("""
        SELECT EXISTS(SELECT FROM nf_trace_tasks WHERE task_id=:task)
            OR EXISTS(SELECT FROM nf_supervisor_jobs WHERE task_id=:task)
    """), {'task':task_id})
    contested = sorted(k for k in writes if k.startswith(('guard:RESOURCE_CONSTRAINT:', 'guard:CONFLICT_GROUP:', 'guard:ENTITY:')))
    if not enrolled or not contested:
        return
    wr = _wr(revision)
    peers = repo.conn.execute(text("""
        SELECT r.task_id::text,r.generation,r.observation_id::text,r.details,r.world_revision_id::text,t.state
        FROM nf_semantic_reads r JOIN nf_tasks t ON t.task_id=r.task_id AND t.generation=r.generation
        JOIN nf_proposals p ON p.proposal_id=CAST(r.details->>'proposal_id' AS uuid)
        WHERE r.branch_id=:branch AND r.task_id<>:task AND r.keys && CAST(:keys AS text[])
          AND r.details->>'origin'='PROPOSAL' AND r.details->'story_time'=CAST(:at AS jsonb)
          AND p.template_id<>'JOINT_INTERACTION' AND t.state NOT IN ('ACCEPTED','CANCELLED','FAILED')
          AND (EXISTS(SELECT FROM nf_trace_tasks WHERE task_id=r.task_id)
               OR EXISTS(SELECT FROM nf_supervisor_jobs WHERE task_id=r.task_id))
        ORDER BY r.task_id
    """), {'branch':wr['branch_id'], 'task':task_id, 'keys':contested, 'at':json.dumps(at)}).mappings()
    for peer in peers:
        overlap = sorted(set(contested).intersection(peer['details']['write_effects']))
        if not overlap:
            continue
        peer_id = peer['task_id']
        workflows = repo.conn.scalars(text("""
            SELECT DISTINCT coalesce(tt.trace_id,t.task_id)::text FROM nf_tasks t
            LEFT JOIN nf_trace_tasks tt USING(task_id) WHERE t.task_id=ANY(CAST(:ids AS uuid[]))
        """), {'ids':[task_id,peer_id]}).all()
        info = {'decision_version':VERSION, 'detection_method':'PENDING_SAME_SCENE_WRITES',
                'scopes':overlap, 'source_workflow_ids':sorted(workflows),
                'source_world_refs':_unique([wr,{**wr,'world_revision_id':peer['world_revision_id']}]),
                'dependency_manifest_refs':[observation,peer['observation_id']],
                'affected_task_ids':sorted([task_id,peer_id]), 'evidence_refs':peer['details']['evidence_refs'],
                'story_time_constraints':[at], 'resolution_strategy':'JOINT_ADJUDICATION',
                'conflict_type':'JOINT_ADJUDICATION_REQUIRED'}
        for target, gen, source in ((task_id,generation,peer_id),(peer_id,peer['generation'],task_id)):
            case = str(uuid5(UUID(target), f'{VERSION}:pending:{gen}:{wr["world_revision_id"]}'))
            _emit_case(repo, wr, target, gen, source, case, info)


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


_MATCH_INDEXED = """
    FROM nf_semantic_changes c JOIN nf_semantic_reads r
      ON r.branch_id=c.branch_id AND r.work_id=c.work_id AND r.branch_seq<c.branch_seq
      AND r.keys && c.keys
      AND c.source_task_id IS DISTINCT FROM r.task_id
    JOIN nf_tasks t ON t.task_id=r.task_id AND t.generation=r.generation AND t.branch_id=r.branch_id
    WHERE t.state NOT IN ('CANCELLED','FAILED')
      AND NOT EXISTS(SELECT FROM nf_semantic_revalidations p
                     WHERE p.observation_id=r.observation_id AND p.branch_seq>=c.branch_seq)
      AND NOT ((coalesce(r.details->'knowledge_cut'->>'plane','')='BELIEF' OR r.details ? 'historical_through')
        AND NOT ('history'=ANY(c.keys) OR 'interpretation'=ANY(c.keys))
        AND jsonb_array_length(c.details->'story_points')>0
        AND NOT EXISTS (SELECT 1 FROM jsonb_array_elements(c.details->'story_points') AS e(p)
             WHERE e.p='null'::jsonb OR
              ((e.p->>'tick')::bigint,(e.p->>'phase')::bigint) <=
              ((coalesce(r.details->'historical_through',r.details->'knowledge_cut'->'known_at')->>'tick')::bigint,
               (coalesce(r.details->'historical_through',r.details->'knowledge_cut'->'known_at')->>'phase')::bigint)))
"""

_MATCH = _MATCH_INDEXED.replace('AND r.keys && c.keys', """AND (r.keys && c.keys OR r.created_at IS NULL
           OR EXISTS(SELECT FROM nf_semantic_upgrade_cuts u
               WHERE u.branch_id=c.branch_id AND c.branch_seq<=u.branch_seq)
           OR EXISTS(SELECT FROM nf_semantic_read_supplements s
                     WHERE s.observation_id=r.observation_id AND s.keys && c.keys)
           OR EXISTS(SELECT FROM nf_semantic_change_supplements d
                     WHERE d.world_revision_id=c.world_revision_id AND d.keys && r.keys))""")


def check_task_dependencies(repo, task_id, generation):
    """Final short-txn barrier. No case worker, broker, or fresh cache required."""
    # One PostgreSQL snapshot covers late query registration, branch divergence,
    # pending contenders, and legacy inputs whose earlier reads cannot be proven.
    reason = repo.conn.scalar(text("SELECT CASE WHEN EXISTS(SELECT 1 " + _MATCH + """
        AND r.task_id=:task AND r.generation=:gen) THEN 'stale'
        WHEN EXISTS(SELECT FROM nf_conflict_cases WHERE task_id=:task AND generation=:gen
           AND conflict_type='JOINT_ADJUDICATION_REQUIRED' AND details->>'detection_method'='PENDING_SAME_SCENE_WRITES')
            THEN 'joint'
        WHEN EXISTS(SELECT FROM nf_semantic_reads r JOIN nf_tasks t USING(task_id)
                    WHERE r.task_id=:task AND r.generation=:gen AND r.branch_id<>t.branch_id)
            THEN 'branch'
        WHEN EXISTS(SELECT FROM nf_contexts ctx JOIN nf_branches b
              ON b.branch_id=CAST(ctx.read_cut->'world_ref'->>'branch_id' AS uuid)
              WHERE ctx.task_id=:task AND ctx.generation=:gen
                AND b.head_revision_id<>CAST(ctx.read_cut->'world_ref'->>'world_revision_id' AS uuid)
                AND NOT EXISTS(SELECT FROM nf_semantic_reads s
                    WHERE s.task_id=ctx.task_id AND s.generation=ctx.generation
                      AND s.details->>'origin'='CONTEXT'))
            THEN 'legacy' END
    """), {'task':task_id,'gen':generation})
    if reason:
        messages = {'stale':'This task input requires revalidation.',
                    'joint':'Joint adjudication is required before accepting this candidate.',
                    'branch':'A branch-specific task input requires review.',
                    'legacy':'Earlier reads on this input need a new verified task.'}
        raise DomainError('DEPENDENCY_CONFLICT', messages[reason])


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
    if all(scopes_for_row <= (set(row['observation'].get('uncertain_keys', [])) | set(row['uncertain_keys']))
           for row in rows if (scopes_for_row := set(row['keys']).intersection(row['read_keys']))):
        return 'SEMANTIC_UNCERTAIN', 'ESCALATE_REVIEW'
    if any(r['observation'].get('uncertain') for r in rows):
        return 'SEMANTIC_UNCERTAIN', 'ESCALATE_REVIEW'
    if rows[0]['state'] in {'ACCEPTED', 'RESULT_STORED', 'VALIDATING', 'WAITING_REVIEW'}:
        return 'STALE_PRECONDITION', 'RECOMPUTE_DEPENDENTS'
    return 'STALE_PRECONDITION', 'REVALIDATE'


def _emit_case(repo, wr, task, generation, src, case, details):
    from .audit_log import LogService

    made = repo.conn.scalar(text("""
        INSERT INTO nf_conflict_cases
        (case_id,work_id,branch_id,task_id,generation,source_task_id,world_revision_id,conflict_type,strategy,details)
        VALUES (:case,:work_id,:branch_id,:task,:gen,:src,:world_revision_id,:kind,:strategy,CAST(:proof AS jsonb))
        ON CONFLICT DO NOTHING RETURNING case_id
    """), {**wr, 'case': case, 'task': task, 'gen': generation, 'src': src,
           'kind': details['conflict_type'], 'strategy': details['resolution_strategy'], 'proof': json.dumps(details)})
    if made:
        LogService.conflict(repo.conn, 'detected', case_id=case, receipt_id=case, task_id=task,
                            generation=generation, outcome=details['conflict_type'], details=details)
        repo.outbox('semantic.conflict.detected', 'conflict', case,
                    {'case_id': case, 'work_id': wr['work_id'], 'branch_id': wr['branch_id'],
                     'task_id': task, 'generation': generation, 'world_ref': wr}, 'semantic-case:' + case)
        repo.conn.exec_driver_sql('NOTIFY nf_supervisor_wake')
    return bool(made)


def _detect(repo, condition, params, *, legacy=True):
    match = _MATCH if legacy else _MATCH_INDEXED
    projection = ("c.keys AS keys,r.keys AS read_keys,ARRAY[]::text[] AS uncertain_keys" if not legacy else """
        c.keys || coalesce((SELECT d.keys FROM nf_semantic_change_supplements d
                            WHERE d.world_revision_id=c.world_revision_id),ARRAY[]::text[]) AS keys,
        r.keys || coalesce((SELECT s.keys FROM nf_semantic_read_supplements s
                            WHERE s.observation_id=r.observation_id),ARRAY[]::text[]) AS read_keys,
        coalesce((SELECT s.uncertain_keys FROM nf_semantic_read_supplements s
                  WHERE s.observation_id=r.observation_id),ARRAY[]::text[]) AS uncertain_keys""")
    rows = repo.conn.execute(text("""
        SELECT /* semantic_detector */ c.work_id,c.branch_id,c.world_revision_id,c.source_task_id,c.details,
               r.observation_id,r.details AS observation,r.task_id,r.generation,
               r.world_revision_id AS read_revision,t.state,
        """ + projection + match + " AND " + condition +
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
        if _emit_case(repo, wr, task, generation, src, case, details):
            inserted.append(case)
    return inserted


def detect_revision(repo, world_ref):
    # Keep the hot inverted-index path free of disjuncts. A separately
    # indexed compatibility path applies only to migrated incomplete cuts.
    legacy = repo.conn.scalar(text("""
        SELECT EXISTS(SELECT FROM nf_semantic_reads r WHERE r.created_at IS NULL
                      AND r.branch_id=c.branch_id AND r.branch_seq<c.branch_seq)
            OR EXISTS(SELECT FROM nf_semantic_upgrade_cuts u WHERE u.branch_id=c.branch_id AND c.branch_seq<=u.branch_seq)
            OR EXISTS(SELECT FROM nf_semantic_read_supplements s
                      WHERE s.branch_id=c.branch_id AND s.branch_seq<c.branch_seq AND s.keys && c.keys)
            OR EXISTS(SELECT FROM nf_semantic_change_supplements d WHERE d.world_revision_id=c.world_revision_id)
        FROM nf_semantic_changes c WHERE c.world_revision_id=:world_revision_id
          AND c.branch_id=:branch_id AND c.work_id=:work_id
    """), world_ref)
    if legacy is None:
        return []
    return _detect(repo, 'c.world_revision_id=:world_revision_id AND c.branch_id=:branch_id AND c.work_id=:work_id',
                   world_ref, legacy=legacy)


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
            SELECT r.*,s.keys AS extra_keys FROM nf_semantic_reads r
            LEFT JOIN nf_semantic_read_supplements s USING(observation_id)
            WHERE task_id=:task AND generation=:gen
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
        keys = set(receipt['keys']) | set(receipt['extra_keys'] or ())
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


def revalidate_task(repo, kernel, task_id, generation, world_ref):
    """Re-prove every matched receipt on the locked HEAD or leave all barriers.

    The supported proof is intentionally narrow: deterministic registered
    guarded commands and known exact objective state at the old cut. Both the
    sampled state and the latest projection must retain their value. A client
    cannot attest completeness, waive an evidence version, or install a proof.
    """
    from novel_factory.domain.world import state_at

    from .templates import GUARDED, build

    branch = repo.one('branches', {'branch_id': world_ref['branch_id'], 'work_id': world_ref['work_id']}, lock='update')
    task = repo.one('tasks', {'task_id': task_id, 'work_id': world_ref['work_id']}, lock='update')
    if not branch or not task or task['generation'] != generation or task['branch_id'] != world_ref['branch_id']:
        return False
    if branch['head_revision_id'] != world_ref['world_revision_id'] or task['state'] in {'CANCELLED','FAILED','ACCEPTED'}:
        return False
    rows = list(repo.conn.execute(text("""
        SELECT DISTINCT r.observation_id,r.details,r.world_revision_id,
                        (r.created_at IS NULL OR EXISTS (SELECT FROM nf_semantic_upgrade_cuts u
                          WHERE u.branch_id=c.branch_id AND c.branch_seq<=u.branch_seq)) AS legacy
        """ + _MATCH + " AND r.task_id=:task AND r.generation=:gen"), {'task': task_id, 'gen': generation}).mappings())
    if not rows:
        return False
    current = kernel.revision(repo, world_ref)
    originals, proofs = {}, []
    for row in rows:
        proof = row['details']
        if row['legacy']:
            return False
        rid = str(row['world_revision_id'])
        old_ref = {**world_ref, 'world_revision_id': rid}
        if rid not in originals:
            originals[rid] = kernel.revision(repo, old_ref)
        old = originals[rid]
        if (old['interpretation_ref'] != current['interpretation_ref']
                or old['provenance'].get('history_epoch', 0) != current['provenance'].get('history_epoch', 0)):
            return False
        if proof['origin'] == 'PROPOSAL':
            prop = repo.one('proposals', {'proposal_id': proof['proposal_id'], 'task_id': task_id, 'generation': generation})
            if not prop or prop['template_id'] not in GUARDED:
                return False
            try:
                rebuilt = build(prop['template_id'], prop['template_args'], current['snapshot'], prop['proposal_id'])
            except DomainError:
                return False
            if rebuilt != prop['footprint']:
                return False
            # Closed process frontiers are admission facts; preserving the
            # template's output alone cannot allow inserting into closed time.
            for advance in prop['payload']['process_advances']:
                if current['frontiers'].get(advance['process_id']) != advance['expected_frontier']:
                    return False
        elif proof['origin'] in {'QUERY', 'CONTEXT'}:
            query = proof['query']
            if (query['kind'] != 'STATE' or proof['knowledge_cut']['plane'] != 'WORLD'
                    or proof['story_time']['kind'] != 'POINT' or proof.get('uncertain_keys')):
                return False
            at = proof['story_time']['at']
            for subject in query['subject_ids']:
                entity = old['snapshot']['entities'].get(subject)
                if not entity:
                    return False
                scope = entity.get('scope_id', subject)
                if point_key(old['frontiers'].get(scope, {'tick':0,'phase':0})) <= point_key(at):
                    return False
                for predicate in query['predicates']:
                    for moment in (at, None):
                        before = state_at(old['snapshot'], subject, predicate, moment)
                        after = state_at(current['snapshot'], subject, predicate, moment)
                        if (before['value_status'] == 'UNRESOLVED' or before['value_status'] != after['value_status']
                                or before['data'] != after['data']):
                            return False
            for evidence in proof['evidence_refs']:
                if evidence.get('kind') == 'EVENT':
                    event = evidence['event_ref']
                    if current['selection'].get(event['occurrence_id']) != event['event_version_id']:
                        return False
        else:
            return False
        oid = str(row['observation_id'])
        proofs.append({**world_ref, 'id': oid, 'seq': current['branch_seq'],
                       'hash': fingerprint([VERSION, oid, old_ref, world_ref, current['state_hash'], proof['origin']])})
    repo.conn.execute(text("""
        INSERT INTO nf_semantic_revalidations(observation_id,work_id,branch_id,world_revision_id,branch_seq,proof_hash)
        VALUES (:id,:work_id,:branch_id,:world_revision_id,:seq,:hash) ON CONFLICT DO NOTHING
    """), proofs)
    return True


def inherit_material(repo, task_id, generation, revision, refs, cut, head_id):
    """Flatten recorded provenance when an objective task consumes work products.

    A character receives exactly the authorized bytes. Resolution of their
    BELIEF input never exposes a hidden dependency of the author or a sender.
    """
    if not refs or cut['plane'] == 'BELIEF':
        return
    wr = _wr(revision)
    rows = list(repo.conn.execute(text("""
        SELECT DISTINCT r.observation_id,r.work_id,r.branch_id,r.world_revision_id,r.branch_seq,
               r.keys || coalesce(s.keys,ARRAY[]::text[]) AS keys,r.details,
               s.uncertain_keys,a.evidence_id AS product,r.created_at IS NULL AS legacy
        FROM jsonb_to_recordset(CAST(:refs AS jsonb)) AS selected(artifact_id uuid,artifact_revision_id uuid,sha256 text)
        JOIN nf_artifacts art ON (art.artifact_id,art.artifact_revision_id,art.sha256)
                              =(selected.artifact_id,selected.artifact_revision_id,selected.sha256) AND art.work_id=:work
        JOIN nf_audit_events a ON a.evidence_id=art.artifact_revision_id
        JOIN nf_semantic_reads r
          ON (a.task_id,a.generation)=(r.task_id,r.generation)
        LEFT JOIN nf_semantic_read_supplements s USING(observation_id)
        WHERE a.work_id=:work AND r.work_id=:work
          AND a.task_id IS NOT NULL AND a.task_id<>:task AND a.event_type LIKE 'artifact.%'
          AND (r.created_at IS NULL OR r.created_at<=a.recorded_at) ORDER BY r.observation_id LIMIT 257
    """), {'work':wr['work_id'], 'refs':json.dumps(refs), 'task':task_id}).mappings())
    if len(rows) > 256:
        raise DomainError('CAPACITY_LIMIT', 'Material dependency budget requires a narrower task.')
    for row in rows:
        if row['details'].get('knowledge_cut', {}).get('plane') == 'BELIEF':
            # The consumer depends on frozen attributed testimony. The sender's
            # private cognition changes do not retroactively rewrite testimony.
            continue
        details = {'origin':'ARTIFACT', 'dependency_kind':'DERIVED_FROM', 'base_world_ref':wr,
                   'knowledge_cut':cut, 'source_observation_id':str(row['observation_id']),
                   'source_artifact_version':str(row['product']), 'evidence_refs':[
                       ref for ref in refs if ref['artifact_revision_id'] == str(row['product'])]}
        uncertainty = set(row['details'].get('uncertain_keys', ())) | set(row['uncertain_keys'] or ())
        if uncertainty:
            details['uncertain_keys'] = sorted(uncertainty)
        inherited = set(row['keys'])
        if row['legacy']:
            details['uncertain'] = True
            inherited.add('uncertain')
        elif row['details'].get('historical_through'):
            details['historical_through'] = row['details']['historical_through']
        record_observation(repo, task_id, generation, row, inherited, details, head_id=head_id)


def record_material_read(repo, task_id, generation, revision, ref, knowledge_cut, story_time, head_id):
    from novel_factory.domain.canonical import strict_json_loads
    from novel_factory.domain.temporal import contains

    keys, uncertain_keys = set(), set()
    if knowledge_cut['plane'] in {'WORLD','SIMULATION'}:
        # The active carrier relation selects the source. An ordinary TEXT or
        # EVIDENCE artifact is equally valid, regardless of its issuance path
        # or the JSON formatting used to encode its immutable reference.
        for rel in revision['snapshot']['relations']:
            if (rel['predicate'] != 'message_content_ref' or rel['plane'] != 'WORLD'
                    or not contains(rel['valid_time'], story_time)):
                continue
            try:
                selected = strict_json_loads(rel['object_value']['value'])
            except DomainError:
                continue  # An unstructured genesis string binds no artifact.
            if selected == ref:
                keys.add(slot(rel['subject_id'], 'message_content_ref'))
                uncertain_keys.update(slot(rel['subject_id'], p) for p in ('message_authentic','attributed_reading_ref'))
    info = {'origin':'ARTIFACT', 'dependency_kind':'SOURCE_OF', 'knowledge_cut':knowledge_cut,
            'story_time':{'kind':'POINT','at':story_time}, 'uncertain_keys':sorted(uncertain_keys),
            'evidence_refs':[{'kind':'ARTIFACT','artifact_ref':ref}]}
    record_observation(repo, task_id, generation, revision, keys | uncertain_keys, info, head_id=head_id)
    inherit_material(repo, task_id, generation, revision, [ref], knowledge_cut, head_id)
