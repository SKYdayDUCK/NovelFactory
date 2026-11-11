"""Direct measurements on the unchanged 500-agent seed; no model or external calls."""
from hashlib import sha256
from collections import defaultdict
from contextlib import ExitStack
import cProfile
import io
import json
import os
from pathlib import Path
import pstats
from statistics import median
import tempfile
from time import perf_counter, process_time

from sqlalchemy import event, text
from novel_factory.application import queries as query_module
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
            witness=getattr(kernel,"_last_verified_projection",None)
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
        phase = {}
        counts = {"calls": 0, "sql_s": 0.0}
        phase_rows = defaultdict(list)
        def before_sql(conn,cursor,statement,parameters,context,many):
            context.p55_profile_started = perf_counter()
        def after_sql(conn,cursor,statement,parameters,context,many):
            counts["calls"] += 1
            counts["sql_s"] += perf_counter()-context.p55_profile_started
        def instrument(owner, name, stack):
            original = getattr(owner,name,None)
            if original is None:
                return
            def wrapped(*args,**kwargs):
                t0, c0 = perf_counter(), process_time()
                n0, sql0 = counts["calls"], counts["sql_s"]
                try:
                    return original(*args,**kwargs)
                finally:
                    phase_rows[(phase["name"],name)].append({
                        "wall_ms":1000*(perf_counter()-t0), "cpu_ms":1000*(process_time()-c0),
                        "sql_statements":counts["calls"]-n0, "sql_cursor_ms":1000*(counts["sql_s"]-sql0)})
            setattr(owner,name,wrapped)
            stack.callback(setattr,owner,name,original)
        cache_profile=cProfile.Profile()
        with ExitStack() as stack:
            event.listen(kernel.db.engine,"before_cursor_execute",before_sql)
            event.listen(kernel.db.engine,"after_cursor_execute",after_sql)
            stack.callback(event.remove,kernel.db.engine,"before_cursor_execute",before_sql)
            stack.callback(event.remove,kernel.db.engine,"after_cursor_execute",after_sql)
            instrument(WorldQueryService,"_sample",stack)
            instrument(WorldQueryService,"_delivery_check",stack)
            instrument(query_module,"record_query",stack)
            for label in ("initial_delivery","exact_replay"):
                phase["name"] = label
                for member in fixture["population"][:40]:
                    service.query(kernel.authenticate(member["token"]),member["state"])
        public["delivery_measurements"]={
            label+"."+section:{"calls":len(rows),"median":{k:median(row[k] for row in rows) for k in rows[0]},"raw":rows}
            for (label,section),rows in phase_rows.items()}
        public["measurement_note"]="Substage counts are inclusive. record_query runs inside _delivery_check on S9 only. Both arms use the same preregistered logical seed with fresh isolated credentials."
        cache_profile.enable()
        for member in fixture["population"][:40]:
            service.query(kernel.authenticate(member["token"]),member["state"])
        cache_profile.disable()
        buf=io.StringIO();pstats.Stats(cache_profile,stream=buf).strip_dirs().sort_stats("cumulative").print_stats(35)
        public.update(environment=hardware(),observed_profile=buf.getvalue(),scope="Serial direct service calls, non-gating profile. Full network acceptance measured separately.")
        out.write_text(json.dumps(public,indent=2)+"\n")
if __name__=="__main__":
    main()
