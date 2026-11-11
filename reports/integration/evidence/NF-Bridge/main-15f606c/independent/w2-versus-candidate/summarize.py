from pathlib import Path
from collections import Counter
import json
import hashlib
import gzip
import shutil
import subprocess
import sys
root = Path(sys.argv[1])
dst=root/"release-evidence"
dst.mkdir(exist_ok=True)
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def read_json(p):return json.loads(p.read_bytes())
def archive_raw(src,target):
    # gzip timestamp set to zero, preserving the exact original JSON bytes.
    target.write_bytes(gzip.compress(src.read_bytes(),mtime=0))
results={}
traces={}
for arm in ("w2","nfbridge"):
    p=root/arm
    d=read_json(p/'result.json')
    db=d["service_metrics"]["database"]
    status=d["status_counts"]
    assert d["scheduled_requests"]==5000 and len(d["raw_requests"])==5000 and not d["environment"]["git_dirty"]
    results[arm]={k:d[k] for k in ('network_window_seconds','client_validation_seconds','scheduled_requests','completed_requests','wire_started_requests','validated_successes','dispatch_start_rps','observed_wire_start_rps','successful_query_rps','latency_all_attempts','status_counts','offered_wire_rate_gate','capacity_gate','world_unchanged','new_world_commits','real_model_calls')}
    results[arm].update(tree=(p/'source.txt').read_text().splitlines()[1],commit=d['environment']['git_sha'],start=(p/'start-utc.txt').read_text().strip(),finished=(p/'finish-utc.txt').read_text().strip(),host_load_start=(p/'start-load.txt').read_text().strip(),host_load_finish=(p/'finish-load.txt').read_text().strip(),correctness_errors=len(d['correctness_errors']),
        core_sql={k:db[k] for k in ('sql_statements','transactions','failed_statements','statement_kinds','sql_cursor_execution_seconds','configured_pool_size')},
        preexisting_429_counts={k:v for k,v in Counter(row.get('code') for row in d['raw_requests'] if row.get('status')==429).items()},transport_errors_by_type=dict(Counter(row.get('error_type') for row in d['raw_requests'] if row.get('status')=='transport_error')),
        raw_result_sha256=sha(p/'result.json'),environment=d['environment'],original_exit=int((p/'exit.txt').read_text()))
    target=dst/arm
    target.mkdir(exist_ok=True)
    archive_raw(p/'result.json',target/'original-result.json.gz')
    for s,n in [('pg/postgres_session.json','postgres.json'),('benchmark.log','original-run.log'),('query-trace.json','query-trace.json'),('diagnostic-pg/postgres_session.json','diagnostic-postgres.json')]:shutil.copyfile(p/s,target/n)
    traces[arm]=read_json(p/'query-trace.json')
for n in ['code-mapping.json','mapping.py','request-sql.py','run-pair.sh','run-sql.sh','summarize.py']:shutil.copyfile(root/n,dst/n)
comparison={}
for sample in traces['w2']['sample_calls']:
    rows={a:traces[a]['sample_calls'][sample] for a in results}
    comparison[sample]={'ordered_sql_identical':rows['w2']['ordered_sql_shape_sha256']==rows['nfbridge']['ordered_sql_shape_sha256'],
        'statement_count':{a:len(rows[a]['ordered_sql_shape_sha256']) for a in rows},
        'delegation_table_hits':{a:sum(any(t.startswith('nf_delegation_') for t in traces[a]['shape_tables'][h]) for h in rows[a]['ordered_sql_shape_sha256']) for a in rows},'validated':{a:rows[a]['validated'] for a in rows}}
report={'workflow':'.github/workflows/p3-scale-experiments.yml Measured 1000-read-open, original targets retained', 'ordered_arms':['w2','nfbridge'],'raw_compressed_with_lossless_gzip':True,'results':results,'candidate_minus_baseline':{k:results['nfbridge'][k]-results['w2'][k] for k in ('validated_successes','observed_wire_start_rps','successful_query_rps','network_window_seconds')},'separate_request_shape_probe':comparison,
    'limitations':['One baseline-then-candidate pair on a shared host. The host and affinity were the same. Concurrent activity elsewhere, warm filesystem caches and run order were not controlled. The fixture uses deterministic subject construction but fresh UUIDs, tokens, task generation, and databases. No statistical inference about equivalence, overhead or cause.','PostgreSQL 17.11 from Debian on the local host via with_postgres.py; two fresh clusters with 60 allowed connections, loopback-only generated data and matching durability. GitHub uses its own Ubuntu runner and postgres:17.11-bookworm service with its own connection settings. Local absolute throughput is not pooled with the hosted numbers.','Only the original 1000-read-open job was timed. Unsaturated SQL trace after both timed runs uses the real HTTP endpoint and the identical STATE request logic for three selected calls; it records SQL-shape hashes, never values. SQL of every concurrent request in the 5000-request timed windows was not individually traced.','The read-only designation refers to unchanged authoritative history. Existing pre-bridge S9 observations may be recorded as writes. The published workload counts SQL statement kinds separately.','Each raw result retains every request and gate; this comparison does not relax or close issue #13.']}
report['files_sha256']={str(p.relative_to(dst)):sha(p) for p in sorted(dst.rglob('*')) if p.is_file() and p.name!='comparison.json'}
(dst/'comparison.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps({k:report[k] for k in ['candidate_minus_baseline','separate_request_shape_probe']}))
