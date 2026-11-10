"""Real PostgreSQL end-to-end verification for the P1 audit remediations."""
from __future__ import annotations

from copy import deepcopy

import pytest

from novel_factory.domain.errors import DomainError
from novel_factory.domain.propositions import encode_proposition
from novel_factory.fixtures import run_command
from novel_factory.infrastructure.repository import KernelRepository
from .conftest import seed, start, request, compile_request, p

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def test_non_admin_artifact_must_match_live_task_generation_and_cut(query_env):
    # Formal cognition at D20 cites an actual immutable EVIDENCE artifact.
    # A different task at D15 (same grant) must not borrow that D20 source.
    world = seed(query_env, covered=0)
    kernel, service = query_env['kernel'], query_env['query']
    ids = world['ids']
    secret_ref = kernel.put_artifact(world['principal'], world['world_ref']['work_id'],
                                     b'formal secret revealed only on D20', 'EVIDENCE')
    cognition = run_command(kernel, world['principal'], world['world_ref'], 'RECORD_COGNITION', {
        'actor_id': ids['P'],
        'statement': encode_proposition(ids['sword'], 'possessed_by',
                                        {'kind': 'ENTITY', 'entity_id': ids['F']}),
        'record_kind': 'BELIEF', 'epistemic_mode': 'HEARD',
        'evidence_refs': [{'kind': 'ARTIFACT', 'artifact_ref': secret_ref}], 'at': p(20),
    })
    wr = cognition['world_ref']
    issued = kernel.issue_grant(
        world['principal'], wr['work_id'], role_id='character_actor', actor_id=ids['P'],
        capabilities=['READ', 'COMPILE', 'TASK_CREATE', 'TASK_CLAIM'], planes=['BELIEF'],
        max_story_time=p(40))
    actor = issued['principal']
    later = start(query_env, world, principal=actor, role='character_actor',
                  at=p(35), wr=wr)
    later_req = compile_request(later, plane='BELIEF', actor=ids['P'])
    result = service.context_compile(actor, later_req)
    bundle = result['compiled']['bundle']
    assert {'kind': 'ARTIFACT', 'artifact_ref': secret_ref} in bundle['permitted_evidence_refs']
    current = start(query_env, world, principal=actor, role='character_actor',
                    at=p(15), wr=wr)
    current_req = compile_request(current, plane='BELIEF', actor=ids['P'])
    early_result = service.context_compile(actor, current_req)
    early_bundle = early_result['compiled']['bundle']
    assert {'kind': 'ARTIFACT', 'artifact_ref': secret_ref} not in early_bundle['permitted_evidence_refs']
    proof = dict(task_id=later['task']['task_id'], generation=later['task']['generation'],
                 context_bundle_id=bundle['context_bundle_id'], world_ref=wr,
                 story_time=p(35), knowledge_cut=later_req['knowledge_cut'])
    assert kernel.get_artifact(actor, wr['work_id'], secret_ref, **proof) == b'formal secret revealed only on D20'
    with pytest.raises(DomainError) as missing:
        kernel.get_artifact(actor, wr['work_id'], secret_ref)
    assert missing.value.code == 'VIEW_DENIED'
    with pytest.raises(DomainError) as crossing:
        kernel.get_artifact(actor, wr['work_id'], secret_ref, **{
            **proof, 'task_id': current['task']['task_id'],
            'generation': current['task']['generation'],
            'context_bundle_id': early_bundle['context_bundle_id'],
            'story_time': p(15), 'knowledge_cut': current_req['knowledge_cut']})
    assert crossing.value.code == 'VIEW_DENIED'
    with pytest.raises(DomainError):
        kernel.get_artifact(actor, wr['work_id'], secret_ref, **{
            **proof, 'task_id': current['task']['task_id'],
            'generation': current['task']['generation'], 'story_time': p(15),
            'knowledge_cut': current_req['knowledge_cut']})
    with pytest.raises(DomainError) as changed_cut:
        kernel.get_artifact(actor, wr['work_id'], secret_ref, **{
            **proof, 'knowledge_cut': current_req['knowledge_cut']})
    assert changed_cut.value.code == 'VIEW_DENIED'
    kernel.cancel_task(actor, later['task']['task_id'], later['task']['generation'], 'cancel-old-artifact-task')
    with pytest.raises(DomainError):
        kernel.get_artifact(actor, wr['work_id'], secret_ref, **proof)
    kernel.revoke_grant(world['principal'], actor.grant_id)
    with pytest.raises(DomainError):
        kernel.get_artifact(actor, wr['work_id'], secret_ref, **proof)


def test_actual_query_snapshots_skip_events_but_fact_timeline_keeps_semantics(query_env, monkeypatch):
    world = seed(query_env)
    task = start(query_env, world)
    service = query_env['query']
    principal = task['principal']
    calls = []
    original_many = KernelRepository.many

    def watched_many(self, table, where, *args, **kwargs):
        if table == 'events':
            calls.append(deepcopy(where))
        return original_many(self, table, where, *args, **kwargs)

    monkeypatch.setattr(KernelRepository, 'many', watched_many)
    state_req = request(task)
    state = service.query(principal, state_req)
    assert state['payload']['state_items'][1]['answer']['data']['entity_id'] == world['ids']['V']
    assert not calls
    timeline_req = request(task, operation={'kind': 'TIMELINE',
        'subject_ids': [world['ids']['sword']], 'entry_kinds': ['STATE_ASSERTION'],
        'time_axis': 'VALID', 'include_inactive': True})
    relations = service.query(principal, timeline_req)
    assert len(relations['payload']['entries']) >= 1
    assert not calls
    batch_req = {k: state_req[k] for k in ('schema_version', 'task_id', 'generation',
                                           'read_contract', 'knowledge_cut')}
    batch_req['items'] = [
        {'item_id': 'state', 'subject_time': state_req['subject_time'],
         'query': state_req['query'], 'page_size': 100},
        {'item_id': 'relations', 'subject_time': timeline_req['subject_time'],
         'query': timeline_req['query'], 'page_size': 100},
    ]
    batched = service.batch(principal, batch_req)
    assert [item['status'] for item in batched['items']] == ['SUCCESS', 'SUCCESS']
    assert not calls
    # The authoritative snapshot is used for the returned values in all three
    # paths, without fetching event JSON from the ledger at query time.
    assert batched['items'][0]['payload'] == state['payload']
    assert batched['items'][1]['payload'] == relations['payload']
