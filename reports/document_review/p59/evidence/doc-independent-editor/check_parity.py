"""Compare the reader-facing examples, technical identifiers and diagram relationships."""
import argparse
import hashlib
import json
import re
import subprocess
from collections import Counter
from pathlib import Path
def fenced(text):
    return re.findall(r'^\x60{3}([^\s]+)[^\n]*\n(.*?)^\x60{3}', text, flags=re.M | re.S)
def diagrams(text):
    result=[]
    for typ,block in fenced(text):
        if typ != 'mermaid':
            continue
        result.append({
            'type': block.splitlines()[0],
            'edges': [('seq',*m.groups()) for m in re.finditer(r'(?m)^\s*(\w+)\s*(-+>>?)\s*(\w+)\s*:',block)] if 'sequenceDiagram' in block else
                     [('edge',*m.groups()) for m in re.finditer(r'(?m)^\s*(\w+|\[\*\])(?:\[.*?\]|\{.*?\})?\s*(-->)\s*(?:\|[^|]*\|)?\s*(\w+|\[\*\])',block)]
        })
    return result
def paragraph_hashes(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()
def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('root',type=Path)
    parser.add_argument('out',type=Path)
    args=parser.parse_args()
    root=args.root
    pairs=[(root/'README.md',root/'README.en.md')]
    chinese=sorted((root/'docs/zh-CN').glob('*.md'))
    english=sorted((root/'docs/en').glob('*.md'))
    assert len(chinese)==len(english)==10
    assert [p.name[:2] for p in chinese]==[p.name[:2] for p in english]
    pairs.extend(zip(chinese,english))
    reg=json.loads((root/'docs/reference/catalog.json').read_text())
    examples=json.loads((root/'docs/reference/examples.json').read_text())
    assert len(reg)==len(examples)==98
    data={'pairs': [], 'endpoints':len(reg), 'examples':len(examples), 'sample_kinds':dict(Counter(v['kind'] for v in examples.values()))}
    for zn,en in pairs:
        a,b=zn.read_text(),en.read_text()
        res={'zh':str(zn.relative_to(root)),'en':str(en.relative_to(root)), 'zh_sha256':paragraph_hashes(zn),'en_sha256':paragraph_hashes(en)}
        zblocks=[v for v in fenced(a) if v[0]!='mermaid'];eblocks=[v for v in fenced(b) if v[0]!='mermaid']
        res['code_blocks']={'zh':len(zblocks),'en':len(eblocks),'literal_match':zblocks==eblocks}
        res['diagram_count']=len(diagrams(a));res['same_diagram_topology']=diagrams(a)==diagrams(b)
        methods=lambda s: re.findall(r'^### \x60([^\x60]+)\x60$',s,flags=re.M)
        if methods(a):
            res.update(methods_in_same_order=methods(a)==methods(b),same_catalog=set(methods(a))==set(reg)==set(examples))
            missing=[]
            for name in reg:
                for src in (a,b):
                    section=src.split('### '+chr(96)+name+chr(96)+'\n',1)[1].split('\n### ',1)[0]
                    if not all(x in section for x in ['../reference/catalog.json#L','../reference/examples.json#L','../../']):missing.append(name)
            res['unlinked_entries']=missing
        args_schema=lambda s:sorted(set(re.findall(r'\b(?:NF_|AW_|P6_|[a-z]+_(?:get|list|register|resume|prepare|reconcile|migrate|repair)|WORLD|BELIEF|PLAN|SIMULATION|NOT_EVALUATED|UNKNOWN|RUNTIME_CAPACITY_LIMIT)[A-Z0-9_]*\b',s)))
        res['technical_id_symmetric_difference']=sorted(set(args_schema(a))^set(args_schema(b)))
        for typ,block in zblocks+eblocks:
            if typ=='bash':
                subprocess.run(['bash','-n'],input=block,text=True,check=True)
            elif typ=='json':
                json.loads(block)
        assert res['code_blocks']['literal_match'] and res['same_diagram_topology'], en
        if methods(a):
            assert len(methods(a))==len(set(methods(a)))==len(reg)
            assert res['methods_in_same_order'] and res['same_catalog'] and not res['unlinked_entries']
        data['pairs'].append(res)
    args.out.write_text(json.dumps(data,indent=2,ensure_ascii=False)+'\n')
    print('Pairs',len(pairs),'APIs',len(reg))
    for p in data['pairs']:
        print(p['en'],p['code_blocks'],p['diagram_count'],p['same_diagram_topology'],p['technical_id_symmetric_difference'][:20])
if __name__=='__main__':main()
