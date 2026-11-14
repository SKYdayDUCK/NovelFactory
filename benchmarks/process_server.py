"""Isolated, bounded multi-process HTTP service for measured load experiments.

The load generator has its own interpreter/GIL. Every service process creates
its own SQLAlchemy pool after spawn and shares only a listening socket and the
existing authoritative PostgreSQL/object-store ports. The private pipe is a
measurement/control channel, not an application endpoint or authentication path.
No access tokens or payload bodies are written to disk.
"""
from __future__ import annotations

import multiprocessing
import os
import socket
import threading
import time
from collections import Counter
from pathlib import Path

import uvicorn

from benchmarks.http_load import SqlMeasurements
from novel_factory.application.kernel import Kernel
from novel_factory.application.queries import WorldQueryService
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.objects import ObjectStore
from novel_factory.interfaces.admission import ConnectionBudget
from novel_factory.interfaces.http import HttpSettings, create_app


def _resource_usage():
    """Process lifetime CPU seconds and peak resident memory in KiB.

    Windows FILETIMEs count 100 ns ticks and PeakWorkingSetSize counts bytes.
    Linux getrusage already reports seconds and ru_maxrss in KiB. Neither path
    substitutes current RSS for the lifetime peak; callers difference CPU only.
    """
    if os.name == 'nt':
        import win32api
        import win32process

        process = win32api.GetCurrentProcess()
        times = win32process.GetProcessTimes(process)
        memory = win32process.GetProcessMemoryInfo(process)
        return {'user_cpu_seconds': times['UserTime'] / 10_000_000,
                'system_cpu_seconds': times['KernelTime'] / 10_000_000,
                'peak_rss_kib': memory['PeakWorkingSetSize'] // 1024}
    import resource

    current = resource.getrusage(resource.RUSAGE_SELF)
    return {'user_cpu_seconds': current.ru_utime, 'system_cpu_seconds': current.ru_stime,
            'peak_rss_kib': current.ru_maxrss}


def _worker(channel, listener, url, objects, processes, pool_size):
    """Spawn target: inherited engines/connections are deliberately impossible."""
    db = Database(url, pool_size=pool_size, max_overflow=0)
    kernel = Kernel(db, ObjectStore(objects))
    service = WorldQueryService(kernel)
    budget = ConnectionBudget(api_processes=processes, api_pool_size=pool_size,
                              worker_processes=0)
    app = create_app(kernel, query_service=service, settings=HttpSettings(),
                     connection_budget=budget)
    server = uvicorn.Server(uvicorn.Config(app, log_level='error', access_log=False,
        lifespan='off', timeout_keep_alive=5))
    diagnostics = None
    if os.environ.get('NF_BENCH_DIAGNOSTICS') == '1':
        from benchmarks.service_diagnostics import ServiceDiagnostics
        diagnostics = ServiceDiagnostics().install(db, kernel, service, app)
    before = _resource_usage()
    started = time.monotonic()
    try:
        with SqlMeasurements(db.engine) as meter:
            thread = threading.Thread(target=server.run, kwargs={'sockets': [listener]}, daemon=True)
            thread.start()
            deadline = time.monotonic() + 20
            while not server.started:
                if not thread.is_alive() or time.monotonic() >= deadline:
                    raise RuntimeError('measurement service did not start')
                time.sleep(.01)
            channel.send({'ready': True})
            while True:
                command = channel.recv()
                if command == 'stop':
                    break
                if command != 'snapshot':
                    raise ValueError('unknown measurement control')
                current = _resource_usage()
                channel.send({'database': meter.result(time.monotonic() - started),
                    'cache_hits': service.cache_hits, 'cache_misses': service.cache_misses,
                    'admission': app.state.admission.snapshot(),
                    'authentication_admission': app.state.authentication_admission.snapshot(),
                    'ingress': app.state.ingress.snapshot(),
                    'resources': {
                        'user_cpu_seconds': current['user_cpu_seconds'] - before['user_cpu_seconds'],
                        'system_cpu_seconds': current['system_cpu_seconds'] - before['system_cpu_seconds'],
                        'peak_rss_kib': current['peak_rss_kib']},
                    'diagnostics': diagnostics.snapshot() if diagnostics else None})
    except (EOFError, BrokenPipeError):
        pass
    finally:
        server.should_exit = True
        if 'thread' in locals():
            thread.join(15)
        if diagnostics:
            diagnostics.close()
        db.close()
        listener.close()
        channel.close()


def aggregate(snapshots, duration):
    """Sum counters; preserve per-process peaks instead of inventing simultaneity."""
    databases = [row['database'] for row in snapshots]
    counters = {key: sum(row[key] for row in databases) for key in (
        'sql_statements', 'transactions', 'sql_cursor_execution_seconds', 'failed_statements',
        'checked_out_at_end', 'configured_pool_size', 'transaction_seconds_before_commit', 'finished_transactions')}
    kinds = Counter()
    for row in databases:
        kinds.update(row['statement_kinds'])
    counters.update(
        sql_statements_per_second=counters['sql_statements'] / max(duration, 1e-9),
        statement_kinds=dict(kinds),
        max_sql_cursor_execution_seconds=max(row['max_sql_cursor_execution_seconds'] for row in databases),
        peak_checked_out_connections=sum(row['peak_checked_out_connections'] for row in databases),
        peak_connections_note='Conservative sum of per-process peaks; not a simultaneous sample',
        sql_timing_excludes_result_decoding=True,
        max_transaction_seconds_before_commit=max(row['max_transaction_seconds_before_commit'] for row in databases),
        per_process=databases)
    result = {'database': counters,
        'cache_hits': sum(row['cache_hits'] for row in snapshots),
        'cache_misses': sum(row['cache_misses'] for row in snapshots),
        'service_processes': snapshots,
        'diagnostics': [row['diagnostics'] for row in snapshots if row.get('diagnostics')]}
    for kind in ('admission', 'authentication_admission', 'ingress'):
        keys = {key for row in snapshots for key in row[kind]}
        result[kind] = {key: sum(row[kind].get(key, 0) for row in snapshots) for key in keys}
        result[kind]['peak_note'] = 'Sum of process peaks; see process snapshots for exact values'
    result['resources'] = {
        'service_user_cpu_seconds': sum(row['resources']['user_cpu_seconds'] for row in snapshots),
        'service_system_cpu_seconds': sum(row['resources']['system_cpu_seconds'] for row in snapshots),
        'service_sum_peak_rss_kib': sum(row['resources']['peak_rss_kib'] for row in snapshots),
        'peak_rss_note': 'Sum of lifetime process peaks in KiB; not simultaneous RSS',
    }
    return result


class ServiceProcesses:
    """One or more API processes with a fixed aggregate pool, isolated client."""
    def __init__(self, url, objects: str | Path, *, processes=1, total_pool=8):
        if processes not in (1, 2, 4) or total_pool % processes or total_pool // processes < 2:
            raise ValueError('process count must divide the pool and leave >=2 slots each')
        self.url, self.objects = url, str(objects)
        self.processes, self.pool_size = processes, total_pool // processes
        self.children = []
        self.channels = []
        self.listener = None

    def __enter__(self):
        context = multiprocessing.get_context('spawn')
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(('127.0.0.1', 0))
        listener.listen(128)
        self.listener = listener
        self.base_url = f'http://127.0.0.1:{listener.getsockname()[1]}'
        try:
            for _ in range(self.processes):
                parent, child = context.Pipe()
                process = context.Process(target=_worker, args=(child, listener, self.url,
                    self.objects, self.processes, self.pool_size))
                process.start()
                child.close()
                self.children.append(process)
                self.channels.append(parent)
            for channel in self.channels:
                if not channel.poll(30) or channel.recv() != {'ready': True}:
                    raise RuntimeError('isolated service startup failed')
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def snapshot(self, duration=1):
        for channel in self.channels:
            channel.send('snapshot')
        snapshots = []
        for channel in self.channels:
            if not channel.poll(30):
                raise RuntimeError('isolated service metrics timed out')
            snapshots.append(channel.recv())
        return aggregate(snapshots, duration)

    def __exit__(self, *args):
        for channel in self.channels:
            try:
                channel.send('stop')
            except (BrokenPipeError, EOFError, OSError):
                pass
        for process in self.children:
            process.join(20)
            if process.is_alive():
                process.terminate()
                process.join(5)
        for channel in self.channels:
            channel.close()
        if self.listener:
            self.listener.close()
