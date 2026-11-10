from copy import deepcopy

import pytest

from novel_factory.fixtures import load_fixture, seed_sword_history, create_bound_task, run_command, point
from novel_factory.domain.errors import DomainError
from novel_factory.domain.propositions import encode_proposition


def query(task, wr, subjects, predicates, day, *, plane='WORLD', actor=None):
    cut={'plane':plane}
    if plane=='BELIEF':cut.update(actor_id=actor,known_at=point(day))
    return {'schema_version':'2.1','task_id':task['task_id'],'generation':task['generation'],
            'read_contract':{'revision':{'mode':'FIXED','world_ref':wr},'process_policy':'ALLOW_PARTIAL','index_policy':'AUTHORITATIVE','max_wait_ms':0},
            'knowledge_cut':cut,'subject_time':{'kind':'POINT','at':point(day)},
            'query':{'kind':'STATE','subject_ids':subjects,'predicates':predicates},'page_size':100,'cursor':None}


def test_actual_sword_acceptance_history_and_ryw(query_env):
    k=query_env['kernel'];q=query_env['query'];f=seed_sword_history(k,load_fixture(k));ids=f['ids'];principal=f['principal'];wr=f['world_ref']
    task=create_bound_task(k,principal,wr,point(60),[ids['sword']])
    expected=[(5,'smith','smith'),(15,'protagonist','protagonist'),(25,'protagonist','friend'),(35,'protagonist','rival')]
    for day,owner,holder in expected:
        result=q.query(principal,query(task,wr,[ids['sword']],['owner','active_holder'],day))
        answers=result['payload']['state_items']
        assert answers[0]['answer']['data']['entity_id']==ids[owner]
        assert answers[1]['answer']['data']['entity_id']==ids[holder]
    destroyed=q.query(principal,query(task,wr,[ids['sword']],['last_confirmed_owner','active_holder','active_use'],41))
    assert destroyed['payload']['state_items'][1]['answer']['value_status']=='NONE'
    assert destroyed['payload']['state_items'][2]['answer']['value_status']=='NOT_APPLICABLE'
    belief=q.query(principal,query(task,wr,[ids['sword']],['active_holder'],35,plane='BELIEF',actor=ids['protagonist']))
    assert belief['payload']['state_items'][0]['answer']['data']['entity_id']==ids['friend']
    receipt=f['steps'][-1]['receipt']
    receipt_ref=k.put_artifact(principal,wr['work_id'],receipt,'EVIDENCE')
    request=query(task,wr,[ids['sword']],['active_holder'],41)
    request['read_contract']['revision']={'mode':'READ_YOUR_WRITES','branch_selector':{'kind':'CANON','work_id':wr['work_id']},'min_receipt_ref':receipt_ref}
    result=q.query(principal,request)
    assert result['read_cut']['world_ref']==wr
    forged=deepcopy(receipt);forged['command_id']=f['loan']['receipt']['command_id']
    fake=k.put_artifact(principal,wr['work_id'],forged,'EVIDENCE')
    request['read_contract']['revision']['min_receipt_ref']=fake
    with pytest.raises(DomainError) as raised:q.query(principal,request)
    assert raised.value.code=='INVALID_ARGUMENT'


def test_fixed_latest_batch_and_compiler_rebind(query_env):
    k=query_env['kernel'];q=query_env['query'];f=load_fixture(k);ids=f['ids'];principal=f['principal'];old=f['world_ref']
    task=create_bound_task(k,principal,old,point(60),[ids['sword']])
    result=run_command(k,principal,old,'TRANSFER_ITEM',{'item_id':ids['sword'],'from_id':ids['smith'],'to_id':ids['friend'],'transfer_kind':'GIFT','at':point(10)})
    new=result['world_ref']
    new=run_command(k,principal,new,'ADVANCE_SCOPE',{'scope_id':ids['sword'],'target':point(42)})['world_ref']
    request=query(task,old,[ids['sword']],['owner'],15)
    assert q.query(principal,request)['payload']['state_items'][0]['answer']['evaluation_status']=='PENDING'
    request['read_contract']['revision']={'mode':'LATEST_AT_START','branch_selector':{'kind':'CANON','work_id':old['work_id']}}
    latest=q.query(principal,request)
    assert latest['read_cut']['world_ref']==new and latest['payload']['state_items'][0]['answer']['data']['entity_id']==ids['friend']
    batch={key:request[key] for key in ('schema_version','task_id','generation','read_contract','knowledge_cut')}
    batch['items']=[{'item_id':str(i),'subject_time':{'kind':'POINT','at':point(15+i)},'query':request['query'],'page_size':100} for i in range(3)]
    result=q.batch(principal,batch)
    assert result['read_cut']['world_ref']==new
    assert all(item['payload']['state_items'][0]['answer']['world_ref']==new for item in result['items'])
    compile_req={'schema_version':'2.1','task_id':task['task_id'],'generation':task['generation'],'recipe_id':'ADJUDICATOR',
                 'input_manifest_ref':task['manifest_ref'],'read_contract':request['read_contract'],'knowledge_cut':{'plane':'WORLD'},
                 'subject_time':{'kind':'POINT','at':point(60)},'token_budget':50000,'idempotency_key':'rebind-new-cut'}
    with pytest.raises(DomainError) as raised:q.context_compile(principal,compile_req)
    assert raised.value.code=='TASK_REBIND_REQUIRED'


def test_pagination_pins_world_and_rejects_changed_query(query_env):
    k=query_env['kernel'];q=query_env['query'];f=seed_sword_history(k,load_fixture(k));ids=f['ids'];wr=f['world_ref'];principal=f['principal']
    task=create_bound_task(k,principal,wr,point(60),[ids['sword']])
    request=query(task,wr,[ids['sword']],['owner'],35)
    request['subject_time']={'kind':'INTERVAL','interval':{'start':point(1),'end':point(50),'bounds':'[)'}}
    request['query']={'kind':'TIMELINE','subject_ids':[ids['sword']],'entry_kinds':['FACT'],'time_axis':'SOURCE','include_inactive':True}
    request['page_size']=2
    first=q.query(principal,request)
    assert len(first['payload']['entries'])==2
    request['cursor']=first['payload']['next_cursor']
    second=q.query(principal,request)
    assert second['read_cut']['world_ref']==wr
    assert {e['entry_id'] for e in first['payload']['entries']}.isdisjoint(e['entry_id'] for e in second['payload']['entries'])
    request['query']['subject_ids']=[ids['sword2']]
    with pytest.raises(DomainError):q.query(principal,request)


def test_formal_late_knowledge_preserves_target_time_and_supports_retrospection(query_env):
    k=query_env['kernel'];q=query_env['query'];f=seed_sword_history(k,load_fixture(k))
    ids=f['ids'];principal=f['principal'];wr=f['world_ref']
    evidence={'kind':'EVENT','world_ref':f['theft']['world_ref'],
              'event_ref':f['theft']['receipt']['accepted_event_refs'][0]}
    value={'kind':'ENTITY','entity_id':ids['rival']}
    common={'actor_id':ids['protagonist'],'record_kind':'BELIEF','epistemic_mode':'HEARD','evidence_refs':[evidence]}
    present=run_command(k,principal,wr,'RECORD_COGNITION',{
        **common,'statement':encode_proposition(ids['sword'],'possessed_by',value),'at':point(50)})
    retrospective=run_command(k,principal,present['world_ref'],'RECORD_COGNITION',{
        **common,'statement':encode_proposition(ids['sword'],'possessed_by',value,{'kind':'POINT','at':point(31)}),'at':point(51)})
    wr=retrospective['world_ref'];task=create_bound_task(k,principal,wr,point(60),[ids['sword']])
    def holder(target,known):
        req=query(task,wr,[ids['sword']],['active_holder'],target,plane='BELIEF',actor=ids['protagonist'])
        req['knowledge_cut']['known_at']=point(known)
        return q.query(principal,req)['payload']['state_items'][0]['answer']['data']['entity_id']
    assert holder(25,50)==ids['friend']
    assert holder(31,35)==ids['friend']
    assert holder(31,51)==ids['rival']
