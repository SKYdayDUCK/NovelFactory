"""Compare row-at-a-time, pipeline and set SQL inside one private PostgreSQL transaction.

Uses connection-local disposable fixtures. Time includes checking every returned
key/value and commit. This microbenchmark cannot certify a NovelFactory endpoint.
"""
import argparse
from contextlib import nullcontext
import json
import os
from pathlib import Path
import platform
import resource
from time import perf_counter, process_time

import psycopg
from sqlalchemy.engine import make_url


def measure(conn, op, mode, keys):
    count = len(keys)
    placeholders = {
        'read': 'SELECT k,v FROM nf_probe WHERE k=%s',
        'insert': 'INSERT INTO nf_probe(k,v) VALUES (%s,0) RETURNING k,v',
        'conflict': 'INSERT INTO nf_probe(k,v) VALUES (%s,1) ON CONFLICT(k) DO UPDATE SET v=nf_probe.v+1 RETURNING k,v',
        'update': 'UPDATE nf_probe SET v=v+1 WHERE k=%s RETURNING k,v',
    }
    grouped = {
        'read': 'SELECT k,v FROM nf_probe WHERE k=ANY(%s::int[])',
        'insert': 'INSERT INTO nf_probe(k,v) SELECT k,0 FROM unnest(%s::int[]) AS a(k) RETURNING k,v',
        'conflict': 'INSERT INTO nf_probe(k,v) SELECT k,1 FROM unnest(%s::int[]) AS a(k) ON CONFLICT(k) DO UPDATE SET v=nf_probe.v+1 RETURNING k,v',
        'update': 'UPDATE nf_probe n SET v=n.v+1 FROM unnest(%s::int[]) AS a(k) WHERE n.k=a.k RETURNING n.k,n.v',
    }
    with conn.transaction():
        conn.execute('TRUNCATE nf_probe')
        if op != 'insert':
            conn.execute('INSERT INTO nf_probe SELECT i,0 FROM generate_series(1,%s) AS a(i)', (count,))
    begin, cpu = perf_counter(), process_time()
    with conn.transaction():
        if mode == 'set':
            rows = conn.execute(grouped[op], (keys,)).fetchall()
        else:
            with conn.pipeline() if mode == 'pipeline' else nullcontext():
                cursors = [conn.execute(placeholders[op], (key,)) for key in keys]
            rows = [cursor.fetchone() for cursor in cursors]
        value = int(op in {'update', 'conflict'})
        if sorted(rows) != [(key, value) for key in keys]:
            raise ValueError('Row result mismatch')
    elapsed = perf_counter() - begin
    return {'operation': op, 'method': mode, 'keys': count, 'elapsed_ms': 1000 * elapsed,
            'cpu_ms': 1000 * (process_time() - cpu), 'statements': 1 if mode == 'set' else count,
            'verified_rows': len(rows), 'rows_per_s': len(rows) / elapsed}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    url = make_url(os.environ['NF_TEST_DATABASE_URL']).set(drivername='postgresql')
    rows = []
    with psycopg.connect(url.render_as_string(hide_password=False), autocommit=True) as conn:
        version = conn.execute('SELECT version()').fetchone()[0]
        conn.execute('CREATE TEMP TABLE nf_probe(k int PRIMARY KEY,v int NOT NULL)')
        for count in (1, 8, 16, 32, 64, 128):
            for op in ('read', 'insert', 'update', 'conflict'):
                for mode in ('row', 'pipeline', 'set', 'set', 'pipeline', 'row'):
                    rows.append(measure(conn, op, mode, list(range(1, count + 1))))
        with conn.transaction():
            conn.execute('TRUNCATE nf_probe')
        try:
            with conn.transaction(), conn.pipeline():
                conn.execute('INSERT INTO nf_probe VALUES (1,1)')
                conn.execute('INSERT INTO nf_probe VALUES (1,2)')
        except psycopg.errors.UniqueViolation:
            pass
        else:
            raise AssertionError('Pipeline constraint violation missing')
        if conn.execute('SELECT count(*) FROM nf_probe').fetchone() != (0,):
            raise AssertionError('Failed pipeline left partial state')
    result = {'scope': 'one local connection, TEMP fixture, no inter-client locks or business authorization',
              'python': platform.python_version(), 'postgres': version, 'psycopg': psycopg.__version__,
              'pipeline_failure_rolled_back': True, 'application_connections': 1, 'driver_queue': 0,
              'peak_process_rss_kib_linux': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
              'measurement': 'One private session; no cross-session table contention. SQL executions exclude setup and transaction control. Protocol packets are not counted.',
              'records': rows}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
