"""Measure SQL shape/resources and Python allocations for a fixed-cut compile."""
import json
from pathlib import Path
import sys
import tempfile
import time
import tracemalloc
from sqlalchemy import text
from benchmarks.context_profile import make_world,prepare
from novel_factory.application.kernel import Kernel
from novel_factory.application.queries import WorldQueryService
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.objects import ObjectStore

def profile(out):
    db=Database(pool_size=4)
    try:
        with tempfile.TemporaryDirectory() as path:
            k=Kernel(db,ObjectStore(path))
            world,items,_=make_world(k,250)
            request=prepare(k,world,items[:100])
            service=WorldQueryService(k)
            with db.engine.begin() as conn:
                conn.execute(text("SELECT pg_stat_statements_reset()"))
            tracemalloc.start(5)
            start=time.monotonic()
            result=service.context_compile(world["principal"],request)
            elapsed=time.monotonic()-start
            current,peak=tracemalloc.get_traced_memory()
            snap=tracemalloc.take_snapshot()
            tracemalloc.stop()
            assert len(result["compiled"]["bundle"]["facts"])==100
            with db.engine.connect() as conn:
                statement_rows = [dict(row) for row in conn.execute(text("""
                    SELECT queryid AS query_id,split_part(ltrim(query),' ',1) AS verb,
                           calls,rows, total_exec_time,mean_exec_time,shared_blks_hit,shared_blks_read,
                           shared_blks_dirtied,temp_blks_written,wal_bytes::bigint
                    FROM pg_stat_statements WHERE dbid=(SELECT oid FROM pg_database WHERE datname=current_database())
                    ORDER BY total_exec_time DESC
                """)).mappings()]
                info=dict(conn.execute(text("SELECT extversion, current_setting('server_version') AS postgres_version FROM pg_extension WHERE extname='pg_stat_statements'")).mappings().one())
        allocations=[]
        for stat in snap.statistics("lineno")[:20]:
            frame=stat.traceback[0]
            allocations.append({"file":Path(frame.filename).name,"line":frame.lineno,"count":stat.count,"bytes":stat.size})
        (out/"statement-allocation.json").write_text(json.dumps({"dataset":"same 250-item world / 100 subject compile / no external model","facts":100,"elapsed_seconds_with_tracemalloc":elapsed,"traced_current_bytes":current,"traced_peak_bytes":peak,"live_allocations_after_return":allocations, "query_stats":statement_rows,"extension":info},indent=2)+"\n")
    finally:
        db.close()
if __name__=="__main__":
    profile(Path(sys.argv[1]))
