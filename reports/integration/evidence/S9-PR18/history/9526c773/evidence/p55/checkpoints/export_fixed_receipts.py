"""Allowlisted export from immutable run artifacts containing synthetic benchmark responses."""
import gzip
import hashlib
import json
from pathlib import Path
import re
import shutil
import xml.etree.ElementTree as ET

dest = Path('receipt/evidence/p55/s9-local/remote-55d897')
dest.mkdir(parents=True, exist_ok=True)
guard = Path('artifact-guard')
assert (guard/'source-sha.txt').read_text().strip() == '55d8979b5361a4837701d762feacd83036096b11'
expected = {'guard-comparison.json', 'guard-registration.json', 'queries.xml', 'source-sha.txt'}
sums = {}
for line in (guard/'SHA256SUMS').read_text().splitlines():
    digest, name = line.split()
    assert name in expected and hashlib.sha256((guard/name).read_bytes()).hexdigest() == digest
    sums[name] = digest
assert set(sums) == expected
result = json.loads((guard/'guard-comparison.json').read_bytes())
assert set(result) == {'unit','workload','old_world','new_world','rows'}
assert [r['arm'] for r in result['rows']] == list('ABCD')
for r in result['rows']:
    assert r['model_calls'] == r['model_tokens'] == 0 and r['invoiced_cost'] is None
    assert {o['scenario'] for o in r['observations']} == {'same_object','causal_read_other_write','independent'}
    assert all(len(o['samples_ms']) == 12 for o in r['observations'])
for name in sorted(expected):
    data = (guard/name).read_bytes()
    assert len(data) < 500_000
    assert not re.search(rb'Bearer [a-zA-Z0-9._-]+|postgres(?:ql)?[^ ]*://[^ ]+:[^ ]+@|-----BEGIN .* PRIVATE KEY',data)
    shutil.copyfile(guard/name,dest/name)
shutil.copyfile(guard/'SHA256SUMS',dest/'guard-SHA256SUMS')
suites = ET.parse(dest/'queries.xml')
assert all(s.get('errors') == s.get('failures') == s.get('skipped') == '0' for s in suites.iter('testsuite'))
fixed_keys = {'index','agent','kind','started_seconds','arrival_delay_ms','valid','status','code','latency_ms','correctness_error','error_type'}
summaries = []
for arm, run, job, artifact in (
    ('candidate-attempt-1',43036514307,130576649020,'p3-scale-500-read-soak-1'),
    ('previous-attempt-1',43035161204,130571937813,'p3-scale-500-read-soak-1'),
):
    binary = (Path('artifact-'+arm)/'result.json').read_bytes()
    src = json.loads(binary)
    assert src['real_model_calls'] == 0 and src['new_world_commits'] == 0
    rows = src['records']
    assert len(rows) == src['scheduled_count'] == 12000
    for row in rows:
        assert set(row) <= fixed_keys and all(isinstance(value,(int,float,bool,str,type(None))) for value in row.values())
        assert all(not isinstance(value,str) or re.fullmatch(r'[a-zA-Z0-9_.:-]{0,100}',value) for value in row.values())
    (dest/(arm+'-records.json.gz')).write_bytes(gzip.compress(json.dumps(rows,separators=(',',':')).encode(),mtime=0))
    summary = {k:src[k] for k in ['scheduled_count','duration_seconds','actual_call_start_rps','successful_query_rps','latency_all_attempts','status_counts','correctness_errors','world_unchanged','new_world_commits','real_model_calls','probe_gate','long_duration_gate']}
    summary.update(run=run,job=job,artifact=artifact,source_result_sha256=hashlib.sha256(binary).hexdigest(),arm=arm,
        cpu_models=src['environment']['cpu_models'],initial_load_average=src['environment']['load_average'],
        admission=src['service_final']['admission'],service_resources=src['service_final']['resources'],
        sql_execution_seconds=src['service_final']['database']['sql_cursor_execution_seconds'])
    summaries.append(summary)
(dest/'long-read-summaries.json').write_text(json.dumps(summaries,indent=2)+'\n')
