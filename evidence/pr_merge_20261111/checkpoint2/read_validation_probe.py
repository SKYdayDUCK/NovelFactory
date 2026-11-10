"""Diagnostic candidates only. No production module files are modified."""
import cProfile
from collections import OrderedDict
from contextlib import contextmanager
from copy import deepcopy
import json
import os
from pathlib import Path
import pstats
import statistics
import sys
import tempfile
import time

from benchmarks.context_paired_profile import world_identity
from benchmarks.p2_behavioral_load import make_population
from novel_factory.application.kernel import Kernel
from novel_factory.application.queries import WorldQueryService
from novel_factory.domain import canonical as c
from novel_factory.domain.errors import DomainError
from novel_factory.contracts import registry
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.objects import ObjectStore

ROOT=Path('/workspace/scratch/f6d7dc8c770b')
old_check=c._check
old_validate=registry.validate_contract

def fast_check(value):
    kind=type(value)
    if kind is str:
        try:value.encode('utf-8','strict')
        except UnicodeEncodeError as exc:raise DomainError('INVALID_ARGUMENT','Invalid Unicode') from exc
        return
    if value is None or kind is bool:return
    if kind is int:
        if abs(value)>c.MAX_SAFE_INTEGER:raise DomainError('INVALID_ARGUMENT','Integer outside the JSON safe range')
        return
    if kind is list:
        for item in value:fast_check(item)
        return
    if kind is dict:
        for key,item in value.items():
            if type(key) is not str:raise DomainError('INVALID_ARGUMENT','JSON object keys must be strings')
            try:key.encode('utf-8','strict')
            except UnicodeEncodeError as exc:raise DomainError('INVALID_ARGUMENT','Invalid Unicode') from exc
            fast_check(item)
        return
    raise DomainError('INVALID_ARGUMENT','Canonical JSON accepts no floats or custom objects')

memo=OrderedDict()
memo_hits=memo_misses=0
def memo_validate(name,payload,version='2.0'):
    global memo_hits,memo_misses
    value=registry.plain(payload)
    validator=registry._validator(name,version)
    try:
        encoded=c.canonical_bytes(value)
        key=(name,version,id(validator),encoded) if len(encoded)<=65536 else None
    except DomainError:key=None
    if key is not None and memo.get(key) is validator:
        memo.move_to_end(key);memo_hits+=1
    else:
        memo_misses+=1
        errors=sorted(validator.iter_errors(value),key=lambda e:str(list(e.path)))
        if errors:raise DomainError('INVALID_ARGUMENT',f'Invalid {name}: contract constraints failed')
        if key is not None:
            memo[key]=validator
            while len(memo)>4096:memo.popitem(last=False)
    return deepcopy(value)

@contextmanager
def arm(name):
    changed=[]
    if name in {'memo','combined'}:
        for module_name,module in list(sys.modules.items()):
            if module_name.startswith('novel_factory.') and module is not None:
                for key,value in list(vars(module).items()):
                    if value is old_validate:
                        changed.append((module,key,value));setattr(module,key,memo_validate)
    if name in {'check','combined'}:c._check=fast_check
    try:yield
    finally:
        c._check=old_check
        for module,key,value in changed:setattr(module,key,value)

def stable_response(response):
    result=deepcopy(response)
    del result['request_id']
    del result['read_cut']['resolved_at']
    del result['read_cut']['read_cut_id']
    return result

def profile(callback):
    p=cProfile.Profile();p.enable();result=callback();p.disable()
    rows=[{'file':Path(key[0]).name,'line':key[1],'function':key[2],'calls':v[1],'self_seconds':v[2],'cumulative_seconds':v[3]}
          for key,v in sorted(pstats.Stats(p).stats.items(),key=lambda x:x[1][3],reverse=True)[:60]]
    return result,rows

records=[]
db=Database(pool_size=4)
with tempfile.TemporaryDirectory(prefix='nf-validation-probe-') as tmp:
    kernel=Kernel(db,ObjectStore(Path(tmp)/'objects'))
    fixture=make_population(kernel,200,.8)
    actors=fixture['population'][50:55]
    service=WorldQueryService(kernel)
    before=world_identity(kernel,fixture['world'])
    reference=[stable_response(service.query(a['principal'],a['state'])) for a in actors]
    for repeat in range(8):
        order=['baseline','check','memo','combined']
        if repeat%2:order.reverse()
        for name in order:
            start=time.perf_counter();cpu=time.process_time()
            with arm(name):
                responses=[service.query(a['principal'],a['state']) for _ in range(2) for a in actors]
            elapsed=time.perf_counter()-start
            assert [stable_response(r) for r in responses]==reference*2
            records.append({'repeat':repeat,'arm':name,'wall_seconds_per_query':elapsed/len(responses),
                            'cpu_seconds_per_query':(time.process_time()-cpu)/len(responses),
                            'full_payload_and_evidence_equal':True})
    profiles=[]
    for name in ['baseline','check','memo','combined']:
        with arm(name):_,rows=profile(lambda:service.query(actors[0]['principal'],actors[0]['state']))
        profiles.append({'arm':name,'profile':rows})
    after=world_identity(kernel,fixture['world']);assert before==after
    summary={name:{key:statistics.median(r[key] for r in records if r['arm']==name)
                   for key in ['wall_seconds_per_query','cpu_seconds_per_query']}
             for name in ['baseline','check','memo','combined']}
    output={'kind':'REAL_POSTGRESQL_DIAGNOSTIC_SAME_FIXED_QUERY_INPUTS','affinity':sorted(os.sched_getaffinity(0)),
            'fixture_agents':200,'queries_per_arm':80,'records':records,'profiles':profiles,'summary':summary,
            'memo_hits':memo_hits,'memo_misses':memo_misses,'memo_entries':len(memo),
            'memo_payload_bytes':sum(len(key[-1]) for key in memo),
            'world_before':before,'world_after':after,
            'volatile_response_fields':['request_id','read_cut.resolved_at','read_cut.read_cut_id'],
            'limitation':'Warm repeated real application queries for five actors, same synthetic world; no TCP/admission contention or 1000 RPS capacity claim.'}
    (ROOT/'read_validation_probe.json').write_text(json.dumps(output,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(summary,indent=2),flush=True)
db.close()
