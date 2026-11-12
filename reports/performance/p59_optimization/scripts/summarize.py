"""Rebuild P5.9 operation, capacity, and controlled comparison tables from raw observations."""
import collections
import gzip
import json
import math
from pathlib import Path
import statistics
import xml.etree.ElementTree as ET
ROOT=Path(__file__).resolve().parents[1]
def read(path):
    path=ROOT/path
    if path.is_file():return json.loads(path.read_bytes())
    return json.loads(gzip.decompress(Path(str(path)+".gz").read_bytes()))
def percentile(a,f):
    return sorted(a)[max(0,math.ceil(len(a)*f)-1)]
def table(header,rows):
    return "\n".join(["| "+" | ".join(header)+" |","| "+" | ".join(["---"]*len(header))+" |",*["| "+" | ".join(map(str,row))+" |" for row in rows]])
def write(name,content):
    (ROOT/name).write_text(content.rstrip()+"\n")
def run():
    matrix=read("raw/matrix-final/samples.json")["samples"]
    pairs=read("raw/scope-pair.json")
    projects=read("raw/multi-project.json")["trials"]
    selected={
      "1 Task creation":"Kernel.create_task","2 Context compile":"WorldQueryService.context_compile","3 World STATE query":"WorldQueryService.query.STATE",
      "4 Historical TIMELINE":"WorldQueryService.query.TIMELINE","5 Agent dispatch":"RuntimeSupervisor.run_once",
      "6 Guarded world writes":"Kernel.accept","7 Redis stream read/reclaim":"Streams.read_batch",
      "8 MCP call over stdio":"ClientSession.call_tool","9 Model routing":"model_routing.select","10 Local HTTP generation":"OpenAICompatibleAdapter.generate",
      "11 Delegation receipt lookup":"Delegations.call.nf_delegation_get","13 Cross-volume advance":"production_pipeline.advance_pipeline",
      "14 Fine-grained creative review":"CreativeReviewService.review","15 Statistics read":"StatisticsService.read",
      "16 Recovery":"RuntimeSupervisor.recover_project",
    }
    metrics=[]
    for label,suffix in selected.items():
        r=[x for x in matrix if x['operation'].endswith(suffix)]
        timings=[x['wall_ms'] for x in r]
        metrics.append([label,len(r),sum(x["outcome"]=="RETURNED" for x in r),*(f"{percentile(timings,k):.2f}" for k in (.5,.95,.99)),f"{statistics.median(x['thread_cpu_ms'] for x in r):.2f}",f"{statistics.median(x['sql_cursor_calls'] for x in r):g}"])
    flow=[]
    for t in projects:
        lag=[r['latency_ms'] for r in t['jobs']]
        flow.append([t['works'],t['mode'],t['sent_by_endpoint'],sum(r['status']=='RESULT_STORED' for r in t['jobs']),sum('UNKNOWN' in r['attempt_states'] for r in t['jobs']),f"{t['completed_tasks_per_second']:.2f}",f"{max(lag):.2f}"])
    write("OPERATION_MATRIX.md","# Original PG17 operation samples\n\nThe 18 scenario checks in raw/matrix-final/junit.xml passed. Durations cover successful calls and expected refusals from correctness tests. SQL is the parent thread's cursor-call count; MCP children and HTTP server processes have separate SQL sessions. RSS is the parent high-water mark. Nested calls and concurrent threads must not be added. Percentiles use nearest rank. With fewer than 100 observations P99 is the sample maximum. These calls have varied real fixture inputs, and the table describes the observed distribution rather than independent stationary throughput. Exact tests and command are in scripts/matrix.sh.\n\n"+table(["Operation","Calls","Returned","P50 ms","P95 ms","P99 ms","CPU P50 ms","SQL P50"],metrics)+"\n\n## 12. Concurrent project scheduling and provider failures\n\nEach authorized supervisor has a different work, budget and task. Endpoints listen on loopback. Idle admission is retried on the same task. Each task receives exactly one model Attempt. A simulated 503 after send retains its UNKNOWN Attempt. The healthy endpoint completed all its independently routed work.\n\n"+table(["Works","Mode","Endpoint sends 1/2","Stored results","UNKNOWN","Stored tasks/s","Max latency ms"],flow)+"\n\nSee raw/multi-project.json for per-project results, waiting and peaks. The first one-poll-only attempt is under raw/attempted; it exposed legitimate quota deferral when more projects competed.\n")
    pair_rows=[]
    for bench,n in sorted({(s['bench'],s['size']) for s in pairs['samples']}):
        rows=[x for x in pairs['samples'] if x['bench']==bench and x['size']==n]
        a=[x for x in rows if x['arm']=='BASELINE']; b=[x for x in rows if x['arm']=='CANDIDATE']
        av,bv=[statistics.median(z['wall_ms'] for z in arm) for arm in (a,b)]
        pair_rows.append([bench,n,len(a),f"{av:.3f}",f"{bv:.3f}",f"{100*(bv/av-1):+.1f}%",f"{a[0]['sql']} → {b[0]['sql']}",sum(x['wall_ms']<y['wall_ms'] for x,y in zip(b,a))])
    paired=table(["Measured boundary","Items / keys","Pairs","Base ms","Candidate ms","Median change","SQL/arm","Candidate faster pairs"],pair_rows)
    write("BEFORE_AFTER.md","# Same-state paired comparison\n\n"+paired+"\n\nThe accept timings include ordinary verification, event writes, scope updates and a fault immediately before commit. Every trial rolls back the same frozen proposal; a final actual commit succeeds and its idempotent retry returns the same receipt. A real commit flush is outside the paired latency window. Seven independently committed baseline writes including durable receipt checks are in raw/write-baseline.json. Varying task/fixture observations elsewhere in this directory cannot be used as time-paired comparisons.\n")
    caps=[]
    for path in sorted((ROOT/'raw').glob('at30*/*/load.json.gz')):
        x=json.load(gzip.open(path)); group=path.parent.parent.name; test=path.parent.name
        m=x.get('metrics',{}); offer=x.get('open_loop_offered_rate') or {}
        if isinstance(offer,str):offer={}
        rate=x.get('observed_wire_start_rps',offer.get('transport_header_start_rps'))
        status=x.get('status_counts',m.get('status_counts',{}))
        capacity=x.get('capacity_gate',x.get('probe_gate','see raw'))
        latency=x.get('latency_all_attempts',m.get('latency_all_responses',{}))
        checked=x.get('validated_successes',m.get('valid_successful_calls',status.get('200')))
        resource=x.get("service_resources") or (x.get("service_metrics") or x.get("service_final") or {}).get("resources") or {}
        db=x.get("database_application") or (x.get("service_metrics") or x.get("service_final") or {}).get("database") or {}
        n_attempts=m.get("completed_requests")
        retries=n_attempts-m.get("completed_logical_operations",0) if n_attempts is not None else 0
        caps.append({"retry_attempts":retries,"validated_rps":x.get("successful_query_rps",m.get("effective_call_rps_excluding_duplicates")),"new_world_commits":x.get("new_world_commits",m.get("new_world_commit_receipts")),"database_pool_peak":db.get("peak_checked_out_connections"),"database_sql":db.get("sql_statements"),"service_cpu_seconds":resource.get("service_user_cpu_seconds",0)+resource.get("service_system_cpu_seconds",0),"service_sum_peak_rss_kib":resource.get("service_sum_peak_rss_kib"),"arm":group,"scenario":test,"gate":capacity,"business":x.get('bounded_recovery_business_gate'),"wire_rps":rate,"status":status,"validated":checked,"latency":latency,"raw":str(path.relative_to(ROOT))})
    write("CAPACITY_RUNS.md","# Original capacity gate executions\n\nThe 200 overload offered-call target remains 1000/s and its original minimum 950/s. 1000 open uses the actual header-start and requires all responses validated. P2 status counts include retries and other workflow phases. Each raw asset retains the original configuration, offered traffic, latency distributions, correctness, resource metrics and exit status. Read soak groups are separate repetitions.\n\n"+table(["Arm","Scenario","Capacity","Business recovery","Wire starts/s","HTTP/transport outcomes"],[[r["arm"],r["scenario"],r["gate"],r["business"] or "—",f"{r['wire_rps']:.2f}" if isinstance(r['wire_rps'],(int,float)) else "—",str(r["status"])] for r in caps]))
    with (ROOT/"CAPACITY_RUNS.md").open("a") as file:
        file.write("\n## Latency, useful work and resource use\n\nP2 uses all HTTP responses across phases; P3 uses all attempted responses, including transport failures. P2 useful throughput excludes successful duplicate receipts. The read-only P3 value is validated query throughput. RSS is the sum of per-worker high-water marks. The database column counts application cursor executions. P2 contention retries are counted from actual HTTP completions minus distinct completed logical operations.\n\n")
        file.write(table(["Arm / scenario","P50/P95/P99 ms","Useful/s","Commits","Retries","SQL","Peak pool","CPU s","Peak RSS KiB"],[
          [r["arm"]+"/"+r["scenario"],"/".join(f"{r['latency'][k]:.1f}" for k in ("p50_ms","p95_ms","p99_ms")),f"{r['validated_rps']:.2f}",r["new_world_commits"],r["retry_attempts"],r["database_sql"],r["database_pool_peak"],f"{r['service_cpu_seconds']:.1f}",r["service_sum_peak_rss_kib"]] for r in caps]))
        file.write("\n")
    write("capacity-summary.json",json.dumps(caps,indent=2))
if __name__=="__main__":run()
