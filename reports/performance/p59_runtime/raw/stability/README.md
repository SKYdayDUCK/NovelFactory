# One-hour fixed-read evidence

Each manifest records the byte length and SHA-256 of the original JSON, the gzip byte stream and each ordered part. The original records include one row per scheduled request, sampled service statistics and PostgreSQL activity. Each run used 500 distinct task principals and a fixed historical read cut. The summary includes HTTP status, domain error codes, latencies, world-head and pool checks and any worker lease renewals.

To reconstruct a run, execute the following from this directory, replacing the manifest name as appropriate. Write the output to a fresh directory. No third-party Python modules are needed.

```python
import gzip
import hashlib
import json
from pathlib import Path
import tempfile

manifest = json.loads(Path("soak-baseline-manifest.json").read_text())
chunks = []
for part in manifest["parts"]:
    data = Path(part["name"]).read_bytes()
    assert len(data) == part["bytes"]
    assert hashlib.sha256(data).hexdigest() == part["sha256"]
    chunks.append(data)
packed = b"".join(chunks)
assert hashlib.sha256(packed).hexdigest() == manifest["gzip_sha256"]
raw = gzip.decompress(packed)
assert hashlib.sha256(raw).hexdigest() == manifest["uncompressed_sha256"]
output = Path(tempfile.mkdtemp()) / manifest["uncompressed_name"]
output.write_bytes(raw)
print(output)
```
