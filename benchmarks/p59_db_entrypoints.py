"""Verify independent role-bound Agent calls and real MCP, including persistent audit work."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import tempfile
from time import perf_counter, process_time

from benchmarks.bound_stdio_throughput import provision, exercise, TOOLS
from benchmarks.http_load import percentiles, SqlMeasurements
from benchmarks.p2_behavioral_load import DatabaseObserver, isolated_kernel
from novel_factory.application.bound_tools import invoke_bound_tool
from novel_factory.interfaces.bindings import read_private_config, runtime_from_config


def measure(samples, directory):
    with isolated_kernel(os.environ['NF_TEST_DATABASE_URL'], directory / 'objects') as (kernel, url):
        roles = [provision(kernel, directory, i) for i in range(2)]
        internal = []
        with DatabaseObserver(url) as observer:
            for role in roles:
                runtime = runtime_from_config(read_private_config(role.path))
                durations = []
                try:
                    with SqlMeasurements(runtime.kernel.db.engine) as meter:
                        started, cpu = perf_counter(), process_time()
                        for i in range(samples):
                            tool = TOOLS[i % len(TOOLS)]
                            begin = perf_counter()
                            result = invoke_bound_tool(runtime, tool, {})
                            if result != role.expected[tool]:
                                raise ValueError('Bound result did not match role')
                            durations.append(perf_counter() - begin)
                        elapsed = perf_counter() - started
                        internal.append({'samples': samples, 'correct': len(durations), 'sql': meter.result(elapsed),
                                         'seconds': elapsed, 'correct_per_second': samples / elapsed,
                                         'cpu_seconds': process_time() - cpu, 'latency': percentiles(durations),
                                         'latencies_ms': [round(v * 1000, 4) for v in durations]})
                finally:
                    runtime.kernel.db.close()
        with DatabaseObserver(url) as mcp_observer:
            mcp = asyncio.run(exercise(kernel, roles, samples, 12))
        return {'internal_agent': internal, 'internal_database_observer': observer.result(),
                'mcp': mcp, 'mcp_database_observer': mcp_observer.result(), 'model_sends': 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--samples', type=int, default=60)
    args = parser.parse_args()
    if args.samples < 2:
        parser.error('samples must be at least 2')
    with tempfile.TemporaryDirectory() as root:
        result = measure(args.samples, Path(root))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    return int(any(phase['statuses'].get('OK', 0) != phase['offered'] for phase in result['mcp']))


if __name__ == '__main__':
    raise SystemExit(main())
