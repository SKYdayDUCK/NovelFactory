"""Time an ordinary accepted command and its scope updates on real PostgreSQL."""
import argparse
from contextlib import contextmanager
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import time
from unittest.mock import patch
from sqlalchemy import text

from benchmarks.context_profile import SQLMeter, make_world
from novel_factory.application.kernel import Kernel
from novel_factory.domain.canonical import fingerprint
from novel_factory.fixtures import entity_value, point, prepare_command
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.objects import ObjectStore
from novel_factory.infrastructure.repository import KernelRepository

def run(output):
    db = Database(pool_size=4)
    meter = SQLMeter(db.engine)
    original = KernelRepository.bump
    rows = []
    def measured(repo, work_id, branch_id, keys):
        keys = list(keys)
        start = time.perf_counter()
        n = meter.count
        result = original(repo, work_id, branch_id, keys)
        rows[-1].update(bump_ms=1000*(time.perf_counter()-start), bump_sql=meter.count-n, keys=sorted(set(keys)), scope_versions=result)
        return result
    try:
        with tempfile.TemporaryDirectory() as objs:
            k = Kernel(db, ObjectStore(objs))
            w, items, owner = make_world(k,100)
            for i in range(7):
                req = prepare_command(k,w["principal"],w["world_ref"],"SET_RELATION",{"subject_id":items[0],"predicate":"legal_owner","object_value":entity_value(owner),"at":point(i+3)},key=f"p59-write-{i}")["request"]
                rows.append({"repeat":i})
                meter.reset()
                with patch.object(KernelRepository,"bump",measured):
                    start = time.perf_counter()
                    receipt = k.accept(w["principal"],req)
                    rows[-1]["accept_ms"] = 1000*(time.perf_counter()-start)
                rows[-1]["accept_sql"] = meter.count
                w["world_ref"] = receipt["world_ref"]
                assert k.accept(w["principal"],req) == receipt
            with db.engine.connect() as c:
                head = c.execute(text("select scope_key,version from nf_scopes where work_id=:w and branch_id=:b order by scope_key"),{"w":w["world_ref"]["work_id"],"b":w["world_ref"]["branch_id"]}).all()
                assert dict(head) == rows[-1]["scope_versions"]
                plan = c.execute(text("EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) SELECT scope_key,version FROM nf_scopes WHERE work_id=:w AND branch_id=:b ORDER BY scope_key"),{"w":w["world_ref"]["work_id"],"b":w["world_ref"]["branch_id"]}).scalar_one()
        output.write_text(json.dumps({"operation":"trusted kernel accept + idempotent replay", "rows":rows,"scope_map_plan":plan,"provider_calls":0},indent=2)+"\n")
    finally:
        db.close()

if __name__ == "__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("output",type=Path)
    run(parser.parse_args().output)
