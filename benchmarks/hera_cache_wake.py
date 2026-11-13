"""Measure the existing authorized fixed-cut cache and commit-notification fallback."""
from argparse import ArgumentParser
from contextlib import nullcontext
from concurrent.futures import ThreadPoolExecutor
import gzip
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from time import perf_counter, sleep

from benchmarks.hera_mixed import population, task_spec
from benchmarks.http_load import SqlMeasurements, percentiles
from benchmarks.p2_behavioral_load import isolated_kernel
from novel_factory.application.queries import WorldQueryService
from novel_factory.domain.errors import DomainError
from novel_factory.infrastructure.supervisor_wake import wait_for_change, wake_listener
from tests.queries.conftest import request, seed, start


def cache_arms(kernel):
    env = {'kernel':kernel}
    world = seed(env)
    task = start(env,world)
    query = request(task)
    expected = None
    rows = []
    for capacity in (0,32,32,0):
        service = WorldQueryService(kernel,cache_size=capacity)
        latencies = []
        with SqlMeasurements(kernel.db.engine) as meter:
            begin = perf_counter()
            for _ in range(24):
                t0 = perf_counter()
                result = service.query(task['principal'],query)
                latencies.append(perf_counter()-t0)
                observed = result['payload'], result['dependency_handle']
                if expected is None:
                    expected = observed
                assert observed == expected
            sql = meter.result(perf_counter()-begin)
        rows.append(dict(capacity=capacity,calls=24,correct=24,hits=service.cache_hits,
                         latency_s=latencies,latency=percentiles(latencies),sql=sql))
    return rows


def wake_arms(kernel):
    grant, wr = population(kernel,1)[0]
    principal = grant['principal']
    rows = []
    with ThreadPoolExecutor(1) as pool:
        for mode in ('poll','listen','listen','poll'):
            for case in range(8):
                spec = task_spec(principal,wr)
                def produce():
                    sleep(.08)
                    receipt = kernel.create_task(principal,spec,spec['task_id'])
                    return perf_counter(), receipt
                # Establish LISTEN before the initial authoritative read. A lost
                # wake still triggers a periodic re-read after the timeout.
                scans = 0
                with wake_listener(kernel.db.engine) if mode=='listen' else nullcontext() as listener:
                    begin = perf_counter()
                    producer = pool.submit(produce)
                    deadline = begin+4
                    while perf_counter()<deadline:
                        scans += 1
                        try:
                            result = kernel.get_task(principal,spec['task_id'])
                            break
                        except DomainError as exc:
                            if exc.code != 'VIEW_DENIED':
                                raise
                        if listener is None:
                            sleep(.015)
                        else:
                            wait_for_change(listener,.3)
                    else:
                        raise RuntimeError('Missing committed task')
                    end = perf_counter()
                    committed, receipt = producer.result()
                assert result['task_id']==receipt['task_id']==spec['task_id']
                assert result['state']=='READY' and result['generation']==0 and result['world_ref']==wr
                rows.append(dict(mode=mode,case=case,task=spec['task_id'],authoritative_reads=scans,
                            begin=begin,producer_return=committed,detected=end,listener_slots=int(bool(listener))))
    return rows


if __name__=='__main__':
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args = parser.parse_args()
    with TemporaryDirectory() as tmp, isolated_kernel(os.environ['NF_TEST_DATABASE_URL'],Path(tmp)) as (kernel,_):
        result = dict(source=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),
                      caches=cache_arms(kernel),wake=wake_arms(kernel))
    with gzip.open(args.output,'wt') as out:
        json.dump(result,out,separators=(',',':'))
