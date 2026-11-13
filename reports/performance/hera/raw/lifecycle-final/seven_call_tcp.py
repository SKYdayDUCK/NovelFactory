"""Identical seven-call task lifecycles on frozen checkouts, on an isolated PG17 database.

Run this file by absolute path with PYTHONPATH pointing only at the checkout
under test. --schema-root selects the shared Alembic schema for all arms.
PostgreSQL must preload pg_stat_statements; set track_functions=pl and
pg_stat_statements.track=all. Only a freshly created benchmark DB is modified.
"""
import argparse
import asyncio
from contextlib import AsyncExitStack, contextmanager
from datetime import UTC, datetime, timedelta
import gzip
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import resource
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
from time import perf_counter, process_time
from uuid import uuid4

import httpx
import psycopg
from sqlalchemy import event, text
from sqlalchemy.engine import make_url

from standard_http_load import loopback_server, percentiles
from novel_factory.application.kernel import Kernel
from novel_factory.fixtures import load_fixture
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.objects import ObjectStore
from novel_factory.interfaces import http


def revision(path):
    return subprocess.check_output(['git', '-C', path, 'rev-parse', 'HEAD'], text=True).strip()


def pg_connect(url):
    dsn = make_url(url).set(drivername='postgresql').render_as_string(hide_password=False)
    return psycopg.connect(dsn, autocommit=True)


def pin(cpus):
    if cpus:
        os.sched_setaffinity(0, {int(cpu) for cpu in cpus.split(',')})


@contextmanager
def fresh_database(base, schema):
    name = 'nf_hera_trial_' + uuid4().hex
    url = make_url(base).set(database=name).render_as_string(hide_password=False)
    with pg_connect(base) as admin:
        admin.execute('CREATE DATABASE ' + name)
        try:
            env = {**os.environ, 'NF_DATABASE_URL': url, 'DATABASE_URL': url,
                   'PYTHONPATH': os.pathsep.join(str(schema / p) for p in ('src', 'workshop/src', '.'))}
            subprocess.run([sys.executable, '-m', 'alembic', 'upgrade', 'head'],
                           cwd=schema, env=env, check=True)
            with pg_connect(url) as ctl:
                ctl.execute('CREATE EXTENSION IF NOT EXISTS pg_stat_statements')
            yield url
        finally:
            admin.execute('DROP DATABASE ' + name + ' WITH (FORCE)')


def server(channel, url, objects, settings, cpus, output):
    pin(cpus)
    db = Database(url, pool_size=4, max_overflow=0)
    kernel = Kernel(db, ObjectStore(objects))
    app = http.create_app(kernel, settings=http.HttpSettings(**settings))
    assert app.state.admission.capacity == app.state.authentication_admission.capacity == 2
    sql, transactions, queues, catalog = [], [], [], {}
    epoch = perf_counter()
    def before(conn, cursor, statement, parameters, ctx, many):
        ctx.hera_t0 = perf_counter() - epoch
    def after(conn, cursor, statement, parameters, ctx, many):
        key = catalog.setdefault(statement, len(catalog))
        sql.append([id(conn), key, ctx.hera_t0, perf_counter() - epoch])
    def begin(conn):
        conn.info['hera_t0'] = perf_counter() - epoch
    def finish(conn):
        transactions.append([id(conn), conn.info.pop('hera_t0', None), perf_counter() - epoch])
    for name, method in [('before_cursor_execute', before), ('after_cursor_execute', after),
                         ('begin', begin), ('commit', finish), ('rollback', finish)]:
        event.listen(db.engine, name, method)
    from contextlib import asynccontextmanager
    def observe(controller, label):
        original = controller.slot
        @asynccontextmanager
        async def timed(key, kind='default'):
            start = perf_counter() - epoch
            async with original(key, kind):
                queues.append([label, kind, start, perf_counter() - epoch])
                yield
        controller.slot = timed
    for label in ('ingress','admission','authentication_admission'):
        observe(getattr(app.state, label), label)
    try:
        with loopback_server(app) as address:
            channel.send((address, os.getpid(), sorted(os.sched_getaffinity(0)), epoch))
            while True:
                action = channel.recv()
                if action == 'stop':
                    break
                channel.send([process_time(), resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                              db.engine.pool.checkedout(), len(sql), len(transactions), len(queues)])
    finally:
        db.close()
        with gzip.open(output, 'wt') as f:
            json.dump(dict(statements=list(catalog), sql=sql, transaction_precommit=transactions,
                           queue_waits=queues, clock_origin=epoch), f, separators=(',',':'))
        channel.close()


async def workload(base, fixtures, phase, rounds):
    rows, cycles = [], []
    gate = asyncio.Event()
    deadline = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    async def lane(client, actor, wr, index):
        await gate.wait()
        for turn in range(rounds):
            tid = str(uuid4())
            spec = dict(schema_version='2.0', task_id=tid, generation=0, role_id='writer',
                        task_kind='hera-comparable', state='READY', world_ref=wr,
                        story_time={'tick':0,'phase':0}, context_bundle_id=None, policy_version='nf-policy-1',
                        dependency_fingerprint=None, runtime_contract_version='1'*64,
                        capability_grant_id=actor['principal'].grant_id, input_artifacts=[],
                        budget={'currency':'USD','max_amount':'1','token_budget':10}, wall_deadline=deadline)
            create = {'spec':spec,'idempotency_key':'create-'+tid}
            status_path = '/api/v1/tasks/'+tid
            ops = [('create','/api/v1/tasks',create), ('ready',status_path,None), ('replay','/api/v1/tasks',create),
                   ('claim','/api/v1/tasks/claim',{'task_id':tid,'worker_id':'w-'+tid,'lease_seconds':300}),
                   ('running',status_path,None), ('cancel','/api/v1/tasks/cancel',{'task_id':tid,
                    'generation':1,'idempotency_key':'cancel-'+tid}), ('cancelled',status_path,None)]
            started, ok, original = perf_counter(), True, None
            for op, path, payload in ops:
                t0 = perf_counter()
                try:
                    response = await client.get(path) if payload is None else await client.post(path,json=payload)
                    body = response.json()
                    code = response.status_code
                    good = code == 200 and body.get('task_id') == tid
                    if op == 'create':
                        original = body
                        good = good and body['generation'] == 0 and body['state'] == 'READY'
                    elif op == 'replay':
                        good = good and body == original
                    elif op == 'cancel':
                        good = good and body['generation'] == 1 and body['status'] == 'CANCELLED'
                    else:
                        expected = {'ready':'READY','claim':'RUNNING','running':'RUNNING','cancelled':'CANCELLED'}[op]
                        good = good and body['state'] == expected and body['world_ref'] == wr and body['generation'] == int(op != 'ready')
                except Exception as error:
                    code, good = type(error).__name__, False
                rows.append([index,turn,tid,op,code,good,t0-epoch,perf_counter()-epoch])
                ok = ok and good
            cycles.append([tid,ok,started-epoch,perf_counter()-epoch])
    async with AsyncExitStack() as stack:
        clients = [await stack.enter_async_context(httpx.AsyncClient(base_url=base, trust_env=False, timeout=60,
                   headers={'Authorization':'Bearer '+actor['token']})) for actor, wr in fixtures]
        async with asyncio.TaskGroup() as group:
            for i, ((actor,wr),client) in enumerate(zip(fixtures,clients)):
                group.create_task(lane(client,actor,wr,i))
            cpu = process_time()
            epoch = perf_counter()
            gate.set()
        cpu = process_time()-cpu
    elapsed = max(row[-1] for row in cycles)
    return dict(phase=phase, clock_origin=epoch, client_cpu_s=cpu, seconds=elapsed,
                goodput=sum(row[1] for row in cycles)/elapsed,
                latency=percentiles([row[-1]-row[-2] for row in rows]), rows=rows, cycles=cycles)


def observe_database(url, stop, samples):
    with pg_connect(url) as con:
        while not stop.is_set():
            rows = con.execute("""SELECT pid, state, wait_event_type, wait_event, pg_blocking_pids(pid)
                    FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid()
                    AND application_name<>'hera-observer'""").fetchall()
            activity = []
            for pid, state, kind, wait, blockers in rows:
                try:
                    stat = Path(f'/proc/{pid}/stat').read_text().split()
                    cpu_ticks, rss_pages = int(stat[13])+int(stat[14]), int(stat[23])
                except (OSError,IndexError):
                    cpu_ticks = rss_pages = None
                activity.append([pid,state,kind,wait,blockers,cpu_ticks,rss_pages])
            samples.append([perf_counter(),activity])
            stop.wait(.05)


def bench(args):
    root = Path.cwd()
    assert Path(http.__file__).resolve().is_relative_to(root/'src')
    schema = args.schema_root.resolve()
    if subprocess.check_output(['git','status','--porcelain','--untracked-files=no']) or revision(root) != args.revision:
        raise RuntimeError('Product must equal the requested clean commit')
    pin(args.client_cpus)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory() as tmp, fresh_database(os.environ['NF_TEST_DATABASE_URL'], schema) as url:
        db = Database(url,pool_size=4)
        kernel = Kernel(db,ObjectStore(tmp))
        books = [load_fixture(kernel),load_fixture(kernel)]
        fixtures = []
        for i in range(args.lanes):
            book = books[i%2]
            wr = book['world_ref']
            fixtures.append((kernel.issue_grant(book['principal'],wr['work_id'],role_id='writer',branch_ids=[wr['branch_id']]),wr))
        db.close()
        settings = dict(max_inflight_requests=128,max_queued_operations=128,rate_per_second=2000,
                        rate_burst=2000,queue_timeout_seconds=60)
        if args.product != 'main':
            settings['fenced_task_status'] = True
        with pg_connect(url) as ctl:
            ctl.execute("SET application_name='hera-observer'")
            if args.quota:
                ctl.execute("UPDATE nf_durable_admission_policy SET enabled=true")
            if args.exact_quota:
                ctl.execute("SELECT nf_hera_set_capacity_mode(true)")
            if args.trigger == 'suspend':
                from novel_factory.orchestration.progress import set_progress_maintenance
                with db.uow() as repo:
                    set_progress_maintenance(repo.conn, suspended=True)
                db.close()
            elif args.trigger == 'off':
                ctl.execute('ALTER TABLE nf_tasks DISABLE TRIGGER nf_hera_progress_delta')
            elif args.trigger == '0025':
                import importlib.util
                path = schema/'migrations/versions/0026_hera_progress_recovery.py'
                spec = importlib.util.spec_from_file_location('freeze_trigger',path)
                mig = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mig)
                ctl.execute('CREATE OR REPLACE FUNCTION nf_hera_count_transition() RETURNS trigger '
                            'LANGUAGE plpgsql AS $$ BEGIN '+mig._DELTA+' END $$')
            parent, child = multiprocessing.get_context('spawn').Pipe()
            trace = args.output.with_suffix('.trace.gz')
            proc = multiprocessing.get_context('spawn').Process(target=server,args=(child,url,tmp,settings,args.server_cpus,trace))
            started = perf_counter()
            proc.start()
            child.close()
            try:
                if not parent.poll(70):
                    raise RuntimeError('Service did not start')
                base,pid,affinity,service_clock = parent.recv()
                startup = perf_counter()-started
                pg_info = {k:ctl.execute('SHOW '+k).fetchone()[0] for k in (
                    'server_version','track_functions','pg_stat_statements.track','shared_buffers','track_io_timing','fsync')}
                postmaster = int(ctl.execute("SELECT split_part(pg_read_file('postmaster.pid'),E'\\n',1)").fetchone()[0])
                result = dict(sha=args.revision, schema=revision(schema), product=args.product, trigger=args.trigger,
                       quota=args.quota, exact_quota=args.exact_quota,
                       flags={key:os.environ.get(key,"0") for key in ("NF_HERA_COMPACT_CREATE","NF_HERA_REUSE_LOOKUP_SQL")},
                       startup_s=startup, service_pid=pid, client_pid=os.getpid(), pg_pid=postmaster,
                       client_cpus=sorted(os.sched_getaffinity(0)), server_cpus=affinity, pg_cpus=sorted(os.sched_getaffinity(postmaster)),
                       postgres=pg_info, pool=4, overflow=0, auth_slots=2, operation_slots=2, model_slots_used=0,
                       driver_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), phases=[])
                for name, rounds in [('cold',1),('warm',args.rounds)]:
                    oid = ctl.execute("SELECT oid FROM pg_database WHERE datname=current_database()").fetchone()[0]
                    ctl.execute('SELECT pg_stat_statements_reset(0,%s,0)',(oid,))
                    lsn = ctl.execute('SELECT pg_current_wal_insert_lsn()').fetchone()[0]
                    samples, stop = [], threading.Event()
                    sampler = threading.Thread(target=observe_database,args=(url,stop,samples))
                    sampler.start()
                    try:
                        parent.send('snapshot')
                        before = parent.recv()
                        phase = asyncio.run(workload(base,fixtures,name,rounds))
                        parent.send('snapshot')
                        after = parent.recv()
                    finally:
                        stop.set()
                        sampler.join()
                    phase.update(server_before=before,server_after=after,db_activity=samples,
                        wal_bytes=int(ctl.execute('SELECT pg_wal_lsn_diff(pg_current_wal_insert_lsn(),%s)',(lsn,)).fetchone()[0]),
                        pg_stat_statements=ctl.execute("""SELECT queryid,query,calls,total_exec_time,rows,shared_blks_hit,
                            shared_blks_read,shared_blk_read_time,shared_blk_write_time,wal_bytes FROM pg_stat_statements
                            WHERE dbid=%s ORDER BY total_exec_time DESC""",(oid,)).fetchall())
                    result['phases'].append(phase)
            finally:
                if proc.is_alive():
                    parent.send('stop')
                proc.join(20)
                if proc.is_alive():
                    proc.kill()
                    proc.join()
                parent.close()
            assert proc.exitcode == 0
            result['functions'] = ctl.execute('SELECT funcname,calls,total_time,self_time FROM pg_stat_user_functions').fetchall()
            result['oracle'] = ctl.execute("""SELECT t.task_id,t.state,t.generation,
                 (SELECT count(*) FROM nf_task_executions e WHERE e.task_id=t.task_id),
                 (SELECT count(*) FROM nf_outbox o WHERE o.aggregate_id=t.task_id::text AND o.topic='task.changed'),
                 (SELECT count(*) FROM nf_task_commands c WHERE c.receipt->>'task_id'=t.task_id::text)
                 FROM nf_tasks t""").fetchall()
            result['attempts'] = ctl.execute('SELECT count(*) FROM nf_call_attempts').fetchone()[0]
            with db.uow(write=False) as repo:
                result['world_heads_unchanged'] = all(kernel.head(repo,b['world_ref']['work_id'],b['world_ref']['branch_id']) == b['world_ref'] for b in books)
            db.close()
            expected = {r[0] for phase in result['phases'] for r in phase['cycles']}
            ok = (len(expected)==args.lanes*(1+args.rounds) and expected=={str(r[0]) for r in result['oracle']}
                  and all(tuple(r[1:])==('CANCELLED',1,1,3,2) for r in result['oracle'])
                  and all(r[1] for phase in result['phases'] for r in phase['cycles'])
                  and result['world_heads_unchanged'] and result['attempts']==0)
            result['verified'] = ok
            with gzip.open(args.output,'wt') as out:
                json.dump(result,out,default=str,separators=(',',':'))
            print(args.output.name, ok, [round(p['goodput'],2) for p in result['phases']], flush=True)
            if not ok:
                raise RuntimeError('Lifecycle or database oracle failed')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--product',choices=['main','early','prior','candidate'],required=True)
    parser.add_argument('--revision',required=True)
    parser.add_argument('--quota',action='store_true')
    parser.add_argument('--exact-quota',action='store_true')
    parser.add_argument('--trigger',choices=['off','0025','installed','suspend'],default='installed')
    parser.add_argument('--schema-root',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--server-cpus',default='')
    parser.add_argument('--client-cpus',default='')
    parser.add_argument('--rounds',type=int,default=4)
    parser.add_argument('--lanes',type=int,default=16)
    bench(parser.parse_args())
