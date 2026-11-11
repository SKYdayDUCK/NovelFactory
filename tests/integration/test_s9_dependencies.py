from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from sqlalchemy import text

from novel_factory.application.message_sources import MessageSourceService
from novel_factory.application.queries import WorldQueryService
from novel_factory.application.semantic_dependencies import (
    add_assumption,
    check_task_dependencies,
    detect_revision,
    revalidate_task,
)
from novel_factory.domain.canonical import canonical_bytes
from novel_factory.domain.errors import DomainError
from novel_factory.domain.propositions import encode_proposition
from novel_factory.fixtures import (
    create_bound_task,
    entity_value,
    fixture_definition,
    point,
    prepare_command,
    run_command,
)

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def world(k):
    f = fixture_definition('white_bridge')
    carrier = str(uuid4())
    f['genesis']['entities'][carrier] = {'entity_id': carrier, 'entity_kind': 'KNOWLEDGE_CARRIER',
                                         'name': '密信', 'created_at': point(1)}
    f['frontiers'][carrier] = point(1, 1)
    return {**f, **k.bootstrap_work('semantic-test', f['genesis'], frontiers=f['frontiers']), 'letter': carrier}


def set_fact(k, f, wr, subject, predicate, value, day):
    return run_command(k, f['principal'], wr, 'SET_RELATION',
                       {'subject_id': subject, 'predicate': predicate, 'object_value': value,
                        'at': point(day)})['world_ref']


def query(k, f, wr, task, subject, predicate, day, *, cut=None, principal=None):
    request = {'schema_version': '2.1', 'task_id': task['task_id'], 'generation': task['generation'],
               'read_contract': {'revision': {'mode':'FIXED','world_ref':wr}, 'process_policy':'ALLOW_PARTIAL',
                                 'index_policy':'AUTHORITATIVE','max_wait_ms':0},
               'knowledge_cut': cut or {'plane':'WORLD'}, 'subject_time': {'kind':'POINT','at':point(day)},
               'query': {'kind':'STATE','subject_ids':[subject],'predicates':[predicate]},
               'page_size': 10, 'cursor': None}
    return WorldQueryService(k).query(principal or f['principal'], request)


def gift(k, f, wr, task):
    ids = f['ids']
    return prepare_command(k, f['principal'], wr, 'TRANSFER_ITEM',
                           {'item_id':ids['sword2'],'from_id':ids['smith2'],'to_id':ids['friend'],
                            'transfer_kind':'GIFT','at':point(20)}, mode='DEPENDENCY_GUARDED', task=task)


def unchanged(k, task):
    with k.db.uow() as r:
        check_task_dependencies(r, task['task_id'], task['generation'])


def test_false_letter_blocks_disjoint_guarded_candidate_before_redis_preserves_belief(kernel_env):
    k = kernel_env['kernel']
    f = world(k)
    admin, wr, carrier, ids = f['principal'], f['world_ref'], f['letter'], f['ids']
    content = MessageSourceService(k).save_version(admin, wr, '请在北门集结。', 'letter')['source_ref']
    link = {'kind':'STRING','value':canonical_bytes(content).decode()}
    source = set_fact(k, f, wr, carrier, 'message_content_ref', link, 2)
    genuine = {'kind':'BOOLEAN','value':True}
    authentic = set_fact(k, f, source, carrier, 'message_authentic', genuine, 3)
    heard = run_command(k, admin, authentic, 'RECORD_COGNITION', {
        'actor_id':ids['protagonist'], 'statement':encode_proposition(carrier, 'message_authentic', genuine),
        'record_kind':'BELIEF','epistemic_mode':'HEARD','evidence_refs':[{'kind':'ARTIFACT','artifact_ref':content}],
        'at':point(4)})['world_ref']
    actor = k.issue_grant(admin, wr['work_id'], role_id='character_actor', actor_id=ids['protagonist'],
                          capabilities=['READ','COMPILE','TASK_CREATE','TASK_CLAIM','ARTIFACT_WRITE'],
                          max_story_time=point(30))['principal']
    actor_task = create_bound_task(k, actor, heard, point(4), [carrier], ['message_authentic'],
                                   plane='BELIEF', actor_id=ids['protagonist'])
    planner = create_bound_task(k, admin, heard, point(20), [carrier], ['message_authentic'])
    material = query(k, f, heard, planner, carrier, 'message_authentic', 3)
    assert material['payload']['state_items'][0]['answer']['data'] == genuine
    proposed = gift(k, f, heard, planner)
    forged = set_fact(k, f, heard, carrier, 'message_authentic', {'kind':'BOOLEAN','value':False}, 9)
    with pytest.raises(DomainError, match='revalidation') as stale:
        k.accept(admin, proposed['request'])
    assert stale.value.code == 'DEPENDENCY_CONFLICT'
    unchanged(k, actor_task)
    with k.db.uow() as r:
        assert k.revision(r, forged)['snapshot']['cognitions'] == k.revision(r, heard)['snapshot']['cognitions']
        with pytest.raises(RuntimeError), r.conn.begin_nested():
            assert detect_revision(r, forged)
            raise RuntimeError('crash before SQL commit')
        assert not r.conn.scalar(text('SELECT count(*) FROM nf_conflict_cases WHERE task_id=:t'), {'t':planner['task_id']})
    def scan(_):
        with k.db.uow() as r:
            return detect_revision(r, forged)
    with ThreadPoolExecutor(2) as pool:
        scans = list(pool.map(scan, range(2)))
    assert sum(map(len, scans)) == 1
    with k.db.uow() as r:
        case = r.conn.execute(text('SELECT * FROM nf_conflict_cases WHERE task_id=:t'), {'t':planner['task_id']}).mappings().one()
        assert case['conflict_type'] == 'STALE_PRECONDITION'
        assert len(case['details']['source_workflow_ids']) == 2
        assert {w['world_revision_id'] for w in case['details']['source_world_refs']} == {heard['world_revision_id'],forged['world_revision_id']}
        assert len(detect_revision(r, forged)) == 0
        assert k.head(r, wr['work_id']) == forged
    assert k.get_artifact(admin, wr['work_id'], content).decode() == '请在北门集结。'


def test_negative_query_after_completed_scan_and_unrelated_work(kernel_env):
    k = kernel_env['kernel']
    f = world(k)
    wr, admin, ids = f['world_ref'], f['principal'], f['ids']
    reader = create_bound_task(k, admin, wr, point(20), [ids['sword2']], ['lifecycle'])
    other = create_bound_task(k, admin, wr, point(20), [ids['smith2']], ['lifecycle'])
    independently = gift(k, f, wr, other)
    after = set_fact(k, f, wr, ids['north'], 'blocked_by', entity_value(ids['blocker']), 10)
    with k.db.uow() as r:
        assert not detect_revision(r, after)
    # The scan already ran. A late old-cut lookup whose result contains no
    # blocker registers a predicate receipt and an actionable case atomically.
    result = query(k, f, wr, reader, ids['north'], 'blocked_by', 20)
    assert result['payload']['state_items'][0]['answer']['data'] is None
    with pytest.raises(DomainError):
        unchanged(k, reader)
    with k.db.uow() as r:
        assert r.conn.scalar(text('SELECT count(*) FROM nf_conflict_cases WHERE task_id=:t'), {'t':reader['task_id']}) == 1
    assert k.accept(admin, independently['request'])['parent_world_ref'] == after


def test_assumption_is_additive_source_bound_and_never_confers_another_tasks_reads(kernel_env):
    k = kernel_env['kernel']
    f = world(k)
    wr, admin, ids = f['world_ref'], f['principal'], f['ids']
    task = create_bound_task(k, admin, wr, point(20), [ids['sword']], ['lifecycle'])
    observed = query(k, f, wr, task, ids['guard1'], 'on_duty', 1)
    binding = {'task_id':task['task_id'], 'generation':task['generation'], 'dependency_handle':observed['dependency_handle'],
               'dependency_kind':'ASSUMES_TRUE', 'statement':'城门如常'}
    a = add_assumption(k, admin, binding)
    assert a['uncertain'] and add_assumption(k, admin, binding) == a
    other = create_bound_task(k, admin, wr, point(20), [ids['sword2']], ['lifecycle'])
    second = query(k, f, wr, other, ids['guard1'], 'on_duty', 1)
    with pytest.raises(DomainError) as denied:
        add_assumption(k, admin, {**binding,'task_id':other['task_id']})
    assert denied.value.code == 'VIEW_DENIED'
    b = add_assumption(k, admin, {**binding,'task_id':other['task_id'],'dependency_handle':second['dependency_handle']})
    assert a['proposition_key'] != b['proposition_key']
    typed = encode_proposition(ids['guard1'], 'on_duty', {'kind':'BOOLEAN','value':True})
    assert not add_assumption(k, admin, {**binding, 'statement':typed})['uncertain']
    with pytest.raises(DomainError):
        add_assumption(k, admin, {**binding,'statement':encode_proposition(ids['guard2'],'on_duty',{'kind':'BOOLEAN','value':True})})
    after = set_fact(k, f, wr, ids['guard2'], 'on_duty', {'kind':'BOOLEAN','value':False}, 10)
    with k.db.uow() as r:
        assert len(detect_revision(r, after)) == 2
        assert set(r.conn.scalars(text('SELECT conflict_type FROM nf_conflict_cases WHERE world_revision_id=:r'),
                                  {'r':after['world_revision_id']})) == {'SEMANTIC_UNCERTAIN'}


def test_revalidate_same_value_is_bounded_to_proven_observation_and_head(kernel_env):
    k = kernel_env['kernel']
    f = world(k)
    wr, admin, ids = f['world_ref'], f['principal'], f['ids']
    task = create_bound_task(k, admin, wr, point(1), [ids['guard1']], ['on_duty'])
    observed = {'kind':'BOOLEAN','value':True}
    # Register a new fact version carrying the same semantic value.
    same = set_fact(k, f, wr, ids['guard1'], 'on_duty', observed, 3)
    with pytest.raises(DomainError):
        unchanged(k, task)
    with k.db.uow() as r:
        assert revalidate_task(r, k, task['task_id'], task['generation'], same)
        check_task_dependencies(r, task['task_id'], task['generation'])
    different = set_fact(k, f, same, ids['guard1'], 'on_duty', {'kind':'BOOLEAN','value':False}, 4)
    with pytest.raises(DomainError):
        unchanged(k, task)
    with k.db.uow() as r:
        assert not revalidate_task(r, k, task['task_id'], task['generation'], different)
        assert not revalidate_task(r, k, task['task_id'], task['generation'], same)


def test_alternate_branch_read_preserves_candidate_without_accepting_it(kernel_env):
    k = kernel_env['kernel']
    f = world(k)
    wr, admin, ids = f['world_ref'], f['principal'], f['ids']
    task = create_bound_task(k, admin, wr, point(20), [ids['smith']], ['lifecycle'])
    candidate = k.fork(admin, wr, 'CANDIDATE', 'read-alternative')
    assert query(k, f, candidate, task, ids['sword'], 'owner', 1)['read_cut']['world_ref'] == candidate
    with k.db.uow() as repo:
        case = repo.conn.execute(text("""
            SELECT branch_id::text,conflict_type,strategy FROM nf_conflict_cases WHERE task_id=:t
        """), {'t':task['task_id']}).one()
        assert tuple(case) == (wr['branch_id'], 'BRANCH_DIVERGENCE', 'CANDIDATE_BRANCH')
        assert k.head(repo, wr['work_id'], wr['branch_id']) == wr
    with pytest.raises(DomainError):
        unchanged(k, task)


def test_closed_history_is_distinct_from_a_continuing_assumption(kernel_env):
    k = kernel_env['kernel']
    f = world(k)
    wr, admin, ids = f['world_ref'], f['principal'], f['ids']
    history, ongoing = [create_bound_task(k, admin, wr, point(30), [ids['smith']], ['lifecycle']) for _ in range(2)]
    query(k, f, wr, history, ids['guard1'], 'on_duty', 1)
    observed = query(k, f, wr, ongoing, ids['guard1'], 'on_duty', 1)
    add_assumption(k, admin, {'task_id':ongoing['task_id'], 'generation':ongoing['generation'],
                    'dependency_handle':observed['dependency_handle'],'dependency_kind':'REQUIRES',
                    'statement':encode_proposition(ids['guard1'],'on_duty',{'kind':'BOOLEAN','value':True})})
    later = set_fact(k, f, wr, ids['guard1'], 'on_duty', {'kind':'BOOLEAN','value':False}, 10)
    unchanged(k, history)
    with pytest.raises(DomainError):
        unchanged(k, ongoing)
    with k.db.uow() as repo:
        assert len(detect_revision(repo, later)) == 1


def test_legacy_context_with_modern_proposal_cannot_cross_changed_head(kernel_env, monkeypatch):
    from novel_factory.application import queries
    k = kernel_env['kernel']
    f = world(k)
    wr, admin, ids = f['world_ref'], f['principal'], f['ids']
    # Emit the original complete P5.5 context, dependency manifest, and task
    # records without S9's new observation, exactly as a pre-upgrade input.
    with monkeypatch.context() as patch:
        patch.setattr(queries, 'record_query', lambda *a, **kw: None)
        task = create_bound_task(k, admin, wr, point(20), [ids['smith']], ['lifecycle'])
    prepared = gift(k, f, wr, task)
    after = set_fact(k, f, wr, ids['guard1'], 'on_duty', {'kind':'BOOLEAN','value':False}, 10)
    with pytest.raises(DomainError) as stale:
        k.accept(admin, prepared['request'])
    assert stale.value.code == 'DEPENDENCY_CONFLICT'
    with k.db.uow() as repo:
        assert k.head(repo, wr['work_id']) == after


def test_material_reading_propagates_attributed_meaning_without_rewriting_bytes(kernel_env):
    k = kernel_env['kernel']
    f = world(k)
    wr, admin, carrier, ids = f['world_ref'], f['principal'], f['letter'], f['ids']
    source = MessageSourceService(k).save_version(admin, wr, '星落时开门。', 'sign')['source_ref']
    link = {'kind':'STRING', 'value':canonical_bytes(source).decode()}
    named = set_fact(k, f, wr, carrier, 'message_content_ref', link, 2)
    task = create_bound_task(k, admin, named, point(2), [ids['smith']], ['lifecycle'])
    bundle = task['compiled']['compiled']['bundle']
    assert k.get_artifact(admin, wr['work_id'], source, task_id=task['task_id'], generation=task['generation'],
                          context_bundle_id=bundle['context_bundle_id'], world_ref=named, story_time=point(2),
                          knowledge_cut={'plane':'WORLD'}).decode() == '星落时开门。'
    meaning = k.put_artifact(admin, wr['work_id'], b'coded signal', 'EVIDENCE')
    later = set_fact(k, f, named, carrier, 'attributed_reading_ref',
                     {'kind':'STRING','value':canonical_bytes(meaning).decode()}, 5)
    with k.db.uow() as repo:
        assert len(detect_revision(repo, later)) == 1
        assert repo.conn.scalar(text('SELECT conflict_type FROM nf_conflict_cases WHERE task_id=:t'),
                                {'t':task['task_id']}) == 'SEMANTIC_UNCERTAIN'
        assert k.revision(repo, named)['snapshot']['cognitions'] == k.revision(repo, later)['snapshot']['cognitions']
    assert k.get_artifact(admin, wr['work_id'], source).decode() == '星落时开门。'


def test_enrolled_candidates_contest_single_item_before_either_can_commit(kernel_env):
    k = kernel_env['kernel']
    f = world(k)
    wr, admin, ids = f['world_ref'], f['principal'], f['ids']
    tasks = [create_bound_task(k, admin, wr, point(20), [ids['sword2']], ['owner']) for _ in range(2)]
    with k.db.uow() as repo:
        for task in tasks:
            repo.conn.execute(text('INSERT INTO nf_trace_tasks(task_id,trace_id) VALUES (:task,:trace)'),
                              {'task':task['task_id'], 'trace':str(uuid4())})
    first = gift(k, f, wr, tasks[0])
    k.propose(admin, tasks[1]['task_id'], tasks[1]['generation'], 'TRANSFER_ITEM',
              {'item_id':ids['sword2'],'from_id':ids['smith2'],'to_id':ids['protagonist'],
               'transfer_kind':'GIFT','at':point(20)}, 'second-intent')
    for task in tasks:
        with pytest.raises(DomainError, match='Joint adjudication'):
            unchanged(k, task)
    with pytest.raises(DomainError) as conflict:
        k.accept(admin, first['request'])
    assert conflict.value.code == 'DEPENDENCY_CONFLICT'
    with k.db.uow() as repo:
        assert k.head(repo, wr['work_id']) == wr
        cases = repo.conn.execute(text('SELECT conflict_type,strategy,details FROM nf_conflict_cases WHERE work_id=:work'),
                                  {'work':wr['work_id']}).mappings().all()
        assert len(cases) == 2
        for case in cases:
            assert case['strategy'] == 'JOINT_ADJUDICATION'
            assert set(case['details']['affected_task_ids']) == {t['task_id'] for t in tasks}


def test_only_authorized_downstream_delivery_inherits_actual_writer_query(kernel_env):
    from test_narrative import arrange, runtime_grant, scene_for, task_for

    from novel_factory.application.narrative import NarrativeService
    k = kernel_env['kernel']
    f = arrange(k)
    wr, ids = f['world_ref'], f['ids']
    item = scene_for(k, f)
    writer = item['task']
    query(k, f, wr, writer, ids['guard2'], 'on_duty', 2, principal=item['writer'])
    draft = NarrativeService(k).store_draft(item['writer'], writer['task_id'], writer['generation'],
            item['scene_ref'], '黄昏之后，队伍原地等候。', 'writer-evidence')
    reviewer = runtime_grant(k, f, 'reviewer')
    downstream = task_for(k, f, reviewer, [draft], 'extract', story_time=point(2, 1))
    narrative = NarrativeService(k)
    with pytest.raises(DomainError):
        narrative.read_text(reviewer, downstream['task_id'], downstream['generation'], draft)
    with k.db.uow() as repo:
        assert not repo.conn.scalar(text("SELECT count(*) FROM nf_semantic_reads WHERE task_id=:task AND details->>'origin'='ARTIFACT'"),
                                    {'task':downstream['task_id']})
    narrative.bind_reader_task(f['principal'], downstream['task_id'], downstream['generation'], [draft], 'evidence-read')
    narrative.read_text(reviewer, downstream['task_id'], downstream['generation'], draft)
    changed = set_fact(k, f, wr, ids['guard2'], 'on_duty', {'kind':'BOOLEAN','value':False}, 2)
    with k.db.uow() as repo:
        cases = detect_revision(repo, changed)
        assert len(cases) == 2
        assert {str(x) for x in repo.conn.scalars(text("""
            SELECT task_id FROM nf_conflict_cases WHERE world_revision_id=:change
        """), {'change':changed['world_revision_id']})} == {writer['task_id'],downstream['task_id']}
        provenance = repo.conn.scalar(text("""
            SELECT details FROM nf_semantic_reads WHERE task_id=:task AND details->>'origin'='ARTIFACT'
              AND details->>'source_artifact_version'=:ref AND :key=ANY(keys)
        """), {'task':downstream['task_id'], 'ref':draft['artifact_revision_id'],
               'key':'fact:'+ids['guard2']+':on_duty'})
        assert provenance['source_observation_id']
    with pytest.raises(DomainError):
        unchanged(k, downstream)
