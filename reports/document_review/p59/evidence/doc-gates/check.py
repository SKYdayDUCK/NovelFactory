"""Audit the bilingual public bundle from a checkout, without modifying it."""
import ast, collections, hashlib, json, re, subprocess, sys, urllib.parse
from pathlib import Path
root=Path(sys.argv[2]).resolve() if len(sys.argv)>2 else Path(__file__).resolve().parents[1]/'NovelFactory'
dest=Path(sys.argv[1]);dest.mkdir(parents=True,exist_ok=True)
P=lambda x:root/x
digest=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
catalog=json.loads(P('docs/reference/catalog.json').read_text());samples=json.loads(P('docs/reference/examples.json').read_text())
docs=[P('README.md'),P('README.en.md'),*sorted(P('docs/en').glob('*.md')),*sorted(P('docs/zh-CN').glob('*.md'))]
links=[];blocks=[];issues=[];dup=collections.defaultdict(list);phrase=[];numeric={};terms={};heads={};method_checks=[];link_files={}
def pos(p,line):return f'{p.relative_to(root)}:{line}'
def err(p,line,msg):issues.append(dict(at=pos(p,line),message=msg))
def slug(s):
 return re.sub(r'[^\w\- ]+','',s.lower(), flags=re.UNICODE).replace(' ','-')
def normal_number(s):return s.replace(',','').replace('，','')
def location(src,l):return src.count('\n',0,l)+1
defensive=re.compile('不代表|不保证|不等同于|并不意味着|不能说明|并非|需要注意的是|值得强调的是|严格来说|从某种意义上说|综上所述|值得一提的是|进一步赋能|强有力的支撑|does not mean|does not imply|does not guarantee|it is important to note|it is worth (?:noting|mentioning)|strictly speaking|in conclusion|it should be emphasized',re.I)
for p in docs:
 s=p.read_text();current=None;ino=None;body=[];method=None;seen=set();anchors=collections.Counter()
 epcodes=set()
 for l,line in enumerate(s.splitlines(),1):
  if current is not None:
   if not line.startswith('```'):
    body.append(line);continue
   code='\n'.join(body);item=dict(at=pos(p,ino),language=current,status='PASS',sha256=hashlib.sha256(code.encode()).hexdigest())
   if current=='bash':
    proc=subprocess.run(['bash','-n'],input=code,text=True,capture_output=True)
    if proc.returncode:item['status']='FAIL';err(p,ino,proc.stderr)
    for token in re.findall(r'(?<![\w/])((?:scripts|reports|benchmarks)/[\w/.\-]+\.(?:py|ya?ml|sh|json|toml))\b',code):
     if not P(token).exists():err(p,ino,'missing literal command input '+token)
   elif current=='json':
    try:json.loads(code)
    except ValueError as exc:item['status']='FAIL';err(p,ino,str(exc))
   elif current=='mermaid':item['status']='see mermaid.jsonl'
   blocks.append(item);current=None;body=[];continue
  m=re.match(r'^```(\S*)\s*$',line)
  if m:current=m[1];ino=l;continue
  if re.match(r'^#{1,6} ',line):anchors[slug(line.split(' ',1)[1])]+=1
  m=re.match(r'^### `([^`]+)`$',line)
  if m and p.name.startswith('07'):
   method=m[1];seen.add(method)
  if p.name.startswith('07') and re.match(r'^- \*\*(Errors|错误)\*\*',line):epcodes.update(re.findall(r'`([A-Z0-9_]+)`',line))
  for match in re.finditer(r'(?<!!)\[([^\]]+)\]\(([^)]+)\)',line):
   label,url=match.groups();parsed=urllib.parse.urlparse(url);ref=urllib.parse.unquote(parsed.path)
   r=dict(at=pos(p,l),target=url)
   if parsed.scheme:
    r['kind']='external';links.append(r);continue
   target=(p.parent/ref).resolve() if ref else p
   r['kind']='internal'
   if not target.is_relative_to(root):err(p,l,'outside repo: '+url)
   elif not target.exists():err(p,l,'missing link: '+url)
   elif parsed.fragment and target.is_file():
    content=link_files.setdefault(target,target.read_text().splitlines()) if target not in link_files else link_files[target]
    lm=re.fullmatch(r'L(\d+)(?:-L(\d+))?',parsed.fragment)
    if lm:
     start=int(lm[1]);end=int(lm[2] or start)
     if not 1<=start<=end<=len(content):err(p,l,'source line out of bounds: '+url)
     elif method and target.name in ('catalog.json','examples.json') and content[start-1].strip()!=json.dumps(method,ensure_ascii=False)+': {':err(p,l,'anchor is not method '+method)
     elif method and target.suffix=='.py' and label in ('Handler','实现'):
      if not re.match(r'^\s*(?:async\s+)?def |^\s*@',content[start-1]):
       err(p,l,'anchor is not handler start: '+url)
     r['kind']='source_line'
    elif parsed.fragment not in {slug(x.split(' ',1)[1]) for x in content if re.match(r'^#{1,6} ',x)}:err(p,l,'unknown heading anchor: '+url)
   links.append(r)
  if m:=defensive.search(line):phrase.append(dict(at=pos(p,l),match=m[0]))
  if re.search(r'reports/(?:code_review/p59_comprehensive|performance/p59_optimization|document_review/p59)/|/workspace/scratch/|runtime-doc-notes',line):err(p,l,'internal P59 evidence path in public text')
  if line.strip() and not p.name.startswith('07') and not line.startswith(('|','#','- ','[','`')) and len(line)>85:dup[line.strip()].append(pos(p,l))
 if current is not None:err(p,ino,'unterminated fence')
 lang='zh' if 'zh-CN' in str(p) or p.name=='README.md' else 'en'
 no=p.name[:2] if p.parent.name in ('en','zh-CN') else '00'
 if p.name.startswith('07'):
  if seen!=set(catalog):err(p,1,'method headings differ: '+str(seen^set(catalog)))
  method_checks.append(dict(file=str(p.relative_to(root)),unique_methods=len(seen),stable_error_tokens=len(epcodes)))
 heads[no,lang]=list(anchors)
 numeric[no,lang]=set(normal_number(x) for x in re.findall(r'(?<![\d.a-z])\d[\d,]*\.\d+(?![\d.a-z])',s))
 terms[no,lang]=set(re.findall(r'(?<![A-Za-z0-9_])(?:[A-Z][A-Z_]{3,}|[AN]F_[A-Z0-9_]+|aw_[a-z][a-z_]*|nf_[a-z][a-z_]*)(?![A-Za-z0-9_])',s))
fences_by_language=collections.defaultdict(list)
for row in blocks:
 path=row["at"].split(":",1)[0]; p=Path(path)
 no=p.name[:2] if p.parent.name in ("en","zh-CN") else "00"
 lang="zh" if p.parent.name=="zh-CN" or p.name=="README.md" else "en"
 if row["language"] in ("bash","json"):fences_by_language[no,lang].append((row["language"],row["sha256"]))
for i in range(11):
 no=f'{i:02}';z='zh';e='en'
 assert (no,z) in heads and (no,e) in heads
def jsonfile(name,vals):(dest/name).write_text(json.dumps(vals,ensure_ascii=False,indent=2)+'\n')
pairs=[];bilingual_mismatches=[]
for i in range(11):
 no=f'{i:02}';dn=numeric[no,'en']^numeric[no,'zh'];dt=terms[no,'en']^terms[no,'zh']
 same=fences_by_language[no,'zh']==fences_by_language[no,'en']
 if not same:issues.append(dict(at='chapter '+no,message='Chinese and English runnable examples differ'))
 pairs.append(dict(chapter=no,identical_shell_and_json_examples=same,heading_counts=[len(heads[no,'zh']),len(heads[no,'en'])],decimal_tokens_only_in_one_language=sorted(dn),identifier_tokens_only_in_one_language=sorted(dt)))
 if dn:bilingual_mismatches.append((no,dn))
if set(samples)!=set(catalog):issues.append({'at':'docs/reference/examples.json','message':'example/catalog sets differ'})
hist=collections.Counter(r['kind'] for r in links)
result=dict(files=len(docs),links=dict(hist),blocks=dict(collections.Counter(r['language'] for r in blocks)),endpoints=dict(collections.Counter(v['transport'] for v in catalog.values())),duplicate_body_paragraphs=sum(len(v)>1 for v in dup.values()),screened_phrase_occurrences=len(phrase),issues=issues)
jsonfile('summary.json',result);jsonfile('links.json',links);jsonfile('blocks.json',blocks);jsonfile('bilingual.json',pairs);jsonfile('styles.json',{'repeated_non_reference_paragraphs':[v for v in dup.values() if len(v)>1],'flagged_phrases':phrase});jsonfile('api-coverage.json',method_checks);jsonfile('inputs.json',{str(p.relative_to(root)):digest(p) for p in [*docs,P('docs/reference/catalog.json'),P('docs/reference/examples.json'),P('scripts/export_public_api.py')]})
print(json.dumps(result,indent=2,ensure_ascii=False));sys.exit(bool(issues))
