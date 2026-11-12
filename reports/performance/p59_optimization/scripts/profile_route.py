"""Profile pure policy resolution. Run from the repository with PYTHONPATH=."""
import argparse
import cProfile
import hashlib
import io
import json
import math
import platform
import pstats
import statistics
import time
from pathlib import Path

from agent_runtime.routing import SCOPE, resolve


def workload(size):
    scope = {k: k + "-fixture" for k in SCOPE}
    rules = [{"id": "default", "when": {}, "profile": "default-profile"}]
    for i in range(size - 1):
        rules.append({"id": f"r{i}", "when": {"role_id": f"role{i}"}, "profile": f"profile{i}"})
    policy = {**scope, "revision": 3, "rules": rules}
    return policy, [{"role_id": f"role{i}"} for i in range(max(size, 1))]


def sample(size, repeats):
    policy, actors = workload(size)
    timings = []
    start = time.perf_counter_ns()
    resolve(policy, actors[0])
    first = (time.perf_counter_ns() - start) / 1000
    for i in range(24):
        resolve(policy, actors[i % len(actors)])
    cpu = time.process_time_ns()
    for i in range(repeats):
        start = time.perf_counter_ns()
        result = resolve(policy, actors[i % len(actors)])
        timings.append((time.perf_counter_ns() - start) / 1000)
        assert result["profile_id"] == (f"profile{i % len(actors)}" if i % len(actors) < size - 1 else "default-profile")
    def percentile(p):
        return sorted(timings)[math.ceil(len(timings)*p)-1]
    return {"rules": size, "calls": repeats, "cold_first_us": first, "warm_p50_us": statistics.median(timings),
            "warm_p95_us": percentile(.95), "warm_p99_us": percentile(.99),
            "calls_per_s": 1e6 / statistics.mean(timings), "cpu_ms": (time.process_time_ns()-cpu)/1e6,
            "samples_us": timings}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--output", required=True, type=Path)
    p.add_argument("--profile", required=True, type=Path)
    p.add_argument("--calls", type=int, default=512)
    options = p.parse_args()
    import agent_runtime.routing as routing
    source = Path(routing.__file__).read_bytes()
    results = {"python": platform.python_version(), "module_sha256": hashlib.sha256(source).hexdigest(),
               "timer": "perf_counter_ns", "unit": "us", "series": [sample(size, options.calls) for size in (1,16,256)]}
    options.output.write_text(json.dumps(results)+"\n")
    profile = cProfile.Profile()
    policy, actors = workload(256)
    with profile:
        for actor in actors:
            resolve(policy, actor)
    stats = io.StringIO()
    pstats.Stats(profile, stream=stats).sort_stats("cumulative").print_stats(18)
    options.profile.write_text(stats.getvalue())
