"""Run from a checkout with the public docs and its dependencies installed.
Supply --js-dir with markdownlint-cli2, mermaid and jsdom under node_modules
and the accompanying parse.mjs. Set --python to the NF dev interpreter.
"""
import argparse, json, os, subprocess
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
here=Path(__file__).resolve().parent
parser=argparse.ArgumentParser();parser.add_argument('--root',type=Path,required=True);parser.add_argument('--output',type=Path,required=True);parser.add_argument('--js-dir',type=Path,required=True);parser.add_argument('--python',type=Path,required=True);args=parser.parse_args()
root=args.root.resolve();out=args.output.resolve();assets=args.js_dir.resolve();python=args.python.absolute();out.mkdir(parents=True,exist_ok=True)
files=['README.md','README.en.md',*(str(p.relative_to(root)) for lang in ['en','zh-CN'] for p in sorted((root/'docs'/lang).glob('*.md')))]
commands={'markdownlint.log':[assets/'node_modules/.bin/markdownlint-cli2','--config',here/'quality.markdownlint-cli2.jsonc',*files],'mermaid.jsonl':['node',assets/'parse.mjs',*files],'api.log':[python,'scripts/export_public_api.py','--check','docs/reference/catalog.json','--examples','docs/reference/examples.json'],'check.log':[python,here/'check.py',out,root]}
def run(item):
 name,cmd=item
 with (out/name).open('w') as dest:res=subprocess.run(cmd,cwd=root,stdout=dest,stderr=subprocess.STDOUT)
 return dict(log=name,exit_code=res.returncode,command=[str(x) for x in cmd])
with ThreadPoolExecutor(max_workers=len(commands)) as pool:results=list(pool.map(run,commands.items()))
(out/'commands.json').write_text(json.dumps(results,indent=2,ensure_ascii=False)+'\n')
for row in results:print(row['log'],row['exit_code'])
raise SystemExit(any(r['exit_code'] for r in results))
