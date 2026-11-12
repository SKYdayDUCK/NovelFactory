"""Direct measurements on the unchanged 500-agent seed; no model or external calls."""
from hashlib import sha256
import cProfile
import io
import json
import os
from pathlib import Path
import pstats
from statistics import median
import tempfile
from time import perf_counter, process_time

from sqlalchemy import text
from benchmarks.p2_behavioral_load import isolated_kernel, make_population, hardware
from novel_factory.application.queries import WorldQueryService
from novel_factory.domain.canonical import fingerprint
from pydantic_core import from_json

def measure(fn, count=15):
    elapsed, cpu = [], []
    for i in range(count):
        start, clock = perf_counter(), process_time()
        fn()
        elapsed.append(1000*(perf_counter()-start))
        cpu.append(1000*(process_time()-clock))
    return {"wall_median_ms":median(elapsed),"cpu_median_ms":median(cpu),"wall_ms":elapsed,"cpu_ms":cpu}

def main():
    out = Path(os.environ["BENCH_RESULT"])
    with tempfile.TemporaryDirectory() as objects, isolated_kernel(os.environ["NF_TEST_DATABASE_URL"],Path(objects)) as (kernel,target):
        fixture=make_population(kernel,500,.8)
        wr=fixture["world"]["world_ref"]
        with kernel.db.uow(write=False) as r:
            query=text("select snapshot::text as raw, state_hash, pg_column_size(snapshot) as compressed_bytes from nf_revisions where world_revision_id=:id")
            row=r.conn.execute(query,{"id":wr["world_revision_id"]}).mappings().one()
            raw=row["raw"]; source=json.loads(raw); sig=sha256(raw.encode()).hexdigest()
            assert source == from_json(raw) and fingerprint(source)==row["state_hash"]
            kernel.revision(r,wr)
            witness=kernel._last_verified_projection
            verified=bool(witness and witness==(row["state_hash"],raw))
            timings={
                "canonical_fingerprint":measure(lambda:fingerprint(source)),
                "json_loads":measure(lambda:json.loads(raw)),
                "pydantic_core_from_json":measure(lambda:from_json(raw)),
                "full_sql_text_roundtrip":measure(lambda:r.conn.execute(query,{"id":wr["world_revision_id"]}).mappings().one()),
                "live_raw_digest_sql_roundtrip":measure(lambda:r.conn.execute(text("select encode(sha256(convert_to(snapshot::text,'UTF8')),'hex') from nf_revisions where world_revision_id=:id"),{"id":wr["world_revision_id"]}).scalar_one()),
                "kernel_hot_revision":measure(lambda:kernel.revision(r,wr)),
            }
            assert sig==r.conn.execute(text("select encode(sha256(convert_to(snapshot::text,'UTF8')),'hex') from nf_revisions where world_revision_id=:id"),{"id":wr["world_revision_id"]}).scalar_one()
            public={"raw_characters":len(raw),"raw_bytes":len(raw.encode()),"postgres_toast_bytes":row["compressed_bytes"],"witness_retained":verified,"timings":timings}
        service=WorldQueryService(kernel)
        cache_profile=cProfile.Profile()
        cache_profile.enable()
        for member in fixture["population"][:40]:
            service.query(kernel.authenticate(member["token"]),member["state"])
        cache_profile.disable()
        buf=io.StringIO();pstats.Stats(cache_profile,stream=buf).strip_dirs().sort_stats("cumulative").print_stats(35)
        public.update(environment=hardware(),observed_profile=buf.getvalue(),scope="Serial direct service calls, non-gating profile. Full network acceptance measured separately.")
        out.write_text(json.dumps(public,indent=2)+"\n")
if __name__=="__main__":
    main()
