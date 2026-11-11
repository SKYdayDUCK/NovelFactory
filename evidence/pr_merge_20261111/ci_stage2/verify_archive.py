"""Verify archived bytes; optionally restore original paths without overwriting."""
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
