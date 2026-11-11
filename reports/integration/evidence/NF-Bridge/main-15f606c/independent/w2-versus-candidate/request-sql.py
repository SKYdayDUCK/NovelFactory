"""Observe the actual SQL shapes of selected calls, outside the timed load windows."""
from collections import defaultdict
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import httpx
from sqlalchemy import event
from benchmarks.http_load import loopback_server
from benchmarks.p2_behavioral_load import isolated_kernel, make_population
from benchmarks.p3_open_arrival import validate_responses
from novel_factory.interfaces.http import create_app

def main(out):
    with tempfile.TemporaryDirectory(prefix="nf-bridge-sql-") as objects, isolated_kernel(os.environ["NF_TEST_DATABASE_URL"],Path(objects)) as (kernel, target):
        # Only fixtures used; no load or reduction of the original 1000-agent measurement.
        fixture = make_population(kernel, 10, .8)
        shapes = {}
        traces = {}
        current = []
        def before(conn, cursor, statement, params, context, many):
            # Never collect parameters. Normalize variable length batch bind placeholders.
            shape = re.sub(r"%\([\w_]+\)s","?", " ".join(statement.split()))
            digest = sha256(shape.encode()).hexdigest()
            current.append(digest)
            shapes[digest] = sorted(set(re.findall(r"\bnf_[a-z_]+\b",shape.lower())))
        event.listen(kernel.db.engine,"before_cursor_execute",before)
        try:
            with loopback_server(create_app(kernel)) as url, httpx.Client(base_url=url,trust_env=False) as http:
                for kind, idx in [('hot_first',0),('hot_repeat',0),('cold_first',9)]:
                    client = fixture["population"][idx]
                    encoded = json.dumps(client["state"],separators=(",",":"),ensure_ascii=False).encode()
                    current.clear()
                    response = http.post("/api/v1/queries/timeline",content=encoded,
                        headers={"Authorization":"Bearer "+client["token"], "Content-Type":"application/json"})
                    row = {'status':response.status_code,'_content':response.content}
                    validate_responses([row],fixture)
                    assert row['valid']
                    traces[kind] = {"status":row['status'],"validated":row['valid'],"ordered_sql_shape_sha256":list(current)}
        finally:
            event.remove(kernel.db.engine,"before_cursor_execute",before)
        Path(out).write_text(json.dumps(dict(sample_calls=traces,shape_tables=shapes,
            validation="Original benchmark validates UnifiedQueryResult, fixed WorldRef and both state items.",source="make_population; same HTTP request and validated response logic as p3_open_arrival, independent unsaturated diagnostic on one loopback API process, 10 generated fixture identities; 3 requests only."),indent=2)+"\n")
if __name__ == "__main__":
    main(sys.argv[1])
