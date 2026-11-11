"""Attribute admission outcomes and inclusive timings to public workload operations.

Run with the same arguments as round2_capacity.py. Applied only inside the
disposable benchmark services, identically in both arms. No principal, request
payload, private path or credentials enter the evidence.
"""
from collections import Counter, defaultdict
from contextlib import asynccontextmanager
from contextvars import ContextVar
import json
from multiprocessing.util import Finalize
import os
from pathlib import Path
import time
from unittest.mock import patch

import round2_capacity as capacity
import scion_paired as paired


ROUTES = {
    "/api/v1/queries/timeline": "query", "/api/v1/queries/batch": "query",
    "/api/v1/contexts/compile": "context", "/api/v1/proposals": "proposal",
    "/api/v1/world/acceptance-certificates": "certificate",
    "/api/v1/world/accept-guarded": "accept", "/api/v1/world/accept": "accept",
}


def install_stage_meter():
    from novel_factory.domain.errors import DomainError
    from novel_factory.interfaces.admission import AdmissionController, TokenBucketLimiter
    from novel_factory.interfaces.http import RequestBoundaryMiddleware

    active_request = ContextVar("benchmark_admission_route", default=None)
    counters = defaultdict(Counter)
    boundary = RequestBoundaryMiddleware.__call__
    slot = AdmissionController.slot
    limit = TokenBucketLimiter.check

    async def identify(middleware, scope, receive, send):
        if scope["type"] != "http":
            return await boundary(middleware, scope, receive, send)
        token = active_request.set((ROUTES.get(scope["path"], "other"), middleware.ingress))
        try:
            return await boundary(middleware, scope, receive, send)
        finally:
            active_request.reset(token)

    @asynccontextmanager
    async def measure(controller, key, kind="default"):
        request = active_request.get()
        if request is None:
            async with slot(controller, key, kind):
                yield
            return
        route, ingress = request
        stage = "ingress" if controller is ingress else "auth" if kind == "default" else "operation"
        counts = counters[route + "." + stage]
        start = time.monotonic_ns()
        counts["attempts"] += 1
        counts["max_waiters_seen_on_entry"] = max(counts["max_waiters_seen_on_entry"], controller._waiting)
        acquired = False
        try:
            async with slot(controller, key, kind):
                acquired = True
                admitted = time.monotonic_ns()
                counts["admitted"] += 1
                counts["admitted_wait_ns"] += admitted - start
                try:
                    yield
                finally:
                    counts["slot_held_ns"] += time.monotonic_ns() - admitted
        except DomainError as error:
            if not acquired:
                counts["rejected_wait_ns"] += time.monotonic_ns() - start
                outcome = {"Request capacity is full": "queue_full", "Request queue wait expired": "wait_expired"}
                counts[outcome.get(error.safe_message, error.code)] += 1
            raise

    async def track_rate(limiter, key):
        try:
            return await limit(limiter, key)
        except DomainError as error:
            request = active_request.get()
            if request:
                counters[request[0] + ".rate_limit"][error.code] += 1
            raise

    def persist():
        path = Path(os.environ["NF_SCION_METER_DIR"]) / f"{os.getpid()}.json"
        if path.exists():
            data = json.loads(path.read_bytes())
            data["admission_by_route"] = dict(counters)
            path.write_text(json.dumps(data))

    RequestBoundaryMiddleware.__call__ = identify
    AdmissionController.slot = measure
    TokenBucketLimiter.check = track_rate
    Finalize(None, persist, exitpriority=-2)


if __name__ == "__mp_main__" and os.environ.get("NF_SCION_METER_DIR"):
    if os.environ.get("NF_BENCH_WORKER_SCHEMA_LINKS") == "1":
        os.environ["NF_SCION_SCHEMA_LINKS"] = "1"
    paired.install_worker_meter()
    capacity.install_heartbeat()
    install_stage_meter()

if __name__ == "__main__":
    with patch.object(capacity, "__file__", __file__):
        capacity.main()
