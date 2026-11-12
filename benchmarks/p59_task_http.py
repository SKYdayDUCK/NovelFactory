"""End-to-end, role-scoped GET /api/v1/tasks/{id} comparison using production admission."""
import argparse
import asyncio
from collections import Counter
from contextlib import asynccontextmanager
from datetime import datetime, timezone
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
from novel_factory.interfaces.http import create_app, HttpSettings


@asynccontextmanager
async def select_kernel(kernel, mode):
    if mode in ('baseline', 'sql', 'fenced'):
        yield Kernel(kernel.db, kernel.objects, joined_task_status=mode == 'sql')
        return
    # Preserve the stock synchronous HTTP business callback. Dispatched work
    # holds its existing admission slot while waiting; the database budget
    # remains shared with authentication and cannot grow with the queue.
    loop = asyncio.get_running_loop()
    if mode not in ('micro', 'hybrid'):
        raise ValueError("Unknown task status candidate")
    async with Coordinator(kernel, workers=4, batch=32, window_ms=2, join_input=mode == 'hybrid',
                           split_queues=mode == 'hybrid') as coordinator:
        class StatusKernel(Kernel):
            def get_task(self, principal, task_id):
                request = coordinator.submit(TaskStatus(principal, task_id))
                return asyncio.run_coroutine_threadsafe(request, loop).result()
        yield StatusKernel(kernel.db, kernel.objects)


async def trial(mode, count, concurrency, *, mixed_status=False):
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
        leases = []
        lease_bounds = []
        renewed_expiries = [[], []]
        if mixed_status:
            for i, book in enumerate(books):
                owner = book['principal']
                op = task_fixture(kernel, book, owner, 'control_operator', i)
                worker = f"fixture-lease-owner-{i}"
                claim = kernel.claim_task(owner, worker, lease_seconds=300, task_id=op.task_id)
                leases.append((op.task_id, claim['generation'], worker))
                lease_bounds.append((claim['lease_expiry'], claim['deadline']))
        async with select_kernel(kernel, mode) as target:
            budget = ConnectionBudget(api_processes=1, api_pool_size=4, worker_processes=0)
            settings = HttpSettings(fenced_task_status=mode == 'fenced')
            app = create_app(target, settings=settings, connection_budget=budget)
            with loopback_server(app) as base:
                async with httpx.AsyncClient(base_url=base, timeout=15, trust_env=False) as client:
                    async def one(i, *, measured=False):
                        if measured and mixed_status and i % 10 == 0:
                            book = (i // 10) % 2
                            begin = perf_counter()
                            try:
                                # The supervisor owns this durable lease. It shares the
                                # real HTTP database pool, never an HTTP transaction.
                                expiry = await asyncio.to_thread(kernel.tasks.renew, *leases[book], lease_seconds=3600)
                                initial_expiry, deadline = lease_bounds[book]
                                ok = max(initial_expiry, datetime.now(timezone.utc)) < expiry <= deadline
                                renewed_expiries[book].append(expiry)
                                status = 'RENEWED' if ok else 'INVALID_EXPIRY'
                            except Exception as exc:
                                ok, status = False, getattr(exc, 'code', type(exc).__name__)
                            return (i, status, ok, perf_counter() - begin)
                        read_seq = i - (i + 9) // 10 if measured and mixed_status else i
                        path, headers, expected = calls[read_seq % len(calls)]
                        begin = perf_counter()
                        try:
                            resp = await client.get(path, headers=headers)
                            status = str(resp.status_code)
                            ok = resp.status_code == 200 and resp.json() == expected
                        except (httpx.HTTPError, ValueError) as exc:
                            status, ok = type(exc).__name__, False
                        return (i, status, ok, perf_counter() - begin)
                    warmup = [await one(i) for i in range(len(calls))]
                    if not all(row[2] for row in warmup):
                        raise ValueError('Warm-up returned a mismatched task')
                    async def worker(start):
                        return [await one(i, measured=True) for i in range(start, count, concurrency)]
                    with SqlMeasurements(kernel.db.engine) as meters:
                        begin, cpu = perf_counter(), process_time()
                        chunks = await asyncio.gather(*(worker(i) for i in range(concurrency)))
                        elapsed = perf_counter() - begin
                        cpu_seconds = process_time() - cpu
                    sql = meters.result(elapsed)
        rows = sorted(r for chunk in chunks for r in chunk)
        verified = sum(r[2] for r in rows)
        result = {'mode': mode, 'verified': verified, 'count': count, 'concurrency': concurrency,
                'goodput': verified / elapsed, 'seconds': elapsed, 'cpu_s_client_and_service': cpu_seconds,
                'latency': percentiles([r[3] for r in rows]), 'sql': sql,
                'outcomes': dict(Counter(r[1] for r in rows)), 'raw': rows}
        if mixed_status:
            durable = []
            with kernel.db.uow(write=False) as repo:
                for book, (task_id, generation, worker) in enumerate(leases):
                    task = repo.one('tasks', {'task_id': task_id})
                    expected_count = sum(i % 10 == 0 and (i // 10) % 2 == book for i in range(count))
                    if (task['state'] != 'RUNNING' or task['generation'] != generation
                            or task['lease_owner'] != worker or len(renewed_expiries[book]) != expected_count):
                        durable.append(False)
                    else:
                        durable.append(task['lease_expiry'] == max(renewed_expiries[book])
                                       if expected_count else True)
            kinds = {'http_get': [r for r in rows if r[0] % 10], 'lease_renew': [r for r in rows if r[0] % 10 == 0]}
            result.update(mixed_status=True, durable_lease_receipts_match=durable,
                          counts_by_kind={k: len(rs) for k, rs in kinds.items()},
                          verified_by_kind={k: sum(r[2] for r in rs) for k, rs in kinds.items()},
                          latency_by_kind={k: percentiles([r[3] for r in rs]) for k, rs in kinds.items()})
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--count', type=int, default=320)
    parser.add_argument('--concurrency', type=int, default=16)
    parser.add_argument('--fenced', action='store_true', help='Run the registered singleton A/B/B/A')
    parser.add_argument('--mixed-status', action='store_true',
                        help='Replace every tenth GET with a trusted supervisor renewal on the same pool')
    args = parser.parse_args()
    if args.mixed_status and (not args.fenced or args.count < 20):
        parser.error('--mixed-status requires --fenced and at least 20 operations to exercise both leases')
    if not 1 <= args.concurrency <= args.count <= 5000:
        parser.error('Choose positive bounded concurrency and operation count')
    results = []
    modes = ('baseline', 'fenced') if args.fenced else ('baseline', 'sql', 'micro', 'hybrid')
    for index, mode in enumerate((*modes, *reversed(modes))):
        result = asyncio.run(trial(mode, args.count, args.concurrency, mixed_status=args.mixed_status))
        result.update(execution_index=index, order='forward' if index < len(modes) else 'reverse')
        results.append(result)
        print(mode, results[-1]['verified'], round(results[-1]['goodput'], 2), flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, indent=2) + '\n', encoding='utf-8')
    return int(any(r['verified'] != args.count or not all(r.get('durable_lease_receipts_match', ())) for r in results))


if __name__ == '__main__':
    raise SystemExit(main())
