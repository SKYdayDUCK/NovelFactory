"""Independently check the final bilingual corrections to the API examples and prose."""
from pathlib import Path
import hashlib
import json
import re
from uuid import UUID
root=Path('NovelFactory')
reference=root/'docs/reference/examples.json'
examples=json.loads(reference.read_text())
source=Path('doc-independent-tech/path-examples.json')
observed=json.loads(source.read_text())
results=[]
for name,actual in observed.items():
    example=examples[name]
    fields=('kind','test','request','response','pathBindings')
    for field in fields:
        assert example[field]==actual[field],(name,field)
    assert set(re.findall(r'\{(.*?)\}', name))==set(example['pathBindings'])
    for value in example['pathBindings'].values(): UUID(value)
    results.append(dict(name=name,fixture=example['kind'],same_invocation_fields=list(fields)))
assert len(results)==len([e for e in examples.values() if e.get('pathBindings')])==6
for locale in ('zh-CN','en'):
    page=next((root/'docs'/locale).glob('07-*.md')).read_text()
    parts={m[0]:m[1] for m in re.findall(r'^### \x60([^\x60]+)\x60\n(.*?)(?=^### |^## |\Z)',page,re.M|re.S)}
    guard=parts['POST /api/v1/world/accept-guarded']
    assert 'BRANCH_CHANGED' in guard and 'UNPROVEN_FOOTPRINT' in guard
    assert 'SCHEMA_UNSUPPORTED' not in guard
    assert 'SCHEMA_UNSUPPORTED' in parts['POST /api/v1/world/accept']
    assert 'READ' in guard and 'ACCEPT' in guard
    assert '100,000' in parts['POST /api/v1/tasks/{task_id}/continuity']
    assert 'REPAIR' in parts['POST /api/v1/world/promotions'] or 'repair' in parts['POST /api/v1/world/promotions']
    assert 'operation_id' in parts['aw_delegate']
data={'sample_source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
      'public_examples_sha256':hashlib.sha256(reference.read_bytes()).hexdigest(),
      'same_invocation_path_tuples':results,'T07':'reviewed caller key and returned remote operation UUID in chapters 04/07',
      'T08':'reviewed repair promotion, acceptance / retry semantics, continuity default and qualified plan selector'}
Path('doc-independent-editor/final-api-amendments.json').write_text(json.dumps(data,indent=2)+'\n')
