"""Verify actual refs, product blobs and squash history before writing dev evidence."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import subprocess
import xml.etree.ElementTree as ET

def git(*args):
    return subprocess.check_output(['git',*args]).decode().rstrip('\n')
def sha(ref):
    return git('rev-parse',ref)
def tree(ref):
    return sha(ref+'^{tree}')
def entries(ref):
    records={}
    for item in git('ls-tree','-r','-z',ref).split('\0'):
        if item:
            meta,p=item.split('\t',1);records[p]=meta
    return records
parser=argparse.ArgumentParser()
parser.add_argument('main');parser.add_argument('restored');parser.add_argument('audit');parser.add_argument('public_head');parser.add_argument('evidence',type=Path)
parser.add_argument('--junit',type=Path,help='Optional full native product-only JUnit result')
args=parser.parse_args()
frozen='5e322e8af8ddb4465f68f8cab84c9685832461a9'
private=('reports/code_review/p59_comprehensive/','reports/performance/p59_optimization/','reports/document_review/p59/')
assert git('show','-s','--format=%P',args.main)==frozen
assert tree(args.main)==tree(args.public_head)
assert tree(args.restored)==tree(args.audit)
main=entries(args.main); dev=entries(args.restored); base=entries(frozen)
assert not any(k.startswith(private) for k in main)
assert all(dev.get(k)==v for k,v in main.items())
extra=sorted(dev.keys()-main.keys());assert all(x.startswith(private) for x in extra)
changed=sorted(x for x in main if main[x]!=base.get(x))
allowed=json.loads((args.evidence/'publication-manifest.json').read_text())['changed_product_files']
assert changed==sorted(x['path'] for x in allowed)
assert subprocess.run(['git','merge-base','--is-ancestor',args.audit,args.main]).returncode==1
runs=json.loads((args.evidence/'github-runs.json').read_text())
for row in runs['main']:
    assert row['status']=='completed',(row['name'],row['status'])
ci=next(x for x in runs['main'] if x['name']=='P1 and P2 production integrity')
assert ci['conclusion']=='success'
localxml=args.junit
if localxml is not None and localxml.exists():
    cases=list(ET.parse(localxml).iter('testcase'))
    localresults={'case_count':len(cases),'errors':sum(x.find('error') is not None for x in cases),
        'failures':sum(x.find('failure') is not None for x in cases),'skips':sum(x.find('skipped') is not None for x in cases),'junit_sha256':hashlib.sha256(localxml.read_bytes()).hexdigest()}
else:
    localresults=None
proof=dict(frozen_main=frozen,actual_main=sha(args.main),squash_parent=frozen,product_head=sha(args.public_head),audit_at_review=sha(args.audit),
    audit_restored=sha(args.restored),public_tree=tree(args.main),full_audit_tree=tree(args.restored),published_count=len(changed),published={p:main[p] for p in changed},
    excluded_workpapers=extra,main_path_count=len(main),development_path_count=len(dev),local_product_full_run=localresults,
    github_pr=32,github_action_runs=runs,project_issue_13='OPEN / NOT_MET',scion_pr_4='OPEN / DRAFT',
    evidence_note='This receipt itself and later verification files are additional dev-only paths.')
(args.evidence/'tree-and-ci.json').write_text(json.dumps(proof,ensure_ascii=False,indent=2)+'\n')
print(len(changed),len(extra),localresults)
