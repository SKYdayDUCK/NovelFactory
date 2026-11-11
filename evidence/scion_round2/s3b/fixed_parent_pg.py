"""Repeated fixed-parent proposals; isolated PG17 with restored managed checkpoints."""
import gzip
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
from unittest.mock import patch
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from benchmarks.scion_long_history import sha, transfer
from benchmarks.scion_paired import clone, drop
from benchmarks.scion_profile import Attribution, setup_task
from novel_factory.application.authorization import Principal
from novel_factory.application.kernel import Kernel
from novel_factory.application.queries import WorldQueryService
from novel_factory.domain.canonical import fingerprint
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.objects import ObjectStore


def kernel_for(url, store):
    return Kernel(Database(url.render_as_string(hide_password=False),pool_size=4),ObjectStore(store))


def once(url, objects, fixture, tasks, arm, repeat):
    kernel=kernel_for(url,objects); options=kernel.scion
    options.replay=options.replay_lazy=True
    options.replay_prime=arm=='P'
    wr=fixture['world_ref']; caller=Principal(**fixture['principal'])
    action=transfer(fixture['ids'],fixture['count']+1)
    uid=iter(uuid5(NAMESPACE_URL,f"nf-prime-measure/{idx}") for idx in range(2000))
    docs=[]; outputs=[]
    try:
        with Attribution(kernel) as meter, patch('novel_factory.application.kernel.uuid4',lambda:next(uid)):
            for idx,task in enumerate(tasks):
                meter.reset(); before=options.counts()
                meter.active=True
                start,cpu=time.perf_counter(),time.process_time()
                try:
                    out=kernel.propose(caller,task['task_id'],task['generation'],'RESOURCE_TRANSFER',action,f'fixed-parent:{idx}')
                finally:
                    wall,used=time.perf_counter()-start,time.process_time()-cpu
                    meter.active=False
                after=options.counts(); stats=meter.snapshot()
                outputs.append(fingerprint(out))
                docs.append({'proposal':idx+1,'wall_ms':wall*1000,'cpu_ms':used*1000,'sql':stats['sql']['statements'],
                             'full_replays':stats['replay_event_counts'],'json_decode_bytes':stats['database_json_codec']['decode']['bytes'],
                             'scion_delta':{k:n-before.get(k,0) for k,n in after.items() if n>before.get(k,0)}})
        with kernel.db.uow(write=False) as repo:
            assert kernel.head(repo,wr['work_id'])==wr
            assert kernel.revision(repo,wr)['state_hash']==fixture['state_hash']
        return {'arm':arm,'repeat':repeat,'rows':docs,'output_sha':outputs}
    finally:kernel.db.close()


def bench(seed,counts,out):
    url=make_url(os.environ['NF_TEST_DATABASE_URL'])
    admin=create_engine(url.set(database='postgres'),isolation_level='AUTOCOMMIT')
    result={'schema':1,'control':'S3 lazy and replay enabled in both arms; only prime changes',
            'affinity':sorted(os.sched_getaffinity(0)),'seed_dumps':{},'samples':{},'pairs_equal':0,
            'solver_sha256':sha(Path('src/novel_factory/application/replay_proofs.py')),
            'script_sha256':sha(Path(__file__))}
    for n in counts:
        fix=json.loads((seed/f'{n}.json').read_text());dump=seed/f'{n}.dump'
        assert sha(dump)==fix['checkpoint_sha256'];result['seed_dumps'][n]=sha(dump)
        templ='nf_prime_'+uuid4().hex
        subprocess.run(['createdb',templ],check=True)
        try:
            subprocess.run(['pg_restore','--no-owner','--exit-on-error','--dbname',templ,str(dump)],check=True)
            with TemporaryDirectory() as objects:
                objs=Path(objects)/'template';objs.mkdir()
                k=kernel_for(url.set(database=templ),objs)
                try:
                    with k.db.engine.connect() as conn:
                        hashes=conn.execute(text('select hash from nf_objects')).scalars().all()
                    for digest in hashes:
                        relative=Path('sha256')/digest[:2]/digest[2:];src=seed/'seed_objects'/relative
                        assert sha(src)==digest;dest=objs/relative;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(src,dest)
                    ids=fix['ids'];world={'world_ref':fix['world_ref'],'principal':Principal(**fix['principal'])}
                    tasks=[]
                    for idx in range(6):
                        req=setup_task(k,world,[ids['left'],ids['right']],{'tick':n+1,'phase':0},[f"resource:{ids['grain']}:bag"])
                        assert WorldQueryService(k).context_compile(world['principal'],req)['status']=='READY'
                        tasks.append(req)
                finally:k.db.close()
                samples=[]
                for rep in range(1,4):
                    pair=[]
                    for arm in ('EP' if rep%2 else 'PE'):
                        name='nf_prime_'+uuid4().hex
                        clone(admin,name,templ)
                        try:
                            copied=shutil.copytree(objs,Path(objects)/name,copy_function=os.link)
                            sample=once(url.set(database=name),copied,fix,tasks,arm,rep)
                            pair.append(sample['output_sha']);samples.append(sample)
                            print(n,rep,arm,round(sum(r['wall_ms'] for r in sample['rows']),2),flush=True)
                        finally:drop(admin,name)
                    assert pair[0]==pair[1];result['pairs_equal']+=1
                result['samples'][n]=samples
                out.write_text(json.dumps(result,indent=2)+'\n')
        finally:drop(admin,templ)
    admin.dispose()

if __name__=='__main__':bench(Path(sys.argv[1]),[20,1000],Path(sys.argv[2]))
