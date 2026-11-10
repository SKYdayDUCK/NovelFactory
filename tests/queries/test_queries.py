from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from novel_factory.domain.canonical import canonical_bytes
from novel_factory.domain.errors import DomainError
from novel_factory.contracts.registry import validate_contract
from .conftest import seed, start, request, compile_request, p, uid


def test_real_query_world_belief_unknown_and_unadvanced_distinct(query_env):
    world = seed(query_env); task = start(query_env, world); service = query_env['query']
    result = service.query(task['principal'], request(task))
    assert result['payload']['state_items'][1]['answer']['data']['entity_id'] == world['ids']['V']
    belief = service.query(task['principal'], request(task, plane='BELIEF'))
    assert belief['payload']['state_items'][1]['answer']['data']['entity_id'] == world['ids']['F']
    pending = service.query(task['principal'], request(task, at=p(50)))
    assert pending['payload']['state_items'][1]['answer']['evaluation_status'] == 'PENDING'
    assert pending['payload']['state_items'][1]['answer']['value_status'] == 'UNRESOLVED'
    unknown = service.query(task['principal'], request(task, plane='BELIEF', operation={'kind':'STATE','subject_ids':[uid()],'predicates':['possessed_by']}))
    answer = unknown['payload']['state_items'][0]['answer']
    assert answer['knowledge_status'] == 'UNKNOWN' and answer['evaluation_status'] == 'EVALUATED'
    strict = request(task, at=p(50)); strict['read_contract']['process_policy'] = 'REQUIRE_EVALUATED'
    with pytest.raises(DomainError) as raised: service.query(task['principal'], strict)
    assert raised.value.code == 'PROCESS_NOT_EVALUATED'


def test_query_distinguishes_known_zero_balance_from_unknown_resource(query_env):
    world = seed(query_env); task = start(query_env, world); ids = world['ids']
    result = query_env['query'].query(task['principal'], request(task, operation={
        'kind': 'STATE', 'subject_ids': [ids['store']],
        'predicates': [f'resource:{ids["grain"]}:bag', f'resource:{uid()}:bag']}))
    zero, unknown = [item['answer'] for item in result['payload']['state_items']]
    assert zero['knowledge_status'] == 'KNOWN' and zero['data']['quantity'] == '0'
    assert unknown['knowledge_status'] == 'UNKNOWN' and unknown['value_status'] == 'UNRESOLVED'
    assert unknown['data'] is None


def test_actor_context_rejects_world_plan_other_actor_and_future(query_env):
    world = seed(query_env); kernel = query_env['kernel']; service = query_env['query']
    grant = kernel.issue_grant(world['principal'], world['world_ref']['work_id'], role_id='character_actor', actor_id=world['ids']['P'], max_story_time=p(35))
    task = start(query_env, world, principal=grant['principal'], role='character_actor', at=p(35))
    result = service.context_compile(task['principal'], compile_request(task, plane='BELIEF'))
    validate_contract('ContextCompileResult', result, '2.1')
    encoded = canonical_bytes(result).decode()
    assert world['ids']['V'] not in encoded
    assert world['ids']['north'] not in encoded
    assert 'ENTITY:' not in encoded and 'PREDICATE_RANGE:' not in encoded
    assert {s['section_kind'] for s in result['compiled']['sections']} <= {'TASK','HARD_CONSTRAINTS','ACTOR_KNOWLEDGE','UNRESOLVED'}
    with pytest.raises(DomainError): service.query(task['principal'], request(task, plane='WORLD'))
    with pytest.raises(DomainError): service.query(task['principal'], request(task, plane='BELIEF', actor=world['ids']['V']))
    with pytest.raises(DomainError): service.query(task['principal'], request(task, plane='BELIEF', at=p(36)))


def test_actor_hidden_and_nonexistent_have_identical_answer_shape(query_env):
    world = seed(query_env); kernel = query_env['kernel']; service = query_env['query']
    grant = kernel.issue_grant(world['principal'], world['world_ref']['work_id'], role_id='character_actor', actor_id=world['ids']['P'], max_story_time=p(35))
    task = start(query_env, world, principal=grant['principal'], role='character_actor', at=p(35))
    def answer(subject):
        value = service.query(task['principal'], request(task, plane='BELIEF', operation={'kind':'STATE','subject_ids':[subject],'predicates':['located_in']}))['payload']['state_items'][0]['answer']
        value.pop('dependency_handle'); return value
    assert answer(world['ids']['V']) == answer(uid())


def test_cache_hit_rechecks_revoked_grant(query_env):
    world = seed(query_env); kernel = query_env['kernel']; service = query_env['query']
    grant = kernel.issue_grant(world['principal'], world['world_ref']['work_id'], role_id='character_actor', actor_id=world['ids']['P'], max_story_time=p(35))
    task = start(query_env, world, principal=grant['principal'], role='character_actor', at=p(35)); req = request(task, plane='BELIEF')
    service.query(task['principal'], req); before=service.cache_hits; service.query(task['principal'], req)
    assert service.cache_hits == before + 1
    kernel.revoke_grant(world['principal'], task['principal'].grant_id)
    with pytest.raises(DomainError) as raised: service.query(task['principal'], req)
    assert raised.value.code == 'VIEW_DENIED'


def test_context_binding_idempotent_atomic_and_input_fixed(query_env):
    world=seed(query_env);task=start(query_env,world,at=p(35));service=query_env['query'];kernel=query_env['kernel']
    req=compile_request(task);result=service.context_compile(task['principal'],req)
    assert service.context_compile(task['principal'],req)==result
    with kernel.db.uow(write=False) as repo:
        context=repo.one('contexts',{'context_bundle_id':result['compiled']['bundle']['context_bundle_id']})
        current=repo.one('tasks',{'task_id':task['task']['task_id']})
        assert current['context_bundle_id']==context['context_bundle_id']
        assert current['dependency_fingerprint']==context['dependency_fingerprint']
        validate_contract('DependencyManifest', context['dependency_manifest'])
    changed=deepcopy(req);changed['token_budget']=19999
    with pytest.raises(DomainError) as raised:service.context_compile(task['principal'],changed)
    assert raised.value.code=='IDEMPOTENCY_CONFLICT'
    changed['idempotency_key']='another-key'
    with pytest.raises(DomainError) as raised:service.context_compile(task['principal'],changed)
    assert raised.value.code=='TASK_REBIND_REQUIRED'


def test_context_budget_failure_does_not_bind_partial_material(query_env):
    world=seed(query_env);task=start(query_env,world,at=p(35));req=compile_request(task);req['token_budget']=256
    with pytest.raises(DomainError) as raised:query_env['query'].context_compile(task['principal'],req)
    assert raised.value.code=='CONTEXT_INCOMPLETE'
    with query_env['db'].uow(write=False) as repo:
        assert repo.one('tasks',{'task_id':task['task']['task_id']})['context_bundle_id'] is None
        assert repo.one('contexts',{'task_id':task['task']['task_id']}) is None


def test_batch_one_worldref_and_inner_capabilities(query_env):
    world=seed(query_env);kernel=query_env['kernel'];service=query_env['query']
    grant=kernel.issue_grant(world['principal'],world['world_ref']['work_id'],role_id='evidence_maintainer',planes=['WORLD'],
        capabilities=['READ','COMPILE','TASK_CREATE','TASK_CLAIM'],max_story_time=p(60))
    task=start(query_env,world,principal=grant['principal'],role='evidence_maintainer');one=request(task)
    batch={k:one[k] for k in ['schema_version','task_id','generation','read_contract','knowledge_cut']}
    batch['items']=[{'item_id':'state','subject_time':one['subject_time'],'query':one['query'],'page_size':100},
                    {'item_id':'graph','subject_time':one['subject_time'],'query':{'kind':'NEIGHBORHOOD','seed_ids':[world['ids']['sword']],
                       'predicates':['possessed_by','located_in'],'max_hops':3,'max_edges':50},'page_size':100}]
    result=service.batch(task['principal'],batch)
    assert result['items'][0]['status']=='SUCCESS' and result['items'][1]['error']['code']=='VIEW_DENIED'
    assert result['items'][0]['payload']['state_items'][0]['answer']['world_ref']==result['read_cut']['world_ref']
    batch['items'][1]['item_id']='state'
    with pytest.raises(DomainError):service.batch(task['principal'],batch)


def test_graph_path_edges_share_story_time_and_worldref(query_env):
    world=seed(query_env);task=start(query_env,world);req=request(task,operation={'kind':'NEIGHBORHOOD','seed_ids':[world['ids']['sword']],
        'predicates':['possessed_by','located_in'],'max_hops':3,'max_edges':50})
    result=query_env['query'].query(task['principal'],req)
    relations=result['payload']['relations']
    assert [(r['subject_id'],r['object_value']['entity_id']) for r in relations]==[(world['ids']['sword'],world['ids']['V']),(world['ids']['V'],world['ids']['north'])]
    assert all(r['world_ref']==world['world_ref'] for r in relations)
    assert result['payload']['coverage']=='COMPLETE'


def test_fixed_cut_stale_search_is_not_unknown_world(query_env):
    world=seed(query_env);task=start(query_env,world);kernel=query_env['kernel'];service=query_env['query']
    # A watermark deliberately references a different retained branch revision.
    fork=kernel.fork(world['principal'],world['world_ref'],'CANDIDATE','watermark-fork')
    fork_ref=fork['world_ref'] if 'world_ref' in fork else fork
    with kernel.db.uow() as repo:
        repo.add('index_watermarks',{'branch_id':world['world_ref']['branch_id'],'index_name':'timeline',
             'world_revision_id':fork_ref['world_revision_id'],'branch_seq':0,'generation':1})
    req=request(task,operation={'kind':'TIMELINE','subject_ids':[world['ids']['sword']],'entry_kinds':['STATE_ASSERTION'],
           'time_axis':'VALID','include_inactive':True})
    req['read_contract']['index_policy']='ALLOW_STALE_SEARCH'
    stale=service.query(task['principal'],req)
    assert stale['freshness']=='STALE' and stale['payload']['coverage']=='PARTIAL'
    exact=service.query(task['principal'],request(task))
    assert exact['freshness']=='CURRENT' and exact['payload']['state_items'][1]['answer']['knowledge_status']=='KNOWN'


def test_future_plan_is_fixed_artifact_and_never_world_fact(query_env):
    world=seed(query_env);task=start(query_env,world);kernel=query_env['kernel'];service=query_env['query'];ids=world['ids']
    text=kernel.put_artifact(world['principal'],world['world_ref']['work_id'],b'D50 return the sword','TEXT')
    plan={'schema_version':'p1','plans':[{'plan_id':uid(),'plan_ref':text,'status':'APPROVED','target_time':{'kind':'EXACT','at':p(50)},
          'subject_ids':[ids['sword']],'summary':'D50归还青霜剑','requires_active':[ids['sword']],'fulfillment_event_refs':[]}]}
    manifest=kernel.put_artifact(world['principal'],world['world_ref']['work_id'],plan,'PLAN_MANIFEST')
    req=request(task,at=p(50),operation={'kind':'TIMELINE','subject_ids':[ids['sword']],'entry_kinds':['PLAN'],'time_axis':'TARGET','include_inactive':True})
    req['knowledge_cut']={'plane':'PLAN','plan_manifest_ref':manifest}
    result=service.query(task['principal'],req)
    assert result['payload']['entries'][0]['source']['plan_status']=='APPROVED'
    assert result['payload']['entries'][0]['entry_kind']=='PLAN'
    assert result['read_cut']['source_manifest_refs']==[manifest]
    world_future=service.query(task['principal'],request(task,at=p(50)))
    assert all(item['answer']['evaluation_status']=='PENDING' for item in world_future['payload']['state_items'])


def test_cancelled_task_cannot_read_or_compile(query_env):
    world=seed(query_env);task=start(query_env,world,at=p(35));query_env['kernel'].cancel_task(task['principal'],task['task']['task_id'],task['task']['generation'],'cancel-query')
    with pytest.raises(DomainError):query_env['query'].query(task['principal'],request(task))
    with pytest.raises(DomainError):query_env['query'].context_compile(task['principal'],compile_request(task))


def test_concurrent_context_retry_creates_one_binding(query_env):
    world=seed(query_env);task=start(query_env,world,at=p(35));req=compile_request(task);service=query_env['query']
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:service.context_compile(task['principal'],req),range(2)))
    assert results[0]==results[1]
    with query_env['db'].uow(write=False) as repo:
        assert len(repo.many('contexts',{'task_id':task['task']['task_id']}))==1


def test_query_json_contract_rejects_scope_smuggling(query_env):
    world=seed(query_env);task=start(query_env,world);req=request(task);req['role_id']='control_operator'
    with pytest.raises(DomainError) as raised:query_env['query'].query(task['principal'],req)
    assert raised.value.code=='INVALID_ARGUMENT'
