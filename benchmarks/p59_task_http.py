"""End-to-end, role-scoped GET /api/v1/tasks/{id} comparison using production admission."""
import argparse
import asyncio
from collections import Counter
from contextlib import asynccontextmanager
from decimal import Decimal
import json
from pathlib import Path
from time import perf_counter, process_time

import httpx
from fastapi.encoders import jsonable_encoder

from benchmarks.http_load import SqlMeasurements, loopback_server, percentiles
from benchmarks.p59_db_candidates import private_kernel, task_fixture
from experiments.db_execution.coordinator import Coordinator
from experiments.db_execution.operations import TaskStatus
from novel_factory.application.kernel import Kernel
from novel_factory.contracts.registry import plain
from novel_factory.fixtures import load_fixture
from novel_factory.interfaces.admission import ConnectionBudget
from novel_factory.interfaces.http import create_app


@asynccontextmanager
async def select_kernel(kernel, mode):
    if mode != 'hybrid':
        yield Kernel(kernel.db, kernel.objects, joined_task_status=mode == 'sql')
        return
    # Preserve the stock synchronous HTTP business callback. Dispatched work
    # holds its existing admission slot while waiting; the database budget
    # remains shared with authentication and cannot grow with the queue.
    loop = asyncio.get_running_loop()
    async with Coordinator(kernel, workers=4, join_input=True, split_queues=True) as coordinator:
        class StatusKernel(Kernel):
            def get_task(self, principal, task_id):
                request = coordinator.submit(TaskStatus(principal, task_id))
                return asyncio.run_coroutine_threadsafe(request, loop).result()
        yield StatusKernel(kernel.db, kernel.objects)


async def trial(mode, count, concurrency):
    with private_kernel(4) as (kernel, _):
        books = [load_fixture(kernel), load_fixture(kernel)]
        calls = []
        for i in range(16):
            book = books[i % 2]
            grant = kernel.issue_grant(book['principal'], book['world_ref']['work_id'], role_id='writer')
            op = task_fixture(kernel, book, grant['principal'], 'writer', i)
            oracle = kernel.get_task(grant['principal'], op.task_id)
            calls.append((f'/api/v1/tasks/{op.task_id}', {'Authorization': 'Bearer '+grant['token']},
                          jsonable_encoder(plain(oracle), custom_encoder={Decimal: str})))
        async with select_kernel(kernel, mode) as target:
            budget = ConnectionBudget(api_processes=1, api_pool_size=4, worker_processes=0)
            app = create_app(target, connection_budget=budget)
            with loopback_server(app) as base:
                async with httpx.AsyncClient(base_url=base, timeout=15, trust_env=False) as client:
                    async def one(i):
                        path, headers, expected = calls[i % len(calls)]
                        begin = perf_counter()
                        try:
                            resp = await client.get(path, headers=headers)
                            status = str(resp.status_code)
                            ok = resp.status_code == 200 and resp.json() == expected
                        except httpx.HTTPError as exc:
                            status, ok = type(exc).__name__, False
                        return (i, status, ok, perf_counter() - begin)
                    warmup = [await one(i) for i in range(len(calls))]
                    if not all(row[2] for row in warmup):
                        raise ValueError('Warm-up returned a mismatched task')
                    async def worker(start):
                        return [await one(i) for i in range(start, count, concurrency)]
                    with SqlMeasurements(kernel.db.engine) as meters:
                        begin, cpu = perf_counter(), process_time()
                        chunks = await asyncio.gather(*(worker(i) for i in range(concurrency)))
                        elapsed = perf_counter() - begin
                        cpu_seconds = process_time() - cpu
                    sql = meters.result(elapsed)
        rows = sorted(r for chunk in chunks for r in chunk)
        verified = sum(r[2] for r in rows)
        return {'mode': mode, 'verified': verified, 'count': count, 'concurrency': concurrency,
                'goodput': verified / elapsed, 'seconds': elapsed, 'cpu_s_client_and_service': cpu_seconds,
                'latency': percentiles([r[3] for r in rows]), 'sql': sql,
                'outcomes': dict(Counter(r[1] for r in rows)), 'raw': rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--count', type=int, default=320)
    parser.add_argument('--concurrency', type=int, default=16)
    args = parser.parse_args()
    if not 1 <= args.concurrency <= args.count <= 5000:
        parser.error('Choose positive bounded concurrency and operation count')
    results = []
    for mode in ('baseline', 'sql', 'hybrid', 'hybrid', 'sql', 'baseline'):
        results.append(asyncio.run(trial(mode, args.count, args.concurrency)))
        print(mode, results[-1]['verified'], round(results[-1]['goodput'], 2), flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, indent=2) + '\n', encoding='utf-8')
    return int(any(r['verified'] != args.count for r in results))


if __name__ == '__main__':
    raise SystemExit(main())
