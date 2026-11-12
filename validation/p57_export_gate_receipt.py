"""Export bounded, public engineering evidence through GitHub job logs.

Usage: python export.py SUITE ci-evidence/p57-SUITE
Wire: P57EV1 SUITE I/N PAYLOAD_SHA256 GZIP_SHA256 BASE64_SLICE
Concatenate slices by integer I, base64 decode, check gzip SHA, decompress, then check payload SHA.
"""
import base64, gzip, hashlib, json, math, os, re, stat, sys
from collections import Counter
from pathlib import Path

SUITES=set("contracts harness dual faults upgrades workshops-upgrades history projection p6 comparison paired-restore pitr packaging".split())
EXTRA={"comparison":"comparison.json","paired-restore":"paired-restore.json","pitr":"pitr/result.json","packaging":"packaging-evidence.json"}
FCAP=24*1024*1024
PUBLIC_MARK="Synthetic arm outputs, immutable refs, source digests and objective results. No original source corpus or bearer credentials."
ENUM=set("""PASS FAILED ERROR BLOCKED NOT_RUN COMPLETE INCOMPLETE CLEAR UNKNOWN SUCCEEDED SENT PREPARED READY ACCEPTED PAUSED CANCELLED RELEASED REQUESTED RECONCILING BOUND SETTLED WAITING_OPERATOR IDLE APPROVE REJECT ROLLBACK ADOPTED DECLINED MANUAL POLICY_AUTOMATIC UNPROVEN NOT_EVALUATED REAL_ISOLATED_POSTGRESQL ENGINEERING ENGINEERING_ONLY NOT_SENT KNOWN UNKNOWN_COST VALIDATED_CANDIDATE RESULT_STORED TEST_FIXTURE NO_ATTEMPT NOT_RUN TIMEOUT DEADLINE_EXCEEDED NO_ATTEMPT_HELD THREE_ARM ALL_PREREGISTERED_CASES_INCLUDING_FAILURES REQUIRED INDEPENDENT_READING_OR_UNPROVEN EXPERIMENT_FREEZE BASELINE HARNESS HARNESS_ADAPTIVE BASELINE_ONLY TREATMENT CONTROL USD S1 S2 S3 S4 S5 S6 summary writer scene_draft reviewer literary evidence_maintainer w011 LOCAL_FIXTURE s6-scene-calibration-v1""".split())
DATA_KEYS=set("""
status pg postgres execution_kind source_commit baseline_commit python durability fsync synchronous_commit data_checksums
model_calls model_attempts model_tokens production_data_accessed libpq_environment_isolated base_backup_seconds archiver archived_count failed_count
target_world_ref target_lsn replayed_lsn second_target_lsn second_replay_lsn restored_state_sha256 snapshot_equal_before_fencing
table_count object_count recovery_seconds_to_verified_service selected_recovery_point_age_seconds target_commits_lost deliberately_excluded_later_world_commits
restored_world_event_count unknown_calls_retained reserved_amount_retained fenced_task_count old_worker_rejected original_commit_receipt_equal
total_seconds creative_harness_recovery shared_token_budget_after_physical_restore restored_semantic_conflict redis_pitr_reconciliation
work_id branch_id world_revision_id task_id generation previous_generation attempt_id actor_id artifact_id artifact_revision_id sha256
packet_ref decision_ref input_ref compiled_sha256 preserved_source_count provider_cost_status post_target_artifact_absent role_raw_read_denied
target predicted_scope_match verification manifest_sha256 second_wal_target shared_token_budget budget_id total_tokens original_attempt_id
before_first_call live_unknown unknown_wal_target unknown_target_lsn unknown_replayed_lsn recovered_unknown recovered_workshop_observations
after_repeated_invoice final_workshop_observations same_receipt_missing_attempt_peer after_settled_history_mismatch rolled_back_nf_task_count
rolled_back_aw_intent live_nf nf_restored_aw_live aw_restored_nf_live_before_invoice after_explicit_synthetic_invoice resulting_nf_tasks
loopback_synthetic_http_requests supplier_model_calls frozen_manager workshop_offline elapsed_seconds available_tokens reserved_tokens spent_tokens
currency actual_tokens actual_amount actual_amount_usd known_amount spent_amount limit_amount reserved_amount token_limit accounting_state task_state
deadline race_count operation_id receipt_ref token_observations original_attempt_count attempts hold token_total managed held state original_receipt
frozen_head frozen_index_tree wheels distributions filename bytes installed isolated_imports default_switches_off workshop_head role_count skill_count
snapshots snapshot_matrix_sha256 matching_static_files matching_nf_modules matching_workshop_files verified_source_migrations dependencies_consistent failed_phase
matching_original_runtime_files confirmed field_count study freeze_ref freeze experiment_id frozen_at works shared_input_ref case_manifest_ref budget_policy_ref
rubric_ref blind_allocation_ref model_config_sha256 quota order_seed repetitions arms arm_id candidate_ref creative_harness enabled dynamic_agents
adaptive_instructions auto_promote schema_version kind sampling design metrics engineering review_effectiveness tokens cost latency rework literary shared_input
world_ref plan_ref batch_ref scene_ref adjacent_draft_ref adjacent_sha256 original_source_sha256 subject_id reader_cut_ordinal world_effects_permitted
source_locator paragraphs budget_policy per_role_attempts reserve_amount per_call_allowance retries parallel_arms rework_limit paid_provider_spend
optimization_quota max_tokens max_amount max_attempts rubric sha256_of_sealed_labels independent_batch allocation arm_sources flags fixture_sha256
employee_revision revision_ref instance_ref profile_ref proposal_ref skill_ref instruction_proposal source_revision skill_source content_sha
runtime_sha256 source_module_sha256 runtime config calibration_batches confirmatory_batches real_model_calls paid_cost_usd planned_windows rows
arm blind_id stages role task_kind source_type stage usage cached_input_tokens input_tokens output_tokens cost_status elapsed_ns
evidence_ref request_sha256 prompt_ref response_ref output_ref creative_context_ref dynamic_assignment_ref adaptive_instruction_ref
input output_contract version effective_role_sha256 visible_user_sha256 visible_system_sha256 manuscript_sha256 draft_ref review_spans
true_positive false_positive false_negative expected control_fields_in_prose native_narrative_accepted error type code elapsed_ns
official_state_after world_ref_after reconciliation reconciliation_ref advisory_task_id advisory_input_ref advisory_ref quality start end
optimization review_ref selection_ref arm_totals local_engineering_executions evaluation_and_promotion_latency_ns
engineering_report_ref run_manifest_ref baseline treatment complete_windows total_nf_attempts local_meter_units unsettled_attempts
common_training_preparation_latency_ns adaptive_proposal_registration_and_selection_latency_ns expected_count expected_occurrence reader_judgments
unsealed_labels completed_at workshop_attestations binding_id_sha256 freeze_task official_state_before release_ref plan_revision formal_revision
""".split())|{"BASELINE","HARNESS","HARNESS_ADAPTIVE","CONTROL","TREATMENT","writer","reviewer","evidence_maintainer"}
DATA_KEYS.update("""numerator denominator sample_size observation_count known_count unknown_count cases count rate value
defined measurable available eligible reasons held_out_positives held_out_negatives improved regressed executions failures
false_positives false_negatives evidence_position latency_ns native_rework_count true_positives provider_tokens amount
patterns pattern sample_count complete completed excluded_count not_run failed passed skipped zero_denominator
observed_windows expected_windows precision recall provider_billed_tokens result_status objective evidence_count""".split())
ENUM.update("""NO_DENOMINATOR ZERO_DENOMINATOR UNKNOWN NOT_APPLICABLE NO_OBSERVATIONS INCOMPLETE_OR_FAILED READINESS_ONLY_NO_INDEPENDENT_OUTPUT CASE_REGRESSION NO_HELD_OUT_POSITIVE NO_HELD_OUT_NEGATIVE NO_HELD_OUT_GAIN TRUE FALSE MECHANICAL_CADENCE""".split())
SHA=re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
UUID=re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z",re.I)
NUM=re.compile(r"-?[0-9]{1,16}(?:\.[0-9]{1,16})?\Z")
TIME=re.compile(r"20[0-9-]{8}T[0-9:.]{8,24}(?:Z|\+00:00)\Z")
VERSION=re.compile(r"v?[0-9]{1,5}(?:\.[0-9]{1,5}){1,3}\Z")
DSN=re.compile(rb"(?:postgres(?:ql)?(?:\+[a-z]+)?|rediss?)://|(?:Authorization:|Bearer\s+)[A-Za-z0-9._=-]{12,}|-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----|\b(?:sk-(?:proj-|or-v1-|svcacct-)?|ghp_|github_pat_)[A-Za-z0-9_-]{20,}",re.I)
NODE=re.compile(r"(?:workshop/)?tests?/[A-Za-z0-9_./-]+\.py(?:::[A-Za-z_][A-Za-z0-9_]*)+|workshop/(?:acceptance|integration)/[A-Za-z0-9_./-]+\.py(?:::[A-Za-z_][A-Za-z0-9_]*)+")
RESULTS=set("passed failed skipped xfailed xpassed error rerun".split())
MODULE_KEYS=set(["application/"+p+".py" for p in ("adaptive_runtime","adaptive_instructions","adaptive_evaluation","agent_gateway","creative_review","creative_context","creative_context_store")]+["domain/creative_review.py"])
def sha(b):return hashlib.sha256(b).hexdigest()
def pack(v):return json.dumps(v,sort_keys=True,ensure_ascii=True,allow_nan=False,separators=(",",":")).encode()
def pairs(kv):
    d={}
    for k,v in kv:
        if k in d:raise ValueError()
        d[k]=v
    return d
def load_file(rootfd,path):
    # All path components below come from fixed literals. Refuse symlinks,
    # directories/devices as files, oversized files and races on directory traversal.
    fd=os.dup(rootfd)
    try:
        *folders,file=path.split("/")
        for folder in folders:
            nf=os.open(folder,os.O_DIRECTORY|os.O_NOFOLLOW|os.O_RDONLY,dir_fd=fd)
            os.close(fd);fd=nf
        rr=os.open(file,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
        with os.fdopen(rr,"rb") as h:
            st=os.fstat(h.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_size>FCAP:raise ValueError()
            b=h.read(FCAP+1)
            if len(b)!=st.st_size:raise ValueError()
        return b,json.loads(b,object_pairs_hook=pairs,parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
    finally:os.close(fd)
def safe(value,field="",depth=0):
    if depth>32:raise ValueError()
    if value is None or type(value) in (int,bool):return value
    if type(value) is float:
        if not math.isfinite(value):raise ValueError()
        return value
    if isinstance(value,dict):
        if len(value)>10000:raise ValueError()
        return {k:safe(v,k,depth+1) for k,v in value.items() if k in DATA_KEYS or k in MODULE_KEYS}
    if isinstance(value,list):
        if len(value)>30000:raise ValueError()
        return [safe(x,field,depth+1) for x in value]
    if not isinstance(value,str):raise ValueError()
    if value in ENUM or NUM.fullmatch(value) or TIME.fullmatch(value) or VERSION.fullmatch(value):return value
    if SHA.fullmatch(value) or UUID.fullmatch(value):return value
    if "lsn" in field and re.fullmatch(r"[0-9A-F]{1,8}/[0-9A-F]{1,8}",value,re.I):return value
    if field=="filename" and re.fullmatch(r"(?:novel_factory|agent_workshop)-[0-9.]+(?:-py3-none-any\.whl|\.tar\.gz)",value):return value
    return {"withheld_string_sha256":sha(value.encode())}
def cases(d):
    tests=d.get("tests")
    if not isinstance(tests,list) or len(tests)>20000:raise ValueError()
    rows=[]
    for row in tests:
        raw=row["nodeid"];outcome=row["outcome"]
        if not isinstance(raw,str) or len(raw)>10000 or outcome not in RESULTS:raise ValueError()
        stem,sep,param=raw.partition("[")
        valid=NODE.fullmatch(stem) and ".." not in stem and not DSN.search(stem.encode())
        node=stem if valid else "node.sha256."+sha(stem.encode())
        if sep:node+="[param.sha256."+sha(param.encode())+"]"
        rows.append({"nodeid":node,"outcome":outcome})
    counts={k:v for k,v in d.get("summary",{}).items() if k in RESULTS|{"total","collected","deselected"} and type(v) is int}
    return {"tests":rows,"summary":counts,"outcomes":dict(Counter(i["outcome"] for i in rows))}
def run_data(s,d):
    if d.get("suite")!=s or not re.fullmatch(r"[0-9a-f]{40}",str(d.get("source_commit",""))):raise ValueError()
    if type(d.get("exit_code")) is not int or type(d.get("elapsed_seconds")) not in (float,int):raise ValueError()
    w=d.get("working_tree_changes",[])
    if type(w) is not list:raise ValueError()
    return {"suite":s,"source_commit":d["source_commit"],"exit_code":d["exit_code"],"elapsed_seconds":d["elapsed_seconds"],
            "working_tree_change_count":len(w),"working_tree_changes_sha256":sha(pack(w))}
def export(s,where):
    if s not in SUITES:raise ValueError()
    root=Path.cwd().resolve()
    path=Path(os.path.abspath(where))
    allowed=(("ci-evidence","p57-"+s),("ci-evidence","p57",s))
    rel=path.relative_to(root).parts
    if rel not in allowed:raise ValueError()
    fd=os.open(root,os.O_DIRECTORY|os.O_RDONLY)
    try:
        for name in rel:
            nx=os.open(name,os.O_DIRECTORY|os.O_NOFOLLOW|os.O_RDONLY,dir_fd=fd)
            os.close(fd);fd=nx
        files=[] if s=="packaging" else ["run.json"] if s=="pitr" else ["run.json","cases.json"]
        if s in EXTRA:files.append(EXTRA[s])
        entries=[];missing=[]
        for f in files:
            try:raw,obj=load_file(fd,f)
            except FileNotFoundError:
                missing.append(f);continue
            if not isinstance(obj,dict):raise ValueError()
            if f=="cases.json":data=cases(obj)
            elif f=="run.json":data=run_data(s,obj)
            elif s=="comparison" and obj.get("public_data")!=PUBLIC_MARK:
                data={"unpublished_or_partial_comparison":True,"planned_windows":safe(obj.get("planned_windows"))}
            else:data=safe(obj)
            entries.append({"file":f,"source_bytes":len(raw),"source_sha256":sha(raw),"projection":data})
    finally:os.close(fd)
    return {"format":"P57-PUBLIC-EVIDENCE-1","suite":s,"missing":missing,"entries":entries}
def main():
    if len(sys.argv)!=3:raise SystemExit("usage: export.py SUITE ci-evidence/p57-SUITE")
    try:
        s,where=sys.argv[1:];out=export(s,where);data=pack(out)
        if DSN.search(data) or len(data)>8*1024*1024:raise ValueError()
        blob=gzip.compress(data,mtime=0)
        code=base64.b64encode(blob).decode("ascii")
        n=(len(code)+1999)//2000
        for i in range(n):
            print("P57EV1",s,str(i+1)+"/"+str(n),sha(data),sha(blob),code[i*2000:(i+1)*2000],flush=True)
        return int(bool(out["missing"]))
    except Exception:
        print("P57EV1_EXPORT_REFUSED",file=sys.stderr)
        return 2
if __name__=="__main__":raise SystemExit(main())
