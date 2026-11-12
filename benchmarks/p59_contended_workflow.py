"""Verify a two-book workflow while authorized background readers compete for its DB pool."""
import argparse
from concurrent.futures import ThreadPoolExecutor, wait
import json
import os
from pathlib import Path
import tempfile
from threading import Event
from time import perf_counter

from benchmarks.http_load import SqlMeasurements, percentiles
from benchmarks.p2_behavioral_load import DatabaseObserver, isolated_kernel
from benchmarks.p59_db_candidates import seed
from benchmarks.p59_workflow_load import trial


def run(readers, population):
    with tempfile.TemporaryDirectory() as root:
        with isolated_kernel(os.environ['NF_TEST_DATABASE_URL'], Path(root)) as (kernel, url):
            ops, _, expected = seed(kernel, max(2, readers))
            stop = Event()
            entered = [Event() for _ in range(readers)]

            def consume(index):
                times = []
                op = ops[index]
                while not stop.is_set():
                    start = perf_counter()
                    value = kernel.get_task(op.principal, op.task_id)
                    if value != expected[index]:
                        raise AssertionError('Reader received incorrect status')
                    times.append(perf_counter() - start)
                    entered[index].set()
                return times

            with DatabaseObserver(url) as observer, SqlMeasurements(kernel.db.engine) as meter:
                started = perf_counter()
                with ThreadPoolExecutor(max_workers=max(1, readers)) as pool:
                    pending = [pool.submit(consume, i) for i in range(readers)]
                    try:
                        if not all(flag.wait(10) for flag in entered):
                            raise TimeoutError('Reader readiness deadline exceeded')
                        results = trial(kernel, population)
                    finally:
                        stop.set()
                    wait(pending)
                    durations = [f.result() for f in pending]
                elapsed = perf_counter() - started
            return {'readers': readers, 'read_counts': [len(ds) for ds in durations],
                    'read_latency': percentiles([v for ds in durations for v in ds]),
                    'workflow': results, 'db': meter.result(elapsed), 'observer': observer.result()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--agents', default=50, type=int, choices=(50, 200))
    args = parser.parse_args()
    results = []
    for readers in (0, 4, 4, 0):
        results.append(run(readers, args.agents))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(results, indent=2)+'\n', encoding='utf-8')
    return int(any(r['workflow']['gate'] != 'PASS' for r in results))


if __name__ == '__main__':
    raise SystemExit(main())
