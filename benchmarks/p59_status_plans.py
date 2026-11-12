"""Explain exact SELECTs issued by the typed read candidate in a fresh populated database."""
import argparse
import json
from pathlib import Path

from sqlalchemy import event

from benchmarks.p59_db_candidates import private_kernel, seed
from experiments.db_execution.operations import read_statuses


def capture(kernel, operations, expected, joined):
    statements = []

    def record(conn, cursor, sql, params, context, many):
        if sql.lstrip().upper().startswith('SELECT'):
            statements.append((sql, params))

    event.listen(kernel.db.engine, 'before_cursor_execute', record)
    try:
        answers = read_statuses(kernel, operations, join_input=joined)
    finally:
        event.remove(kernel.db.engine, 'before_cursor_execute', record)
    if answers != expected:
        raise AssertionError('Actual result differed from the status oracle')
    plans = []
    with kernel.db.uow(write=False) as repo:
        for sql, params in statements:
            result = repo.conn.exec_driver_sql('EXPLAIN (ANALYZE, BUFFERS, WAL, SETTINGS, FORMAT JSON) '+sql, params)
            plans.append(result.scalar_one()[0])
    return {'joined': joined, 'ids': len(operations), 'verified': len(answers), 'plans': plans}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--agents', type=int, default=200)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not 2 <= args.agents <= 2000:
        parser.error('Choose 2 to 2000 agents')
    with private_kernel(4) as (kernel, _):
        reads, _, expected = seed(kernel, args.agents)
        with kernel.db.engine.begin() as conn:
            for table in ('nf_tasks', 'nf_task_inputs', 'nf_grants', 'nf_works'):
                conn.exec_driver_sql('ANALYZE '+table)
        cases = [capture(kernel, reads[:n], expected[:n], joined)
                 for n in (1, 8, 32, 128) if n <= args.agents for joined in (False, True)]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({'population': args.agents, 'cases': cases}, indent=2)+'\n', encoding='utf-8')


if __name__ == '__main__':
    main()
