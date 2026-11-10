"""Build a wheel and install locked dependencies into a clean temporary venv."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=["P0", "P1"], default="P1",
                        help="P0 checks only modules present in the engineering/contracts stage")
    parser.add_argument("--verify-postgres", action="store_true",
                        help="P1: run packaged-source CLI/migration/HTTP/restore smoke with the clean wheel")
    parser.add_argument("--installer", choices=["uv", "pip"], default="uv",
                        help="Use uv or CPython venv + pip for the independent installation check")
    args = parser.parse_args()
    if args.verify_postgres and (args.phase != "P1" or not os.environ.get("NF_TEST_DATABASE_URL")):
        parser.error("--verify-postgres requires phase P1 and NF_TEST_DATABASE_URL")
    project = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="nf-clean-install-") as directory:
        root = Path(directory)
        venv = root / "venv"
        wheels = root / "wheels"
        installer = (["uv", "pip", "install", "--python", str(venv / "bin/python")]
                     if args.installer == "uv" else [str(venv / "bin/python"), "-m", "pip", "install"])
        steps = [
            (["uv", "venv", "--python", sys.executable, str(venv)] if args.installer == "uv"
             else [sys.executable, "-m", "venv", str(venv)]),
            [*installer, "--require-hashes", "-r", str(project / "requirements.lock")],
            ["uv", "build", "--wheel", "--out-dir", str(wheels), str(project)],
        ]
        for command in steps:
            print("RUN", json.dumps(command), flush=True)
            subprocess.run(command, check=True)
        wheel = next(wheels.glob("novel_factory-*.whl"))
        subprocess.run([*installer, "--no-deps", str(wheel)], check=True)
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        subprocess.run([str(venv / "bin/python"), "-c", """
import importlib, importlib.resources, json, sys
from pathlib import Path
phase = sys.argv[1]
modules = ['novel_factory', 'novel_factory.domain.types', 'novel_factory.contracts.registry',
           'novel_factory.fixtures', 'sqlalchemy', 'alembic', 'psycopg', 'fastapi', 'pydantic', 'pytest']
if phase == 'P1':
    modules += ['novel_factory.application.kernel', 'novel_factory.application.queries',
                'novel_factory.interfaces.http', 'novel_factory.orchestration.tasks',
                'novel_factory.infrastructure.objects', 'novel_factory.infrastructure.recovery']
locations = {name: str(importlib.import_module(name).__file__) for name in modules}
assert all('/venv/' in location for location in locations.values()), locations
data = importlib.resources.files('novel_factory') / 'data' / 'contracts' / 'core.schema.json'
schema = json.loads(data.read_text())
assert schema['$schema'].endswith('2020-12/schema')
profile_data = importlib.resources.files('novel_factory') / 'data' / 'runtime' / 'query_profiles.json'
assert len(json.loads(profile_data.read_text())['profiles']) == 10
if phase == 'P1':
    from novel_factory.application.query_policy import profiles
    assert len(profiles()) == 10
print(json.dumps({'status':'PASS', 'phase':phase, 'modules':locations, 'core_schema_packaged':True, 'runtime_profiles_packaged':10}, indent=2))
""", args.phase], check=True, cwd=root, env=env)
        if args.verify_postgres:
            subprocess.run([str(venv / "bin/python"), str(project / "scripts/reproduction_smoke.py")],
                           check=True, cwd=project, env=env)


if __name__ == "__main__":
    main()
