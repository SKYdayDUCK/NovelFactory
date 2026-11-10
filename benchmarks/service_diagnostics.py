"""Opt-in benchmark observations; never capture credentials or request payloads.

Timers are inclusive (nested stages must not be summed). Thread CPU separates
Python work from blocking, while scheduler/SQL samples describe actual waits.
This module is not imported by production code and changes no admission limits.
"""
from __future__ import annotations

import os
import resource
import sys
import threading
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from functools import wraps
from pathlib import Path


class ServiceDiagnostics:
    def __init__(self):
        self.lock = threading.Lock()
        self.stages = defaultdict(lambda: {'calls': 0, 'errors': 0, 'wall_seconds': 0.0,
            'thread_cpu_seconds': 0.0, 'max_wall_seconds': 0.0})
        self.samples = []
        self.started = time.monotonic()
        self.stop = threading.Event()
        self.originals = []

    def record(self, name, wall, cpu=0.0, error=False):
        with self.lock:
            row = self.stages[name]
            row['calls'] += 1
            row['errors'] += int(error)
            row['wall_seconds'] += wall
            row['thread_cpu_seconds'] += cpu
            row['max_wall_seconds'] = max(row['max_wall_seconds'], wall)

    def wrap(self, function, name):
        @wraps(function)
        def measured(*args, **kwargs):
            wall, cpu, failed = time.perf_counter(), time.thread_time(), True
            try:
                result = function(*args, **kwargs)
                failed = False
                return result
            finally:
                self.record(name, time.perf_counter()-wall, time.thread_time()-cpu, failed)
        return measured

    def replace(self, owner, key, value):
        self.originals.append((owner, key, getattr(owner, key)))
        setattr(owner, key, value)

    def aliases(self, original, name):
        wrapped = self.wrap(original, name)
        for module_name, module in list(sys.modules.items()):
            if module_name.startswith('novel_factory.') and module is not None:
                for key, value in list(vars(module).items()):
                    if value is original:
                        self.replace(module, key, wrapped)

    def install(self, db, kernel, service, app):
        import json

        from psycopg.types.json import set_json_loads

        from novel_factory.contracts import registry
        from novel_factory.domain import world
        from novel_factory.interfaces import http

        for owner, methods in ((type(kernel), ('authenticate', 'revision', 'propose', 'prepare',
                                                'accept', '_accept_transaction')),
                               (type(service), ('_sample', '_delivery_check', '_execute',
                                                'query', 'context_compile'))):
            for method in methods:
                self.replace(owner, method, self.wrap(getattr(owner, method),
                                                     owner.__name__+'.'+method))
        self.aliases(registry.validate_contract, 'contract.validation')
        self.aliases(world.replay, 'world.replay')
        self.replace(world, '_empty_snapshot', self.wrap(world._empty_snapshot, 'world.genesis'))
        # psycopg calls this loader only for JSON/JSONB database values.
        set_json_loads(self.wrap(json.loads, 'database.json_decode'))
        self.json_reset = lambda: set_json_loads(json.loads)
        self.replace(db.engine, 'connect', self.wrap(db.engine.connect, 'database.acquire_connection'))

        original_threadpool = http.run_in_threadpool
        async def measured_threadpool(function, *args, **kwargs):
            scheduled = time.perf_counter()
            def invoke():
                self.record('http.executor_wait', time.perf_counter()-scheduled)
                return function(*args, **kwargs)
            return await original_threadpool(invoke)
        self.replace(http, 'run_in_threadpool', measured_threadpool)

        for label in ('ingress', 'authentication_admission', 'admission'):
            controller = getattr(app.state, label)
            original_slot = controller.slot
            def make_slot(slot, stage):
                @asynccontextmanager
                async def observed(*args, **kwargs):
                    waiting = time.perf_counter()
                    entered = False
                    try:
                        async with slot(*args, **kwargs):
                            entered = True
                            self.record(stage+'.queue', time.perf_counter()-waiting)
                            yield
                    finally:
                        if not entered:
                            self.record(stage+'.rejected_before_entry', time.perf_counter()-waiting,
                                        error=True)
                return observed
            self.replace(controller, 'slot', make_slot(original_slot, label))
        self.thread = threading.Thread(target=self._monitor, daemon=True)
        self.thread.start()
        return self

    def _monitor(self):
        while not self.stop.is_set():
            usage = resource.getrusage(resource.RUSAGE_SELF)
            sample = {'elapsed_seconds': time.monotonic()-self.started,
                'user_cpu_seconds': usage.ru_utime, 'system_cpu_seconds': usage.ru_stime,
                'max_rss_kib': usage.ru_maxrss, 'load_average': list(os.getloadavg())}
            try:
                status = Path('/proc/self/status').read_text().splitlines()
                sample['rss_kib'] = int(next(x for x in status if x.startswith('VmRSS:')).split()[1])
                scheduler = [list(map(int, p.read_text().split()[:3]))
                             for p in Path('/proc/self/task').glob('*/schedstat')]
                sample['thread_count'] = len(scheduler)
                sample['scheduler_run_nanoseconds'] = sum(x[0] for x in scheduler)
                sample['scheduler_wait_nanoseconds'] = sum(x[1] for x in scheduler)
            except (OSError, StopIteration, ValueError):
                sample['proc_sample_unavailable'] = True
            with self.lock:
                if len(self.samples) < 18000:
                    self.samples.append(sample)
            self.stop.wait(.2)

    def snapshot(self):
        with self.lock:
            return {'inclusive_stage_timings': {key: dict(value) for key, value in self.stages.items()},
                'resource_samples': list(self.samples),
                'affinity_cpus': sorted(os.sched_getaffinity(0)) if hasattr(os, 'sched_getaffinity') else None,
                'sample_interval_seconds': .2,
                'scheduler_note': 'Sum over currently live Linux threads; exiting threads can make cumulative samples decrease. Not a complete process-wide wait counter.',
                'timing_note': 'Inclusive stages overlap. CPU is measured on the executing thread; '
                               'connection acquisition includes pool wait, pre-ping and connect.'}

    def close(self):
        self.stop.set()
        self.thread.join(2)
        for owner, key, original in reversed(self.originals):
            setattr(owner, key, original)
        self.json_reset()
