"""Archive frozen CI evidence without altering source observations."""
from pathlib import Path
from collections import Counter
from datetime import datetime, timezone
import gzip, hashlib, json, re, subprocess, sys, zipfile
stage=sys.argv[1]
root=Path('/workspace/scratch/f6d7dc8c770b')
source=root/'control'/stage
repo=root/('NovelFactory' if stage=='ci-stage3' else 'NovelFactory-pr3')
dest=repo/'evidence/pr_merge_20261111'/stage.replace('-','_')
assert not dest.exists() or set(p.name for p in dest.iterdir())=={'archive_ci_evidence.py'}, 'Refuse overwrite'
index=json.loads((source/'index.json').read_text())
final=json.loads((source/'final_metadata.json').read_text())
sha=lambda data:hashlib.sha256(data).hexdigest()
TOP={'load.json','load.log','result.json','run.log','context-p3.json','context-p3.log','unit.xml','unit.log','postgres-all.xml','postgres-all.log','genesis-paired.json','genesis-paired.log','canonical-paired.json','canonical-paired.log','r1-acceptance.json','r1-acceptance.log','load-smoke-10.json','load-smoke-10.log','environment.log','migration.log','schema-validation.log','memory.log','memory-controlled.log','original-mechanisms.log','memory-service.log','SHA256.json','pitr.log'}
PREFIX=('source_snapshots/','run-1/','run-2/','context-historical-e0b0f2b/','pitr/','memory/','memory-controlled/','memory-service/','recovery/','acceptance/')
special={}
for category in ('mixed_loads','loads','open_arrival','stability','junit','context_paired','genesis_paired','canonical_paired','acceptance_matrix'):
 for row in index.get(category,[]):
  if row.get('path'): special[row['path']]=(category,row.get('sha256'))
def safe_text(data,label):
 try:text=data.decode('utf-8')
 except UnicodeDecodeError:return
 for url in re.findall(r'https?://[^\s"<>]+',text):
  if any(q in url.lower() for q in ('sig=','signature=','token=','x-amz-','jwt=')):
   raise ValueError('Signed/token URL in '+label)
 if label.endswith(('.json','.jsonl')):
  def check(value):
   if isinstance(value,dict):
    for k,v in value.items():
     if k.lower() in {'token','password','secret','authorization','access_token','refresh_token'} and isinstance(v,str) and v:
      raise ValueError('Credential-like field in '+label+' key '+k)
     check(v)
   elif isinstance(value,list):
    for item in value:check(item)
  if label.endswith('.json'):check(json.loads(text))
  else:
   for line in text.splitlines():
    if line.strip():check(json.loads(line))
 for word in ('access.json','credentials.json','.env'):
  assert Path(label).name != word, 'Private file name in selection'
def classify(member,rel):
 if rel in special:return special[rel][0]
 if member.startswith('pitr/') or member in ('pitr.log','SHA256.json'):return 'pitr_inventory'
 if member.endswith('.xml'):return 'junit'
 if 'source_snapshots/' in member:return 'source_snapshot'
 if 'paired' in member or member.startswith('context-historical-'):return 'paired_profile'
 if member=='context-p3.json':return 'paired_profile'
 if member.startswith('acceptance/') or member.startswith('recovery/') or member=='r1-acceptance.json':return 'acceptance_recovery'
 if member.startswith('memory'):return 'memory_evaluation'
 if member=='load-smoke-10.json':return 'smoke_load'
 return 'supporting_log'
selected=[];artifacts=[];omitted=[];pitr=[]
for verify in sorted(source.glob('*/*/download_verification.json')):
 v=json.loads(verify.read_text());ar=verify.parent;zpath=ar/'artifact.zip'
 assert v['digest_matches'] and 'sha256:'+sha(zpath.read_bytes())==v['expected_digest']==v['actual_digest']
 members=[]
 with zipfile.ZipFile(zpath) as z:
  znames=sorted(f.filename for f in z.infolist() if not f.is_dir())
  assert znames==sorted(v['members']), ('ZIP member inventory mismatch',verify)
  for member in znames:
   path=ar/'extracted'/member;data=path.read_bytes()
   assert data==z.read(member),('Extracted original differs from ZIP',path)
   rel=path.relative_to(source).as_posix()
   if member in TOP or member.startswith(PREFIX):
    safe_text(data,rel)
    if rel in special and special[rel][1]:assert sha(data)==special[rel][1],('Index raw hash mismatch',path)
    selected.append((path,classify(member,rel),v,member));members.append(member)
   else:
    reason='redundant generated short-volume material' if member.startswith('short-volume') else 'duplicate full JSON test report; JUnit preserved'
    omitted.append({'source':rel,'run_id':v['run_id'],'artifact_id':v['artifact_id'],'member':member,'sha256':sha(data),'bytes':len(data),'reason':reason})
 if (ar/'extracted/SHA256.json').exists():
  m=json.loads((ar/'extracted/SHA256.json').read_text());present=[];missing=[]
  for member,want in m.items():
   p=ar/'extracted'/member
   if not p.exists():missing.append(member)
   else:
    assert sha(p.read_bytes())==want, ('PITR internal digest mismatch',member)
    present.append(member)
  assert len(present)==8 and len(m)==11 and sorted(missing)==['pitr/archive_segment.py','pitr/restore.conf','pitr/source.conf']
  pitr.append({'run_id':v['run_id'],'artifact_id':v['artifact_id'],'manifest_entries':len(m),'actual_retained_manifest_entries':len(present),'retained':present,'missing':missing,'retained_internal_hashes_match':True,'all_manifest_files_restorable':False,'original_zip_retained':True,'archive_note':'Original 8/11 retained entries and SHA256.json only; missing generated historical files were never reconstructed.'})
  selected.append((zpath,'original_pitr_zip',v,None))
 safe_text(verify.read_bytes(),verify.relative_to(source).as_posix())
 selected.append((verify,'artifact_verification',v,None))
 artifacts.append({**v,'selected_extracted_members':members,'selected_extracted_member_count':len(members),'omitted_extracted_member_count':len(v['members'])-len(members)})
assert len(artifacts)==18
assert set(special)<=set(p.relative_to(source).as_posix() for p,_,_,_ in selected)
expected_counts={'mixed_loads':6,'open_arrival':2,'stability':2,'junit':4} if stage=='ci-stage2' else {'loads':20,'junit':4}
for category,expected in expected_counts.items():assert sum(c==category for _,c,_,_ in selected)==expected,(category,expected)
metadata_names=['index.json','overview.json','summary.md','analysis.md','checkout_tree_proof.json']
for name in metadata_names:
 p=source/name
 assert p.exists(),name+' is not finalized'
 safe_text(p.read_bytes(),name);selected.append((p,'final_analysis_metadata',None,None))
metadata_hashes={name:sha((source/name).read_bytes()) for name in metadata_names}
if 'details' in final:
 run_rows=[d['run'] for d in final['details']]
 jobs_by_run={d['run_id']:d['jobs'] for d in final['details']}
else:
 run_rows=final['runs'];jobs_by_run={d['run_id']:d['jobs'] for d in final['jobs']}
def get_jobs(value):return value.get('jobs',[]) if isinstance(value,dict) else value
run_fields=('id','name','head_sha','head_branch','event','status','conclusion','html_url','run_attempt','created_at','updated_at','run_started_at','path')
job_fields=('id','run_id','name','head_sha','status','conclusion','html_url','started_at','completed_at')
step_fields=('name','number','status','conclusion','started_at','completed_at')
runs=[]
for run in run_rows:
 assert run['status']=='completed' and run['head_sha']==index['checkpoint']
 row={k:run[k] for k in run_fields if k in run};row['jobs']=[]
 for job in get_jobs(jobs_by_run[run['id']]):
  assert job['status']=='completed'
  j={k:job[k] for k in job_fields if k in job}
  j['steps']=[{k:s[k] for k in step_fields if k in s} for s in job.get('steps',[])]
  row['jobs'].append(j)
 runs.append(row)
run_status={'checkpoint':index['checkpoint'],'metadata_collected_at':final.get('collected_at'),'source_final_metadata_sha256':sha((source/'final_metadata.json').read_bytes()),'runs':runs,'note':'Terminal run/job/step statuses only. Signed artifact download responses, actor/repository expansions and credentials are not retained here.'}
dest.mkdir(parents=True,exist_ok=True)
records=[];objects={};raw_stats=[]
for p,category,v,member in selected:
 data=p.read_bytes();raw_hash=sha(data);rel=p.relative_to(source).as_posix()
 if raw_hash not in objects:
  archive=rel+'.gz' if v is None else 'objects/'+raw_hash+'.gz'
  compressed=gzip.compress(data,compresslevel=9,mtime=0)
  (dest/archive).parent.mkdir(parents=True,exist_ok=True);(dest/archive).write_bytes(compressed)
  restored=gzip.decompress((dest/archive).read_bytes())
  assert restored==data and sha(restored)==raw_hash
  objects[raw_hash]={'archive':archive,'compression':'gzip','sha256':raw_hash,'bytes':len(data),'gzip_sha256':sha(compressed),'gzip_bytes':len(compressed)}
 record={'source':rel,'category':category,**objects[raw_hash]}
 if v:record.update(run_id=v['run_id'],artifact_id=v['artifact_id'],artifact_name=v['name'],artifact_expected_zip_digest=v['expected_digest'],zip_member=member)
 records.append(record)
 if category in ('mixed_loads','loads','open_arrival','stability','smoke_load'):
  x=json.loads(data)
  stats={'source':rel,'category':category,'raw_arrays':{k:len(x[k]) for k in ('raw_requests','raw_attempts','records','samples','world_commit_receipts') if isinstance(x.get(k),list)}}
  for k in ('capacity_gate','first_attempt_capacity_gate','bounded_recovery_business_gate','engineer_correctness','probe_gate','long_duration_gate','scheduled_requests','completed_requests','validated_successes'):
   if k in x:stats[k]=x[k]
  if category=='open_arrival':assert stats['raw_arrays']['raw_requests']==5000
  if category=='stability':assert stats['raw_arrays']['records']==12000
  raw_stats.append(stats)
mirrors=[]
for name in ('summary.md','analysis.md','checkout_tree_proof.json'):
 data=(source/name).read_bytes();(dest/name).write_bytes(data)
 mirrors.append({'path':name,'sha256':sha(data),'bytes':len(data),'source':name})
(dest/'run_job_status.json').write_text(json.dumps(run_status,ensure_ascii=False,indent=2)+'\n')
verifier='''"""Verify archived bytes; optionally restore original paths without overwriting."""
from pathlib import Path
import argparse,gzip,hashlib,json
def digest(data):return hashlib.sha256(data).hexdigest()
def main():
 parser=argparse.ArgumentParser(description=__doc__)
 parser.add_argument('--extract',type=Path,help='new directory for original relative paths')
 args=parser.parse_args();root=Path(__file__).resolve().parent
 m=json.loads((root/'archive_manifest.json').read_text());cache=set()
 for r in m['records']:
  b=(root/r['archive']).read_bytes()
  assert digest(b)==r['gzip_sha256'] and len(b)==r['gzip_bytes'],r['archive']
  raw=gzip.decompress(b)
  assert digest(raw)==r['sha256'] and len(raw)==r['bytes'],r['source']
  if args.extract:
   relative=Path(r['source'])
   assert not relative.is_absolute() and '..' not in relative.parts
   out=args.extract/relative;out.parent.mkdir(parents=True,exist_ok=True)
   with out.open('xb') as f:f.write(raw)
  cache.add(r['archive'])
 for r in m['readable_mirrors']+m['generated_control_files']:
  b=(root/r['path']).read_bytes()
  assert digest(b)==r['sha256'] and len(b)==r['bytes'],r['path']
 print(json.dumps({'status':'ARCHIVE_BYTES_VERIFIED','checkpoint':m['checkpoint'],'source_records':len(m['records']),'unique_gzip_objects':len(cache),'capacity_statuses_preserved':True}))
if __name__=='__main__':main()
'''
(dest/'verify_archive.py').write_text(verifier)
readme=f"""# {stage} 可长期追溯的 CI 证据归档

冻结被测提交：{index['checkpoint']}。本目录仅保存此历史检查点的实际终态证据；不把后续 HEAD、归档成功或工程绿色解释为容量通过。

覆盖 {len(runs)} 个终态 run、{sum(len(v['jobs']) for v in runs)} 个 job、全部 18 个已下载 artifact 的摘要与 GitHub SHA256 校验映射。全部原始业务负载、全部开环/稳定性原始请求、四份 JUnit、实际可得的 PITR 文件、关键配对结果、执行源码快照和必要验收/恢复记录均原字节保存。没有最佳运行筛选；每次失败、429、transport error 和未恢复请求仍在原始 JSON 中。

archive_manifest.json 为逐文件原始/压缩 SHA256、长度、source run/artifact/member 映射。objects/ 用原始 SHA256 去重相同字节；同一对象的每个来源记录都保留，去重不删除任何请求行。index.json.gz 是原完整审核索引，overview.json.gz 是原精简数据，summary.md / analysis.md / checkout_tree_proof.json 同时保留可读原件。run_job_status.json 仅保留终态 run/job/step 状态和稳定 GitHub 链接。

## 核验与恢复

    python verify_archive.py
    python verify_archive.py --extract /new/empty/archive-restore

验证会重新检查每个 gzip 的 SHA256、解压字节数与原始 SHA256，以及可读摘要和控制文件哈希。恢复到新的目录并拒绝覆盖文件，不执行归档中的脚本。

## 边界

- stage2 的两套 PITR 原 artifact 各仅包含 8/11 清单项，另有 SHA256 清单；原 ZIP 也保留。缺失 pitr/archive_segment.py、pitr/restore.conf、pitr/source.conf，没有补造，也不宣称完整清单可恢复。stage3 没有执行独立 PITR、1000 开环或 600 秒 workflow，不能从别阶段补入冒充本次。
- 原始配对实验的全部运行均保留，包括 canonical 墙钟变慢的运行；归档不重算或提高验收状态。
- 每阶段省略 1104 个可再生成短卷文件、两份短卷日志和四份与 JUnit 重复的完整 JSON 测试报告。原始下载 ZIP/解包文件未删除或修改；完整原成员名、ZIP digest 与省略文件哈希仍在索引/manifest。此目录没有声称保存被省略的字节。
- 不保留签名下载 URL、授权材料或私有运行状态。请求/模型/世界信息来自已有合成测试证据；不开展新模型调用。
"""
(dest/'README.md').write_text(readme)
writer=Path(__file__).read_bytes()
(dest/'archive_ci_evidence.py').write_bytes(writer)
generated=[]
for name in ('README.md','run_job_status.json','verify_archive.py','archive_ci_evidence.py'):
 data=(dest/name).read_bytes();generated.append({'path':name,'sha256':sha(data),'bytes':len(data)})
manifest={'schema_version':'ci-permanent-archive-1','stage':stage,'checkpoint':index['checkpoint'],'created_at_utc':datetime.now(timezone.utc).isoformat(),
 'source_final_analysis_hashes':metadata_hashes,'source_index_sha256':metadata_hashes['index.json'],'source_stage_directory':str(source),
 'run_count':len(runs),'job_count':sum(len(v['jobs']) for v in runs),'artifact_count':len(artifacts),'all_artifact_zip_digests_match':True,
 'source_record_count':len(records),'unique_gzip_object_count':len(objects),'source_bytes_counting_each_origin':sum(r['bytes'] for r in records),
 'unique_raw_bytes':sum(o['bytes'] for o in objects.values()),'gzip_bytes':sum(o['gzip_bytes'] for o in objects.values()),
 'category_counts':dict(Counter(r['category'] for r in records)),'records':records,'readable_mirrors':mirrors,'generated_control_files':generated,
 'raw_observations':raw_stats,'pitr_inventory':pitr,'artifact_verifications':artifacts,'omitted_source_files':omitted,
 'omission_counts':dict(Counter(r['reason'] for r in omitted)),
 'notes':['All selected sources are exact original bytes, including all failures and unfinished targets.','No remote GitHub write was performed by the archiver.','Archive validation is evidence integrity, not a capacity or business PASS.','Every selected extracted member was compared directly with its digest-verified original ZIP.','Missing historical PITR files remain missing.']}
(dest/'archive_manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
assert {name:sha((source/name).read_bytes()) for name in metadata_names}==metadata_hashes,'Final analysis changed during archive'
subprocess.run([sys.executable,str(dest/'verify_archive.py')],check=True)
files=[p for p in dest.rglob('*') if p.is_file()]
print(json.dumps({'stage':stage,'dest':str(dest),'checkpoint':index['checkpoint'],'source_records':len(records),'unique_gzip_objects':len(objects),'files':len(files),'total_bytes':sum(p.stat().st_size for p in files),'gzip_bytes':manifest['gzip_bytes'],'manifest_sha256':sha((dest/'archive_manifest.json').read_bytes()),'raw_observation_count':len(raw_stats),'raw_array_records':sum(sum(v['raw_arrays'].get(k,0) for k in ('raw_requests','raw_attempts','records')) for v in raw_stats),'pitr_inventory_counts':[[v['actual_retained_manifest_entries'],v['manifest_entries']] for v in pitr],'omitted':len(omitted)},ensure_ascii=False))
