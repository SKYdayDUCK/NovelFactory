"""Profile a complete one-worker index drain on a fresh PostgreSQL database."""
import argparse
import cProfile
import gzip
import json
import os
import pstats
import subprocess
from collections import defaultdict
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

from sqlalchemy import event

from benchmarks.p2_behavioral_load import isolated_kernel
from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.derived_worker import DerivedWorldWorker
from novel_factory.application.supervisor import RuntimeSupervisor
from novel_factory.fixtures import load_fixture, point, run_command
from novel_factory.orchestration.processors import TaskDispatcher


def profile(kernel):
    book = load_fixture(kernel)
    principal, ref, ids = book['principal'], book['world_ref'], book['ids']
    for i in range(60):
        ref = run_command(kernel, principal, ref, 'RESOURCE_TRANSFER', {
            'from_account': ids['warehouse'], 'to_account': ids['relief'],
            'resource_id': ids['grain'], 'unit': 'bag', 'quantity': '1', 'at': point(30 + i),
        })['world_ref']
    scope = principal, ref['work_id'], ref['branch_id']
    TaskDispatcher(RuntimeSupervisor(kernel, AgentGateway(kernel, {})), *scope).switch('redis')
    statements = defaultdict(lambda: [0, 0.0])

    def before(conn, cursor, statement, params, ctx, many):
        ctx.index_started = perf_counter()

    def after(conn, cursor, statement, params, ctx, many):
        statements[statement][0] += 1
        statements[statement][1] += perf_counter() - ctx.index_started

    event.listen(kernel.db.engine, 'before_cursor_execute', before)
    event.listen(kernel.db.engine, 'after_cursor_execute', after)
    worker = DerivedWorldWorker(kernel)
    start = perf_counter()
    with cProfile.Profile() as profiler:
        rows = [worker.rebuild_one(*scope) for _ in range(60)]
    seconds = perf_counter() - start
    assert len({row['message_id'] for row in rows if row and row['consumed']}) == len(rows)
    event.remove(kernel.db.engine, 'before_cursor_execute', before)
    event.remove(kernel.db.engine, 'after_cursor_execute', after)
    with kernel.db.uow(write=False) as repo:
        marks = repo.many('index_watermarks', {'branch_id': ref['branch_id']})
        assert {row['index_name'] for row in marks} == {'graph', 'timeline'}
        assert all(row['world_revision_id'] == ref['world_revision_id'] for row in marks)
    stats = pstats.Stats(profiler)
    calls = sorted(((path, lineno, name, values[1], values[2], values[3])
                    for (path, lineno, name), values in stats.stats.items()),
                   key=lambda row: -row[-1])
    return {
        'revision': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
        'worker_count': 1, 'completed': len(rows), 'elapsed_s': seconds,
        'stat_fields': ['file', 'line', 'name', 'calls', 'self_seconds', 'cumulative_seconds'],
        'profile': calls, 'sql': [
            {'statement': statement, 'calls': count, 'seconds': duration}
            for statement, (count, duration) in statements.items()
        ],
    }


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    with TemporaryDirectory() as tmp, isolated_kernel(os.environ['NF_TEST_DATABASE_URL'], Path(tmp)) as (kernel, _):
        with gzip.open(args.output, 'wt') as target:
            json.dump(profile(kernel), target, separators=(',', ':'))
