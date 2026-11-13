"""SDK/REST read matrix using independent client, service, and PostgreSQL processes."""
import argparse
import asyncio
from contextlib import AsyncExitStack
import gzip
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import resource
import subprocess
from tempfile import TemporaryDirectory
from time import monotonic_ns, process_time

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import text

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4
from fastapi.encoders import jsonable_encoder
from novel_factory.fixtures import load_fixture
from standard_http_load import loopback_server, percentiles
from benchmarks.p2_behavioral_load import isolated_kernel
from novel_factory.application.kernel import Kernel
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.objects import ObjectStore
from novel_factory.interfaces.http import HttpSettings, create_app


def task_spec(principal, wr):
    return dict(schema_version="2.0", task_id=str(uuid4()), generation=0, role_id="writer",
                task_kind="hera_load", state="READY", world_ref=wr, story_time={"tick": 0, "phase": 0},
                context_bundle_id=None, policy_version="nf-policy-1", dependency_fingerprint=None,
                runtime_contract_version="a" * 64, capability_grant_id=principal.grant_id,
                input_artifacts=[], budget={"currency": "USD", "max_amount": "1", "token_budget": 10},
                wall_deadline=(datetime.now(UTC) + timedelta(hours=2)).isoformat())


def population(kernel, count):
    books = [load_fixture(kernel), load_fixture(kernel)]
    rows = []
    for i in range(count):
        book = books[i % 2]
        wr = book["world_ref"]
        identity = kernel.issue_grant(book["principal"], wr["work_id"], role_id="writer",
                                      branch_ids=[wr["branch_id"]])
        rows.append((identity, wr))
    return rows


def serve(pipe, url, objects, bindings, affinity):
    os.sched_setaffinity(0, affinity)
    db = Database(url, pool_size=4, max_overflow=0)
    kernel = Kernel(db, ObjectStore(Path(objects)))
    settings = HttpSettings(max_inflight_requests=1024, max_queued_operations=1024, queue_timeout_seconds=180,
                            rate_burst=2000, rate_per_second=2000)
    app = create_app(kernel, settings=settings, **({"mcp_bindings": bindings} if bindings is not None else {}))
    with loopback_server(app, lifespan="on", keepalive=300) as base:
        pipe.send(base)
        while pipe.recv() == "snapshot":
            pipe.send({"server_cpu_s": process_time(), "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                       "queues": {key: getattr(app.state, key).snapshot() for key in
                                  ("ingress", "admission", "authentication_admission")}})
    db.close()


