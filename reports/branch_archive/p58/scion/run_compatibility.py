"""Run the selected acceptance suite from a patched, disposable Scion worktree."""
import argparse
import os
from pathlib import Path
import subprocess
import sys


TESTS = [
    'tests/contracts/test_linked_schema.py',
    'tests/integration/test_scion_prime_pg.py',
    'tests/integration/test_scion_replay.py',
    'tests/integration/test_scion_scope_coverage.py',
    'tests/unit/test_scion_prime_parent.py',
    'tests/unit/test_scion_replay_proofs.py',
    'tests/unit/test_scion_column_switches.py',
    'tests/queries/test_snapshot_witness.py',
    'tests/integration/test_derived_worker.py',
    'tests/concurrency/test_world_races.py',
    'tests/integration/test_harness_isolation.py',
    'tests/reliability/test_objects.py',
    'tests/integration/test_p5_workflow_migration.py',
    'tests/integration/test_p58_routing.py',
    'tests/integration/test_p58_production_pipeline.py',
]


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('xml', type=Path)
    args = parser.parse_args()
    worktree = Path.cwd()
    paths = os.pathsep.join(map(str, [worktree / 'src', worktree]))
    env = {**os.environ, 'PYTHONPATH': paths, 'NF_SCION_SCHEMA_LINKS': '1', 'NF_SCION_SCOPES': '1'}
    subprocess.run([sys.executable, '-m', 'pytest', *TESTS, '-q', '--junitxml', str(args.xml.resolve())], env=env, check=True)
