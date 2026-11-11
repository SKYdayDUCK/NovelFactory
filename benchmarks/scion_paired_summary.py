"""Summarize matched operations from a phase_d directory of lossless JSON samples.

Usage: python benchmarks/scion_paired_summary.py evidence/scion_20261111/phase_d [csv_directory]
The JSON summary is written to stdout. Optional CSVs contain one row per scenario
or operation. Missing instrumentation stays blank, and original samples are retained.
"""
import gzip
import lzma
import json
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median
import sys

root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "results"

def read(path):
    decompressor = lzma.open if path.suffix == ".xz" else gzip.open
    with decompressor(path) as handle:
        return json.load(handle)

def distribution(values):
    if not values:
        return None
    return {"median":median(values), "minimum":min(values), "maximum":max(values), "n":len(values)}

def profile(case, samples):
    result={"case":case, "operations":{}, "counter_snapshots":{}, "replicates":[]}
    grouped=defaultdict(lambda: {"A":defaultdict(list),"B":defaultdict(list)})
    for run,pair in sorted(samples.items()):
        if pair.keys()!={"A","B"}:continue
        a,b=(pair[arm] for arm in "AB")
        # The pre-meter pilot lacks identical harness metadata and a warm chain.
        if (not a['paired_inputs'].get('harness_sha256') or
                a['paired_inputs']['harness_sha256'] != b['paired_inputs'].get('harness_sha256')):
            continue
        assert a['paired_inputs']['fixture_sha256'] == b['paired_inputs']['fixture_sha256']
        assert a['datasets'][0]['state_hash'] == b['datasets'][0]['state_hash']
        if case == 'history-retcon' or case.endswith('-exact'):
            assert a['verified_state_hash'] == b['verified_state_hash']
            assert a['verified_event_selection'] == b['verified_event_selection']
            result['verified_state_hash'] = a['verified_state_hash']
            if case.endswith('-exact'):
                requests=[(row['operation'],row['request_sha256']) for row in a['records']]
                assert requests == [(row['operation'],row['request_sha256']) for row in b['records']]
                result['identical_request_sha256']=dict(requests)
        result['replicates'].append(run)
        result['counter_snapshots'][run]=b.get('scion_counter_snapshots',{})
        for arm,doc in pair.items():
            for record in doc["records"]:
                suffix="warm_parent" if record["suite"].endswith("-chain") else "cold_parent"
                label=record["operation"]
                if label=="state_query":
                    label+="."+record["result_cache_expected"]
                slot=grouped[(label,suffix)][arm]
                for key,value in (
                    ("ms",record["wall_seconds"]*1000),
                    ("cpu_ms",record["process_cpu_seconds"]*1000),
                    ("replay_events",record["replayed_events_total"]),
                    ("full_replay_calls",len(record["replay_event_counts"])),
                    ("json_bytes",record["database_json_codec"].get("decode",{}).get("bytes",0)),
                    ("selected_rows",record["sql"]["selected_rows_reported_by_cursor"]),
                    ("sql",record["sql"]["statements"]),
                    ("select_sql",record["sql"]["statement_kinds"].get("SELECT",0)),
                    ("sql_write",sum(record["sql"]["statement_kinds"].get(k,0) for k in ["INSERT","UPDATE","DELETE"])),
                    ("rss_kib",record["rss_after_kib"]),
                ):
                    slot[key].append(value)
    for (operation,mode),arms in grouped.items():
        result['operations'][mode+'.'+operation]={arm:{key:distribution(values) for key,values in metrics.items()}
            for arm,metrics in arms.items()}
    return result

def aggregate_capacity(doc):
    case=doc['paired_inputs']['case']
    if doc.get('profile') == 'overload':
        case='overload-'+str(doc['logical_agent_task_count'])
    rows=doc.get('raw_requests', doc.get('records', []))
    # Keep waiting connection group samples distinct from the elapsed time.
    waits=Counter()
    for observation in doc['database_observer']['samples']:
        for conn in observation.get('connections',[]):
            waits[conn.get('wait_event_type') or 'no_wait']+=conn.get('count',conn.get('n',1))
    result={'case':case,'fixture_sha256':doc['paired_inputs']['fixture_sha256'], 'cpu_affinity':doc['paired_inputs']['cpu_affinity'],
            'source_sha':doc['paired_inputs']['source_sha'],'server_arrival':doc.get('service_boundary'),
            'pg_cpu_seconds':doc.get('pg_cluster_cpu_seconds'), 'pg_connection_wait_samples':dict(waits)}
    counters=Counter()
    for w in (result['server_arrival'] or {}).get('workers',[]):
        for observed in w.get('scion',[]):counters.update(observed)
    result['worker_scion_counts']=dict(counters)
    if "metrics" in doc:
        m=doc["metrics"]
        result['offered_burst_first_attempts']=doc.get('open_loop_offered_rate',{})
        result.update(capacity_gate=doc['capacity_gate'],latency=m['first_attempt_latency'],planned=m['planned_requests'],
                      statuses=m['status_counts'],first_failures=m['first_attempt_failure_count'],
                      required_operation_exhausted=len(m['unresolved_operational_errors']), retries=m["infrastructure_retries"],
                      committed=doc['correctness']['new_world_revisions'],expected_commits=doc['correctness']['expected_new_world_revisions'],
                      effective_rps=m['effective_call_rps_excluding_duplicates'],commits_tps=m['effective_world_commits_per_second'],
                      first_emitted=m['emitted_logical_operations'],never_emitted=m['client_requests_never_emitted'],
                      completed=m['completed_logical_operations'],correctness_errors=len(m['correctness_errors']),
                      per_operation=m['per_operation'],client_hdr_starts=m['transport_headers_started'],
                      client_headers_per_second=m['transport_headers_started']/m['duration_seconds'])
    else:
        result.update(capacity_gate=doc.get('capacity_gate',doc.get('probe_gate')),latency=doc['latency_all_attempts'],
                      statuses=doc['status_counts'],expected_reads=len(rows),
                      validated_reads=doc.get('validated_successes',sum(1 for r in rows if r.get('valid'))),
                      effective_rps=doc['successful_query_rps'],commits_tps=0,correctness_errors=len(doc['correctness_errors']),
                      client_hdr_starts=doc.get('wire_started_requests'),client_headers_per_second=doc.get('observed_wire_start_rps'),
                      dispatch_rps=doc.get('dispatch_start_rps',doc.get('actual_call_start_rps')))
    if "metrics" in doc:
        final={}
        for row in rows:
            op=row['operation_id']
            if op not in final or row['attempt']>=final[op]['attempt']:
                final[op]=row
        ok_status=sum(r['status']==200 and not r.get('error_type') for r in final.values())
        conflicts=sum(bool(r.get('expected_conflict')) for r in final.values())
        result.update(terminal_valid_200=ok_status, terminal_expected_conflicts=conflicts,
            terminal_200_or_expected_conflict=ok_status+conflicts,
            validated_business_fraction=(ok_status+conflicts)/m['planned_requests'],
            client_cpu_s=sum(doc['resources'].get(k,0) for k in ['process_user_cpu_seconds','process_system_cpu_seconds']),
            service_cpu_s=sum(doc['service_resources'].get(k,0) for k in ['service_user_cpu_seconds','service_system_cpu_seconds']),
            service_peak_rss_kib=doc['service_resources'].get('service_sum_peak_rss_kib'),
            rejection_429=m['status_counts'].get('429',0),rejection_503=m['status_counts'].get('503',0),
            exhausted_terminal_logical_operations=sum(1 for r in final.values() if r.get('retry_exhausted')),
            exhausted_by_phase=dict(Counter(r['label'] for r in final.values() if r.get('retry_exhausted'))),
            distinct_non_emitted=m['client_requests_never_emitted'])
        starts=sorted(r['wire_started_seconds'] for r in rows if r.get('wire_started_seconds') is not None)
        result['client_first_last_header_rps']=(len(starts)-1)/(starts[-1]-starts[0]) if len(starts)>1 and starts[0]!=starts[-1] else None
    else:
        resources=doc.get('service_metrics',doc.get('service_final',{})).get('resources',{})
        result.update(validated_business_fraction=result['validated_reads']/result['expected_reads'],
                      client_cpu_s=None,service_cpu_s=sum(resources.get(k,0) for k in ['service_user_cpu_seconds','service_system_cpu_seconds']),
                      service_peak_rss_kib=resources.get('service_sum_peak_rss_kib'),
                      rejection_429=doc['status_counts'].get('429',0),rejection_503=doc['status_counts'].get('503',0),
                      retries=0,dispatch_target_rps=doc['configuration'].get('arrival_rps',doc['configuration'].get('read_rps')),
                      client_header_instrumented='observed_wire_start_rps' in doc)
    if 'client_header_meter' in doc:
        meter=doc['client_header_meter']
        result.update(client_hdr_starts=meter['n'],client_header_instrumented=True,
                      client_headers_per_second=meter['headers_per_second'],
                      client_cpu_s=meter['process_cpu_seconds_including_profile_finalization'])
    return result

grouped=defaultdict(lambda:defaultdict(dict))
for path in sorted([*(root/'attribution').glob('*.json.gz'),*(root/'retcon').glob('*.json.gz'),*(root/'exact').glob('*.json.gz')]):
    case,arm,run=path.name.removesuffix('.json.gz').rsplit('-',2)
    grouped[case][run][arm]=read(path)
result={"profile":[profile(case,samples) for case,samples in sorted(grouped.items())],"capacity":{}}
for kind in ('capacity','open-repeat','stability-repeat','ci-overload','ci-overload-repeat','mixed50-repeat','ablation-http','stability-headers'):
    # Long soak service snapshots contain repeated nested histories. Standard
    # LZMA stores the same full JSON at a fraction of gzip's size.
    paths = {p.name.rsplit('.',1)[0]:p for p in sorted((root/kind).glob('*.json.gz'))}
    paths.update({p.name.rsplit('.',1)[0]:p for p in sorted((root/kind).glob('*.json.xz'))})
    for path in paths.values():
        result['capacity'][kind+'/'+path.name]=aggregate_capacity(read(path))
# Mixed commands that reference branch-local server-generated IDs necessarily
# differ after issuance. Quantify exact shared payloads, separately from retries.
result["first_http_payload_comparison"]={}
for bucket in ('capacity','ci-overload','ci-overload-repeat','mixed50-repeat'):
    for left in sorted((root/bucket).glob('*-A-1.json.gz')):
        right=left.with_name(left.name.replace('-A-1.','-B-1.'))
        if not right.exists():continue
        a,b=(read(p) for p in (left,right))
        if 'metrics' not in a:continue
        assert a['paired_inputs']['fixture_sha256']==b['paired_inputs']['fixture_sha256']
        def scenario_keys(doc):
            # The client operation counter follows arrival order and changes with
            # scheduling. Identify actions by actor, phase and its local ordinal.
            out,seen={},Counter()
            for r in sorted(doc['raw_requests'],key=lambda row:row['started_seconds']):
                if r.get('attempt',1)!=1:continue
                base=(r['label'],r['agent'])
                out[(*base,seen[base])]=r['request_bytes_sha256']
                seen[base]+=1
            return out
        at,bt=scenario_keys(a),scenario_keys(b)
        counts=defaultdict(Counter)
        for key in at.keys() | bt.keys():
            cell=counts[key[0]]
            if key not in bt:
                cell['A_only']+=1
            elif key not in at:
                cell['B_only']+=1
            else:
                cell['identical' if at[key]==bt[key] else 'different']+=1
        result["first_http_payload_comparison"][bucket+'/'+left.name.split('-A-')[0]]=counts
ablations=defaultdict(lambda:defaultdict(lambda:defaultdict(list)))
for path in sorted((root/'ablation').glob('*.json.gz')):
    case,variant,run=path.name.removesuffix('.json.gz').rsplit('-',2)
    doc=read(path)
    for row in doc['records']:
        line=row['operation']
        if line=='state_query':
            line+='.'+row['result_cache_expected']
        label=('warm.' if row['suite'].endswith('-chain') else 'cold.') + line
        slot=ablations[case][variant]
        for key,value in [('ms',row['wall_seconds']*1000),
                          ('cpu_ms',row['process_cpu_seconds']*1000),
                          ('decoded_json_bytes',row['database_json_codec'].get('decode',{}).get('bytes',0)),
                          ('selected_rows',row['sql']['selected_rows_reported_by_cursor']),
                          ('sql',row['sql']['statements']),('full_replay_calls',len(row['replay_event_counts'])),
                          ('full_replay_events',row['replayed_events_total'])]:
            slot[label+'.'+key].append(value)
result['ablation_medians']={case:{var:{k:distribution(v) for k,v in vals.items()}
    for var,vals in cases.items()} for case,cases in ablations.items()}
print(json.dumps(result,ensure_ascii=False,indent=2))

# Optional compact CSV companion tables. Keep the detailed JSON on stdout.
if len(sys.argv) > 2:
    import csv
    out = Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    def table(filename, rows):
        if not rows:
            return
        keys = list(dict.fromkeys(key for row in rows for key in row))
        with (out/filename).open("w",newline="",encoding="utf-8") as handle:
            writer=csv.DictWriter(handle,keys)
            writer.writeheader()
            writer.writerows(rows)
    rows=[]
    for filename,v in result['capacity'].items():
        arrival=v.get('server_arrival')
        rt=v.get('latency') or {}
        rows.append(dict(sample=filename,case=v['case'],source_sha=v['source_sha'],
            fixture_sha256=v['fixture_sha256'],cpu_affinity='|'.join(map(str,v['cpu_affinity'])),
            denominator=v.get('planned',v.get('expected_reads')),
            valid_terminal_or_expected_conflict=v.get('terminal_200_or_expected_conflict',v.get('validated_reads')),
            validated_business_fraction=v['validated_business_fraction'],status_429=v['rejection_429'],
            status_503=v['rejection_503'],transport_errors=v['statuses'].get('transport_error',0),
            correctness_errors=v['correctness_errors'],first_failures=v.get('first_failures'),
            required_stage_errors=v.get('required_operation_exhausted'),
            total_exhausted=v.get('exhausted_terminal_logical_operations'),never_emitted=v.get('never_emitted'),
            retries=v.get('retries'),new_commits=v.get('committed',0),expected_commits=v.get('expected_commits',0),
            effective_rps=v['effective_rps'],new_commit_tps=v['commits_tps'],p50_ms=rt.get('p50_ms'),
            p95_ms=rt.get('p95_ms'),p99_ms=rt.get('p99_ms'),client_header_rps=v.get('client_first_last_header_rps',v.get('client_headers_per_second')),
            client_dispatch_rps=v.get('dispatch_rps'),
            mixed_overload_burst_dispatch_rps=v.get('offered_burst_first_attempts',{}).get('actual_call_start_rps'),
            mixed_overload_burst_header_rps=v.get('offered_burst_first_attempts',{}).get('transport_header_start_rps'),
            server_asgi_rps=arrival['arrivals_per_second'] if arrival else None,
            server_received=arrival['count'] if arrival else None,
            replay_suffix_hits=v['worker_scion_counts'].get('replay.hit',0) if arrival else None,
            replay_decisions=sum(n for key,n in v['worker_scion_counts'].items() if key.startswith('replay.')) if arrival else None,
            sparse_scope_hits=v['worker_scion_counts'].get('scopes.hit',0) if arrival else None,
            column_projection_hits=v['worker_scion_counts'].get('columns.query_hit',0) if arrival else None,
            sql_lock_connection_samples=v['pg_connection_wait_samples'].get('Lock',0),
            pg_cluster_cpu_seconds=v.get('pg_cpu_seconds'),service_cpu_seconds=v.get('service_cpu_s'),client_cpu_seconds=v.get('client_cpu_s'),
            peak_service_rss_kib=v.get('service_peak_rss_kib'),gate=v.get('capacity_gate')))
    table('http.csv',rows)
    rows=[]
    for case in result['profile']:
        for op,arms in case['operations'].items():
            for arm,vals in arms.items():
                rows.append({'case':case['case'],'operation':op,'arm':arm,'paired_replicates':'|'.join(case['replicates']),
                             **{k+'_median':v['median'] if v else None for k,v in vals.items()}})
    table('stage_metrics.csv',rows)
    rows=[]
    for case,variants in result['ablation_medians'].items():
        for variant,metrics in variants.items():
            rows.extend({'case':case,'variant':variant,'measure':key,**value} for key,value in metrics.items())
    table('ablation.csv',rows)
    rows=[]
    for case,measures in result['first_http_payload_comparison'].items():
        for label,counts in measures.items():
            rows.append({'case':case,'phase':label,**{key:counts.get(key,0) for key in ['identical','different','A_only','B_only']}})
    table('request_identity.csv',rows)
