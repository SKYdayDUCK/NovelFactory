"""Compare isolated feature flags on the same PG17 database/object snapshot.

Run in the frozen baseline checkout with PYTHONPATH pointing there. Uses the
same measurement child as scion_paired.py. The seed process owns synthetic
tokens and sends them only by stdin. All variants use the same candidate SHA.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

from scion_paired import clone, digest, drop, encode, fresh_name, prepare
from benchmarks.p2_behavioral_load import isolated_kernel

COLUMNS = "NF_SCION_COLUMNS"
SCOPES = "NF_SCION_SCOPES"
REPLAY = "NF_SCION_REPLAY"
VARIANTS = {"off": [], "columns": [COLUMNS], "scopes": [SCOPES], "replay": [REPLAY],
            "all": [COLUMNS, SCOPES, REPLAY]}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--candidate", type=Path, required=True)
    p.add_argument("--cases", nargs="+", required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--repetitions", type=int, default=3)
    args = p.parse_args()
    config = json.loads(args.candidate.read_bytes())
    root = Path(config["root"]).resolve()
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True, cwd=root).strip()
    if sha != config["sha"]:
        raise ValueError("candidate checkout SHA mismatch")
    subprocess.run(["git", "diff", "--quiet", "HEAD", "--", "src", "benchmarks"], cwd=root, check=True)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    url = os.environ["NF_TEST_DATABASE_URL"]
    admin = create_engine(make_url(url).set(database="postgres"), isolation_level="AUTOCOMMIT")
    for case in args.cases:
        with tempfile.TemporaryDirectory() as temporary:
            objects = Path(temporary) / "base"
            with isolated_kernel(url, objects) as (kernel, template_url):
                fixture = prepare(kernel, case)
                fixture_sha = digest(fixture)
                kernel.db.close()
                for run in range(1, args.repetitions + 1):
                    order = list(VARIANTS)
                    shift = (2 * (run - 1)) % len(order)
                    order = order[shift:] + order[:shift]
                    if run % 2 == 0:
                        order.reverse()
                    for variant in order:
                        name = fresh_name()
                        clone(admin, name, make_url(template_url).database)
                        try:
                            with tempfile.TemporaryDirectory() as work:
                                target = shutil.copytree(objects, Path(work) / "objects")
                                arm_url = make_url(url).set(database=name).render_as_string(hide_password=False)
                                flags = {flag: str(int(flag in VARIANTS[variant]))
                                         for flag in (COLUMNS, SCOPES, REPLAY)}
                                msg = dict(case=case, fixture=fixture, fixture_sha256=fixture_sha,
                                           sha=sha, url=arm_url, objects=str(target),
                                           output=str(output / f"{case}-{variant}-{run}.json"),
                                           flags=flags)
                                env = dict(os.environ, PYTHONPATH=f"{root}/src:{root}")
                                env.update(flags)
                                print(f"MEASURE {case} {variant} {run}", flush=True)
                                subprocess.run([sys.executable, str(Path(__file__).resolve().with_name(
                                               "scion_paired.py")), "--child"], cwd=root, env=env,
                                               input=json.dumps(msg, default=encode), text=True, check=True)
                        finally:
                            drop(admin, name)
    admin.dispose()


if __name__ == "__main__":
    main()
