"""Sequential, read-only authentication calibration on an isolated PG17 load fixture."""
from collections import Counter
from contextlib import contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
import random
import statistics
import sys
from tempfile import TemporaryDirectory
import time
from unittest.mock import patch

from sqlalchemy import event, text
from benchmarks.p2_behavioral_load import isolated_kernel, make_population
from benchmarks.scion_profile import Attribution
from novel_factory.domain.errors import DomainError
from novel_factory.infrastructure.repository import KernelRepository

COLUMNS = ("grant_id", "principal_id", "active")


@contextmanager
def read_shape(arm):
    # Both arms use the production Kernel.authenticate and one identical shim.
    # Refuse to modify any other repository call or lookup predicate.
    original = KernelRepository.one
    def one(repo, name, where, *, lock=None, columns=None):
        assert name == "grants" and set(where) == {"token_hash"} and lock is columns is None
        return original(repo, name, where, lock=lock, columns=COLUMNS if arm == "projection" else None)
    with patch.object(KernelRepository, "one", one):
        yield


def errors(kernel, tokens):
    outcomes = []
    for token in tokens:
        try:
            kernel.authenticate(token)
        except DomainError as exc:
            outcomes.append((exc.code, exc.safe_message))
        else:
            raise AssertionError("invalid test credential accepted")
    return outcomes


def authenticate_all(kernel, population, order):
    for i in order:
        record=population[i]
        assert kernel.authenticate(record["token"]) == record["principal"]


def wire(kernel, population, arm):
    counts = Counter()
    def after(conn,cursor,statement,parameters,context,many):
        if not statement.upper().startswith("SELECT") or "nf_grants" not in statement:
            return
        result = cursor.pgresult
        counts['selects'] += 1
        counts['rows'] += result.ntuples
        counts['number_of_fields_per_row'] = result.nfields
        for row in range(result.ntuples):
            sizes = [len(result.get_value(row,col) or b'') for col in range(result.nfields)]
            counts['result_field_value_bytes'] += sum(sizes)
            # PostgreSQL DataRow type byte + length + column count + every field's length and raw value.
            counts['data_row_message_bytes'] += 7 + 4 * result.nfields + sum(sizes)
    event.listen(kernel.db.engine,'after_cursor_execute',after)
    try:
        with read_shape(arm):
            authenticate_all(kernel,population,range(len(population)))
    finally:
        event.remove(kernel.db.engine,'after_cursor_execute',after)
    assert counts['selects'] == counts['rows'] == len(population)
    return dict(counts)


def runner(output):
    root = Path.cwd()
    with TemporaryDirectory() as tmp, isolated_kernel(os.environ['NF_TEST_DATABASE_URL'],Path(tmp)) as (kernel,url):
        fixture = make_population(kernel,200,0.20)
        records = fixture['population']
        assert len({p['principal'] for p in records}) == len(records) == 200
        with kernel.db.engine.connect() as conn:
            pgver=conn.scalar(text('select version()'))
        result = {'server':pgver,'affinity':sorted(os.sched_getaffinity(0)), 'fixture':'make_population(kernel, 200, 0.20), generator default seed 20261110',
                  'processes':1,'concurrent_auth_calls':1,'operations_per_arm_per_repeat':len(records),
                  'warmup':'200 original + 200 projection before instrumented pairs; DB and pool warm',
                  'timing':'monotonic wall and process CPU; same pool & SQL observer, no retry or HTTP admission in this microbenchmark',
                  'size_definition':'libpq PGresult bytes and reconstructed DataRow framing; excludes RowDescription, network/TLS/TCP and other SQL commands',
                  'source_sha256':{str(p):sha256((root/p).read_bytes()).hexdigest() for p in map(Path,[
                      'src/novel_factory/application/kernel.py','src/novel_factory/infrastructure/repository.py',
                      'benchmarks/scion_profile.py','benchmarks/p2_behavioral_load.py'])},'samples':[]}
        for a in ['original','projection']:
            with read_shape(a): authenticate_all(kernel,records,range(len(records)))
        with Attribution(kernel) as meter:
            # Attribution replaces idle connections to register its codec. Warm both arms once more.
            for a in ['original','projection']:
                with read_shape(a): authenticate_all(kernel,records,range(len(records)))
            for i in range(10):
                order=list(range(len(records))); random.Random(2411+i).shuffle(order)
                for arm in (('original','projection') if i%2==0 else ('projection','original')):
                    meter.reset()
                    with read_shape(arm):
                        meter.active=True
                        wall,cpu=time.perf_counter(),time.process_time()
                        try: authenticate_all(kernel,records,order)
                        finally:
                            duration,used=time.perf_counter()-wall,time.process_time()-cpu
                            meter.active=False
                    sql=meter.snapshot()
                    result['samples'].append({'repeat':i+1,'arm':arm,'wall_ms':duration*1000,'cpu_ms':used*1000,
                       'SQL_count':sql['sql']['statements'],'SELECT_count':sql['sql']['statement_kinds']['SELECT'],
                       'result_rows':sql['sql']['selected_rows_reported_by_cursor'],'SQL_cursor_ms':sql['sql']['cursor_seconds']*1000,
                       'json_decoded_bytes':sql['database_json_codec'].get('decode',{}).get('bytes',0),
                       'json_decode_calls':sql['database_json_codec'].get('decode',{}).get('calls',0)})
        result['pg_result_size']={a:wire(kernel,records,a) for a in ('original','projection')}
        kernel.revoke_grant(fixture['world']['principal'],records[0]['principal'].grant_id)
        invalid = [records[0]['token'],'Q'*32,'short',None]
        failures={}
        for a in ('original','projection'):
            with read_shape(a):
                failures[a]=errors(kernel,invalid)
                authenticate_all(kernel,records,range(1,len(records)))
        assert failures['original'] == failures['projection']
        result['negative_cases']={'cases':['revoked','unknown','short','nonstring'],'public_errors':failures['original'],
                                  'unrevoked_identities_still_match':199}
        result['medians_per_call']={}
        for a in ('original','projection'):
            subset=[s for s in result['samples'] if s['arm']==a]
            result['medians_per_call'][a]={q:statistics.median(s[q] for s in subset)/len(records) for q in
                                           ('wall_ms','cpu_ms','SELECT_count','result_rows','json_decoded_bytes','SQL_cursor_ms')}
        result['benchmark_sha256']=sha256(Path(__file__).read_bytes()).hexdigest()
        output.write_text(json.dumps(result,indent=2)+'\n')
        print(json.dumps(result['medians_per_call']),flush=True)

if __name__ == '__main__':
    runner(Path(sys.argv[1]).resolve())
