"""Build and verify a product-only tree in a temporary index; leave checkout intact."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile


def git(*args, env=None):
    return subprocess.check_output(['git', *args], env=env)


def entries(ref):
    result = {}
    for item in git('ls-tree', '-rz', ref).decode().split('\0'):
        if item:
            meta,path=item.split('\t',1)
            mode,kind,sha = meta.split()
            result[path] = dict(path=path, mode=mode, type=kind, sha=sha)
    return result


parser=argparse.ArgumentParser()
parser.add_argument('base');parser.add_argument('head');parser.add_argument('output',type=Path)
args=parser.parse_args()
base=entries(args.base); current=entries(args.head)
private=('reports/code_review/p59_comprehensive/','reports/performance/p59_optimization/','reports/document_review/p59/')
allowed=('.github/workflows/','src/novel_factory/','agent_runtime/','scripts/','tests/','docs/zh-CN/','docs/en/','docs/reference/','contracts/','validation/')
allowed_exact={'README.md','README.en.md'}
include=[];omit=[]
for path in sorted(base.keys() | current.keys()):
    if base.get(path)==current.get(path): continue
    if path.startswith(private):
        assert path not in base, path
        omit.append(path)
        continue
    assert path in allowed_exact or path.startswith(allowed), path
    assert path in current, path
    include.append(current[path])
with tempfile.TemporaryDirectory() as tmp:
    env={**os.environ,'GIT_INDEX_FILE':str(Path(tmp)/'product-index')}
    git('read-tree',args.base,env=env)
    lines=''.join(f'{e["mode"]} {e["sha"]}\t{e["path"]}\0' for e in include).encode()
    subprocess.run(['git','update-index','-z','--index-info'],env=env,input=lines,check=True)
    tree=git('write-tree',env=env).decode().strip()
public=entries(tree)
assert not any(k.startswith(private) for k in public)
for key in public: assert public[key]==current[key], key
assert len(current)==len(public)+len(omit)
manifest=dict(base=args.base,review_head=args.head,base_tree=git('rev-parse',args.base+'^{tree}').decode().strip(),
              public_tree=tree,changed_product_files=include,excluded_internal_paths=omit)
args.output.write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
print('product files:',len(include),'internal paths:',len(omit),'public tree:',tree)
