"""Re-read the actual historical capacity result and its public bilingual figures."""
from pathlib import Path
from statistics import median
import gzip
import hashlib
import json
root=Path('NovelFactory')
perf=Path('perf-complete')
def sample(arm, scenario):
    p=perf/'raw'/arm/scenario/'load.json.gz'
    return json.loads(gzip.open(p).read()),hashlib.sha256(p.read_bytes()).hexdigest()
checks=[]
for arm in ('at30','at30-candidate'):
    data,sha=sample(arm,'200-overload-1')
    calls=data['open_loop_offered_rate']
    checks.append(dict(arm=arm,scenario='200-overload-1',raw_sha256=sha,
                       gate=calls['gate'],actual_call_start_rps=calls['actual_call_start_rps'],
                       transport_header_start_rps=calls['transport_header_start_rps'],
                       minimum_call_starts=calls['target_rps']*.95,
                       valid_math=calls['actual_call_start_rps']<calls['target_rps']*.95 and calls['gate']=='NOT_MET'))
for lang,file in [('zh-CN','10-性能与实验.md'),('en','10-performance-and-evaluation.md')]:
    s=(root/'docs'/lang/file).read_text()
    for v in checks:
        assert f"{v['actual_call_start_rps']:.2f}" in s, (lang,v['arm'],'historical gate value')
        assert f"{v['transport_header_start_rps']:.2f}" in s,(lang,v['arm'],'wire header value')
for arm in ('at30','at30-independent'):
    data,sha=sample(arm,'500-soak')
    checks.append(dict(arm=arm,scenario='500-soak',raw_sha256=sha,
                       result={k:data[k] for k in ('probe_gate','long_duration_gate','scheduled_count','status_counts','latency_all_attempts')}))
    assert data['scheduled_count'] == data['status_counts']['200'] == 12000
for arm in ('at30','at30-candidate'):
    data,sha=sample(arm,'1000-open')
    counts=data['status_counts']
    assert sum(counts.values())==5000 and data['capacity_gate']=='NOT_MET'
    checks.append(dict(arm=arm,scenario='1000-open',raw_sha256=sha,
                       wire_starts=data['observed_wire_start_rps'],wire_gate=data['offered_wire_rate_gate'],
                       capacity=data['capacity_gate'],valid=data['validated_successes'],status=counts))
Path('doc-independent-editor/final-measurements.json').write_text(json.dumps(checks,indent=2)+'\n')
print(len(checks),'original capacity evidence files verified in both public documents')
