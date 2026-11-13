"""Pre-registered hot duplicate, cold distinct, and serial real HTTP/SDK status workloads."""
import argparse
import asyncio
import gzip
import importlib.metadata
import json
import logging
import os
import platform
import subprocess
from contextlib import AsyncExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter, process_time

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from benchmarks.hera_mixed import population, task_spec
from benchmarks.http_load import SqlMeasurements, loopback_server, percentiles
from benchmarks.p2_behavioral_load import isolated_kernel
from novel_factory.application.task_operations import TaskOperations, TaskOperationsBinding
from novel_factory.interfaces.http import HttpSettings, create_app


async def trial(kernel, rows, bindings, mode, enabled):
    settings = HttpSettings(fenced_task_status=True, coalesce_task_status=enabled, rate_burst=1000,
                            per_principal_concurrency=6)
    app = create_app(kernel, settings=settings, mcp_bindings=bindings)
    records = []
    with loopback_server(app, lifespan="on") as base:
        async with AsyncExitStack() as stack:
            lanes = []
            for i in range(1 if mode == "serial" else 6):
                identity, task_id, expected = rows[i if mode == "distinct" else 0]
                client = await stack.enter_async_context(httpx.AsyncClient(base_url=base, trust_env=False,
                    headers={"Authorization": "Bearer " + identity["token"]}))
                session = None
                if i % 2:
                    streams = await stack.enter_async_context(streamable_http_client(base + "/mcp", http_client=client))
                    session = await stack.enter_async_context(ClientSession(streams[0], streams[1]))
                    await session.initialize()
                    await session.list_tools()
                lanes.append((client, session, task_id, expected))

            async def invoke(i, lane, epoch, n):
                client, session, task_id, expected = lane
                sent = perf_counter()
                error = None
                try:
                    if session:
                        resp = await session.call_tool("nf_task_status", {"task_id": task_id})
                        ok = not resp.isError and resp.structuredContent == expected
                    else:
                        resp = await client.get("/api/v1/tasks/" + task_id)
                        ok = resp.status_code == 200 and resp.json() == expected
                except Exception as exc:  # noqa: BLE001 - preserve every offered request in the raw record
                    error, ok = type(exc).__name__, False
                end = perf_counter()
                return {"round": n, "lane": i, "transport": "mcp" if session else "http",
                        "scheduled": epoch, "sent": sent, "end": end, "ms": (end-epoch)*1000,
                        "ok": ok, "error": error}

            with SqlMeasurements(kernel.db.engine) as sql:
                start, cpu = perf_counter(), process_time()
                for n in range(6):
                    epoch = perf_counter()
                    records.extend(await asyncio.gather(*(invoke(i, lane, epoch, n) for i, lane in enumerate(lanes))))
                wall, cpu = perf_counter() - start, process_time() - cpu
    correct = sum(row["ok"] for row in records)
    return {"mode": mode, "candidate": enabled, "correct": correct, "wall_s": wall, "cpu_s": cpu,
            "rps": correct/wall, "latency_ms": percentiles([r["end"] - r["scheduled"] for r in records]), "sql": sql.result(wall),
            "joined": app.state.status_coalescer.joined if enabled else 0, "requests": records}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    logging.disable(logging.WARNING)
    with TemporaryDirectory() as tmp, isolated_kernel(os.environ["NF_TEST_DATABASE_URL"], Path(tmp)) as (kernel, _):
        rows, bindings = [], {}
        for identity, wr in population(kernel, 6):
            principal = identity["principal"]
            binding = TaskOperationsBinding(wr["work_id"], wr["branch_id"])
            bindings[principal] = binding
            spec = task_spec(principal, wr)
            kernel.create_task(principal, spec, "create")
            task_id = spec["task_id"]
            expected = TaskOperations(kernel, principal, binding=binding, fenced_status=True).status(task_id)
            rows.append((identity, task_id, expected))
        runs = []
        for mode in ("serial", "hot", "distinct"):
            for enabled in (False, True, True, False):
                runs.append(asyncio.run(trial(kernel, rows, bindings, mode, enabled)))
                run = runs[-1]
                print(mode, enabled, run["correct"], run["joined"], round(run["rps"],2), flush=True)
        args.output.parent.mkdir(exist_ok=True, parents=True)
        with gzip.open(args.output, "wt", encoding="utf8") as out:
            json.dump({"source": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
                       "environment": {"platform": platform.platform(), "python": platform.python_version(),
                        "dependencies": {k: importlib.metadata.version(k) for k in ("mcp", "httpx", "uvicorn", "psycopg")}},
                       "runs": runs}, out, separators=(",", ":"))


if __name__ == "__main__":
    main()
