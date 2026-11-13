"""Unmodified P2 workload; toggle product flag in new API processes only."""
import os
import runpy

from benchmarks.process_server import ServiceProcesses

if __name__ == '__main__':
    start_services = ServiceProcesses.__enter__
    def enter(self):
        old = os.environ.get('NF_HERA_STATIC_CONTRACTS')
        try:
            os.environ['NF_HERA_STATIC_CONTRACTS'] = os.environ['AT30_API_CONTRACT_PLAN']
            return start_services(self)
        finally:
            if old is None:
                del os.environ['NF_HERA_STATIC_CONTRACTS']
            else:
                os.environ['NF_HERA_STATIC_CONTRACTS'] = old
    ServiceProcesses.__enter__ = enter
    runpy.run_module('benchmarks.p2_behavioral_load', run_name='__main__')
