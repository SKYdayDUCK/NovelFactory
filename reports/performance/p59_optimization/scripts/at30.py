"""Replay unchanged capacity commands from the frozen CI workflows, one at a time."""
import argparse
import gzip
import json
import os
from pathlib import Path
import subprocess
import sys
import time

parser = argparse.ArgumentParser()
parser.add_argument("out", type=Path)
parser.add_argument("scenarios", nargs="+")
args = parser.parse_args()
args.out.mkdir(parents=True, exist_ok=True)
for scenario in args.scenarios:
    if scenario == "1000-open":
        module = "benchmarks.p3_open_arrival"
        opts = ["--agents", "1000", "--rounds", "5", "--arrival-rps", "1000", "--hot-fraction", ".8", "--api-processes", "2"]
    elif scenario == "500-soak":
        module = "benchmarks.p3_stability"
        opts = ["--agents", "500", "--seconds", "600", "--read-rps", "20", "--api-processes", "2"]
    else:
        agents, profile, repeat = scenario.split("-")
        module = "benchmarks.p2_behavioral_load"
        opts = ["--agents", agents, "--profile", profile, "--hot-fraction", ".8" if profile == "overload" else ".2", "--arrival-rps", "10", "--open-loop-rps", "1000", "--rounds", "5", "--max-attempts", "5", "--retry-budget-seconds", "30", "--gate-scope", "capacity", "--service-mode", "isolated-process", "--api-processes", "2" if agents == "500" else "1"]
    folder = args.out / scenario
    folder.mkdir(parents=True, exist_ok=True)
    raw = folder / "load.json"
    argv = [sys.executable, "-m", module, *opts, "--output", str(raw.resolve())]
    env = os.environ.copy()
    if module.endswith("p2_behavioral_load") and opts[1] != "500":
        env.update(NF_POOL_SIZE="8", NF_POOL_OVERFLOW="0", NF_BENCH_DIAGNOSTICS="1")
    start = time.time()
    with (folder / "console.log").open("w") as console:
        result = subprocess.run(argv, env=env, stdout=console, stderr=subprocess.STDOUT)
    (folder / "status.json").write_text(json.dumps({"exit_code": result.returncode, "argv": argv, "elapsed_seconds": time.time()-start, "started_epoch_seconds":start, "cpu_affinity": sorted(os.sched_getaffinity(0)), "has_evidence":raw.is_file()},indent=2)+"\n")
    if raw.is_file():
        with gzip.GzipFile(filename=str(raw)+".gz",mode="wb",mtime=0) as archive:
            archive.write(raw.read_bytes())
        raw.unlink()
