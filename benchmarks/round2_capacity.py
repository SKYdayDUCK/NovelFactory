"""Run the frozen CI workloads on paired PG17 fixtures and measure ASGI loop delay.

Both application arms use this same external harness. No request or credential
is recorded by the heartbeat, and a stalled loop does not enqueue catch-up ticks.
"""
import asyncio
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

import scion_paired as paired


def install_heartbeat():
    from multiprocessing.util import Finalize
    from novel_factory.interfaces.http import RequestBoundaryMiddleware

    delays = []
    original = RequestBoundaryMiddleware.__call__
    started = False

    async def observed(middleware, scope, receive, send):
        nonlocal started
        if not started and scope["type"] == "http":
            started = True
            loop = asyncio.get_running_loop()
            interval = .05

            def tick(deadline):
                now = loop.time()
                delays.append(max(0., now - deadline))
                loop.call_at(now + interval, tick, now + interval)

            due = loop.time() + interval
            loop.call_at(due, tick, due)
        return await original(middleware, scope, receive, send)

    def save():
        # Existing per-process meter writes its JSON in the same directory.
        # Add metrics to the same file after its normal finalizer has run.
        meter = Path(os.environ["NF_SCION_METER_DIR"]) / f"{os.getpid()}.json"
        if not meter.is_file():
            return
        data = json.loads(meter.read_bytes())
        from benchmarks.http_load import percentiles
        data["event_loop_delay_ms"] = percentiles(delays)
        data["event_loop_samples"] = len(delays)
        data["event_loop_period_ms"] = 50
        from novel_factory.contracts import registry
        data["service_schema_links_active"] = getattr(registry, "_STATIC_SCHEMA_LINKS", False)
        meter.write_text(json.dumps(data))

    RequestBoundaryMiddleware.__call__ = observed
    Finalize(None, save, exitpriority=-1)


def main():
    if sys.argv[1:] != ["--child"]:
        with patch.object(paired, "__file__", __file__):
            paired.main()
        return
    from benchmarks import p2_behavioral_load as load
    original = load.run_benchmark

    def run(*args, **kwargs):
        if kwargs["profile"] == "hotspot":
            # The original CI case adds all five open burst rounds to mixed work.
            assert kwargs["hot_fraction"] == .8 and kwargs["rounds"] == 5
            kwargs["profile"] = "overload"
        return original(*args, **kwargs)

    with patch.object(load, "run_benchmark", run):
        paired.child()


if __name__ == "__mp_main__" and os.environ.get("NF_SCION_METER_DIR"):
    if os.environ.get("NF_BENCH_WORKER_SCHEMA_LINKS") == "1":
        os.environ["NF_SCION_SCHEMA_LINKS"] = "1"
    paired.install_worker_meter()
    install_heartbeat()

if __name__ == "__main__":
    main()
