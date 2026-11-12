"""Compare each published example with its retained direct capture, and both tables with the sample."""
import re,json,hashlib,sys
from pathlib import Path
root,work,out=map(lambda x:Path(x).resolve(),sys.argv[1:])
examples=json.loads((root/'docs/reference/examples.json').read_text())
catalog=json.loads((root/'docs/reference/catalog.json').read_text())
fresh_path_captures=json.loads((work/'doc-independent-tech/path-examples.json').read_text())
issues=[];rows=[];json_fields={'string','null','integer','number','object','array','boolean'}
def typ(v):return {dict:'object',str:'string',list:'array',int:'integer',bool:'boolean',float:'number',type(None):'null'}[type(v)]
def table(path):
 t=path.read_text()
 return {n:dict(re.findall(r'`([A-Za-z0-9_]+): ([^`]+)`',s)) for n,s in re.findall(r'(?ms)^### `([^`]+)`\s+.*?^- \*\*(?:Result|返回)\*\* (.*?)\n',t)}
en,zh=[table(next((root/'docs'/lang).glob('07-*.md'))) for lang in ('en','zh-CN')]
for n,r in examples.items():
 k=n if n.startswith(('aw_','nf_')) else 'http_'+re.sub(r'\{[^}]+\}','id',catalog[n]['path']).strip('/').replace('-','_').replace('/','_')
 paths=[work/'api-capture/examples'/(k+'.json'),work/'runtime-doc-notes/api-extra'/(k+'.json')]
 raw=fresh_path_captures.get(n) or json.loads(next(x for x in paths if x.exists()).read_text())
 if 'pathBindings' in r and raw.get('pathBindings')!=r['pathBindings']:issues.append([n,'path capture differs'])
 if raw['response']!=r['response'] or raw['request']!=r['request']:issues.append([n,'raw capture differs'])
 fields=sorted(r['response']);p=r['response']
 for lang,t in (('zh',zh),('en',en)):
  for key in fields:
   if key not in t[n]:
    issues.append([n,lang,key,'absent']);continue
   observed=typ(p[key]);decl=t[n][key]
   core=decl.replace('UUID','string').split('(')[0].split('（')[0].strip().split('|')
   if observed not in core and not(observed=='integer' and 'number' in core):issues.append([n,lang,key,observed,decl])
 if set(zh[n])!=set(en[n]):issues.append([n,'bilingual output fields differ'])
 rows.append({'method':n,'captured_top_level':fields,'documented_top_level':sorted(en[n]),'response_sha256':hashlib.sha256(json.dumps(p,sort_keys=True,ensure_ascii=False).encode()).hexdigest()})
report={'count':len(rows),'capture_matches':len(rows)-sum(x[-1]=='raw capture differs' for x in issues),'checked_top_level_field_instances':sum(len(r['captured_top_level']) for r in rows),'issues':issues,'results':rows}
(out/'response-check.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n');print({k:v for k,v in report.items() if k!='results'})
sys.exit(bool(issues))
