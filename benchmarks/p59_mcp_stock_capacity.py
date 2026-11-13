"""Follow-up real OS process/SDK session capacity trial using the stock server entrypoint.

The absolute memory ceiling of the host is shared by other jobs. Keep 1 GiB
free, sample actual resident memory, and observe what remains simultaneously
usable before stopping. On Linux only (psutil USS + host available memory).
"""
import argparse
import asyncio
from contextlib import AsyncExitStack
from pathlib import Path
import os
import subprocess
import sys
import tempfile
from time import perf_counter_ns

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import psutil
from sqlalchemy import text

from p59_mcp_stdio import Bound, MIX, WriteProbe, invoke, phase, write_file
from bound_stdio_throughput import provision
from novel_factory.application.kernel import Kernel
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.objects import ObjectStore


async def stock(stack, role):
    start = perf_counter_ns()
    env = {key: os.environ[key] for key in ("PYTHONPATH", "LD_LIBRARY_PATH") if key in os.environ}
    params = StdioServerParameters(command=sys.executable, args=[
        "-m", "novel_factory.interfaces.mcp_stdio", "--binding", str(role.path)], env=env)
    reader, writer = await stack.enter_async_context(stdio_client(params))
    probe = WriteProbe(writer)
    client = await stack.enter_async_context(ClientSession(reader, probe))
    async with asyncio.timeout(60):
        await client.initialize()
    elapsed = perf_counter_ns()-start
    children = [p for p in psutil.Process().children()
                if str(role.path) in p.cmdline()]
    assert len(children) == 1
    bound = Bound(role, client, probe, children[0].pid, None, elapsed)
    assert await invoke(bound, "tools/list") == "OK"
    return bound


async def trial(kernel, directory, output, report):
    bindings = []
    host = psutil.virtual_memory()
    report.update(host_memory_bytes=host.total, start_available_bytes=host.available,
                  reserve_bytes=1024**3)
    with kernel.db.uow(write=False) as repo:
        report["postgres"] = repo.conn.scalar(text("SELECT version()"))
        report["pg_limit"] = repo.conn.scalar(text("SHOW max_connections"))
    async with AsyncExitStack() as stack:
        for i in range(200):
            previous = [s["uss_bytes"] for s in report["steps"]]
            free = psutil.virtual_memory().available
            cost = max(previous, default=0)
            if free - cost < report["reserve_bytes"]:
                report["stop"] = {"at": i, "reason": "host_available_less_than_reserve_plus_observed_max_uss",
                                  "available_bytes": free, "next_uss_estimate_bytes": cost}
                break
            # An arm may hit a real process/session/PG startup error even when
            # there is spare memory. Preserve the observation and drain peers.
            role = provision(kernel, directory, i, lease_seconds=3600)
            nested = AsyncExitStack()
            try:
                b = await stock(nested, role)
            except Exception as exc:
                await nested.aclose()
                report["stop"] = {"at": i, "reason": "session_start_error", "type": type(exc).__name__}
                break
            stack.push_async_callback(nested.aclose)
            bindings.append(b)
            proc = psutil.Process(b.pid)
            mem = proc.memory_full_info()
            row = {"n": i+1, "pid": b.pid, "rss_bytes": mem.rss, "uss_bytes": mem.uss,
                   "pss_bytes": mem.pss, "cold_ms": b.cold_ns/1e6, "available_before": free}
            report["steps"].append(row)
            if i+1 in (2, 10, 50, 200):
                row["sweep"] = await phase(kernel.db, bindings, MIX, 4, 1, None)
                print(i+1, row["sweep"]["statuses"], flush=True)
            write_file(output, report)
        if bindings:
            report["final_sweep"] = await phase(kernel.db, bindings, MIX, 4, 1, None)
            report["final_live"] = sum(psutil.pid_exists(b.pid) for b in bindings)
            mem = [psutil.Process(b.pid).memory_full_info() for b in bindings]
            report["final_memory"] = {key: sum(getattr(m, key) for m in mem)
                                      for key in ("rss", "pss", "uss")}
            report["host_available_after"] = psutil.virtual_memory().available
            report["roles"] = len({b.role.principal_id for b in bindings})
            report["works"] = len({b.role.expected["nf_task_get"]["world_ref"]["work_id"] for b in bindings})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    db = Database(pool_size=2)
    report = {"source": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
              "server_entrypoint": "python -m novel_factory.interfaces.mcp_stdio", "steps": []}
    try:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            kernel = Kernel(db, ObjectStore(directory/"objects"))
            asyncio.run(trial(kernel, directory, args.output, report))
    except BaseException as exc:
        report["error"] = type(exc).__name__
        raise
    finally:
        write_file(args.output, report)
        db.close()


if __name__ == "__main__":
    main()
