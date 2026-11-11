"""Measure six isolated configurations on PG17 clones of the same world.

Run from the clean baseline checkout using its src on PYTHONPATH. The candidate
JSON has root, sha and final_env; it may be written after baseline collection.
The coordinator never writes the private in-memory population to evidence.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

import scion_exact_requests as exact
import scion_paired as paired
from benchmarks.p2_behavioral_load import isolated_kernel

FLAGS = (
    "NF_SCION_COLUMNS", "NF_SCION_WORKER_COLUMNS", "NF_SCION_REVIEW_COLUMNS",
    "NF_SCION_QUERY_COLUMNS", "NF_SCION_SCOPES", "NF_SCION_REPLAY",
    "NF_SCION_REPLAY_LAZY", "NF_SCION_SCHEMA_LINKS",
)
WORKER, REVIEW, SCOPE = FLAGS[1], FLAGS[2], FLAGS[4]
ENABLED = {"A": (), "B": (WORKER, REVIEW), "C": (SCOPE,), "D": (WORKER, REVIEW, SCOPE),
           "E": ("NF_SCION_REPLAY", "NF_SCION_REPLAY_LAZY")}


def check_source(root, expected):
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    if sha != expected:
        raise ValueError(f"source mismatch at {root}: {sha} vs {expected}")
    subprocess.run(["git", "diff", "--exit-code", "HEAD", "--", "src", "migrations"], cwd=root, check=True)
    extra = subprocess.check_output(["git", "ls-files", "--others", "--exclude-standard", "--", "src", "migrations"],
                                    cwd=root, text=True)
    if extra:
        raise ValueError(f"untracked application sources in {root}")


def measure(admin, base_url, job, source, arm, flags, output, repetition):
    case, template, objects, fixture, fingerprint = job
    db_name = paired.fresh_name()
    paired.clone(admin, db_name, template)
    try:
        with tempfile.TemporaryDirectory() as work:
            target = shutil.copytree(objects, Path(work) / "objects")
            root = Path(source["root"]).resolve()
            env = dict(os.environ, PYTHONPATH=f"{root}/src:{root}", **flags)
            harness = "scion_paired.py"
            child_case = case
            if case.endswith("-exact"):
                harness = "scion_exact_requests.py"
            elif case.startswith("overload-"):
                harness, child_case = "scion_ci_overload.py", case.replace("overload-", "hotspot-")
            elif case.startswith("steady-"):
                harness = "scion_stability_meter.py"
            message = dict(case=child_case, fixture=fixture, fixture_sha256=fingerprint, sha=source["sha"],
                           url=make_url(base_url).set(database=db_name).render_as_string(hide_password=False),
                           objects=str(target), flags=flags, output=str(output / f"{case}-{arm}-{repetition}.json"))
            print(f"MEASURE {case} {arm} {repetition}", flush=True)
            subprocess.run([sys.executable, str(Path(__file__).with_name(harness)), "--child"],
                           cwd=root, env=env, input=json.dumps(message, default=paired.encode), text=True, check=True)
    finally:
        paired.drop(admin, db_name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-sha", required=True)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--cases", required=True, nargs="+")
    parser.add_argument("--arms", nargs="+", choices=list("ABCDEF"), default=list("ABCDEF"))
    parser.add_argument("--repetitions", type=int, default=1)
    args = parser.parse_args()
    if args.repetitions < 1:
        parser.error("repetitions must be positive")
    root = Path.cwd()
    check_source(root, args.baseline_sha)
    base = {"root": str(root), "sha": args.baseline_sha}
    url = os.environ["NF_TEST_DATABASE_URL"]
    admin = create_engine(make_url(url).set(database="postgres"), isolation_level="AUTOCOMMIT")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if list(output.glob("*.json.gz")):
        raise ValueError("provide an empty output directory")
    try:
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as resources:
            for case in args.cases:
                while not args.candidate.exists() and case.endswith("-exact"):
                    time.sleep(2)  # Signed exact-input certificates have a finite lifetime.
                objects = Path(temporary) / case
                with isolated_kernel(url, objects) as (kernel, template_url):
                    setup = exact.prepare if case.endswith("-exact") else paired.prepare
                    fixture = setup(kernel, case.replace("overload-", "hotspot-"))
                    kernel.db.close()
                    template = paired.fresh_name()
                    paired.clone(admin, template, make_url(template_url).database)
                    resources.callback(paired.drop, admin, template)
                job = (case, template, objects, fixture, paired.digest(fixture))
                for rep in range(1, args.repetitions + 1):
                    order = args.arms if rep % 2 else list(reversed(args.arms))
                    for arm in order:
                        source = base
                        if arm != "A":
                            while not args.candidate.exists():
                                time.sleep(2)
                            source = json.loads(args.candidate.read_bytes())
                            check_source(Path(source["root"]), source["sha"])
                        enabled = source["final_env"] if arm == "F" else ENABLED[arm]
                        if set(enabled) - set(FLAGS):
                            raise ValueError("unrecognized feature switch")
                        flags = {k: str(int(k in enabled)) for k in FLAGS}
                        measure(admin, url, job, source, arm, flags, output, rep)
    finally:
        admin.dispose()


if __name__ == "__main__":
    main()
