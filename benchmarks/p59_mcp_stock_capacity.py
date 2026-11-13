"""Follow-up real OS process/SDK session capacity trial using the stock server entrypoint.

The absolute memory ceiling of the host is shared by other jobs. Keep 1 GiB
free, sample actual resident memory, and observe what remains simultaneously
usable before stopping. On Linux only (psutil USS + host available memory).
"""
import argparse
import asyncio
from collections import Counter
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


async def stock(stack, role, server_args=()):
    start = perf_counter_ns()
    env = {key: os.environ[key] for key in ("PYTHONPATH", "LD_LIBRARY_PATH") if key in os.environ}
    params = StdioServerParameters(command=sys.executable, args=[
        "-m", "novel_factory.interfaces.mcp_stdio", "--binding", str(role.path), *server_args], env=env)
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


async def bounded_sweep(db, bindings, width):
    """Keep all sessions open; each small wave finishes before the next starts."""
    waves = []
    counts = Counter()
    start = perf_counter_ns()
    for offset in range(0, len(bindings), width):
        result = await phase(db, bindings[offset:offset+width], MIX, 4, 1, None)
        result["binding_offset"] = offset
        waves.append(result)
        counts.update(result["statuses"])
        # Give recently used bindings time to retire their pool before opening
        # connections from the next wave. This delay is part of end-to-end cost.
        if offset + width < len(bindings):
            await asyncio.sleep(2.25)
    return {"width": width, "open_sessions": len(bindings), "elapsed_s": (perf_counter_ns()-start)/1e9,
            "statuses": dict(counts), "waves": waves}


async def cold_burst(bindings):
    """One common due instant, one real tool call per retained session, no retry."""
    ready = asyncio.Event()
    rows = [{} for _ in bindings]
    due = 0

    async def send(i, b):
        row = rows[i]
        token = b.writer.active.set(row)
        try:
            await ready.wait()
            row.update(binding=i, pid=b.pid, due=due, call=perf_counter_ns())
            async with asyncio.timeout(15):
                row["status"] = await invoke(b, "nf_task_get")
        except Exception as exc:
            row["status"] = type(exc).__name__
        finally:
            row["end"] = perf_counter_ns()
            b.writer.active.reset(token)

    attempts = [asyncio.create_task(send(i, b)) for i, b in enumerate(bindings)]
    await asyncio.sleep(0)
    due = perf_counter_ns()
    ready.set()
    await asyncio.gather(*attempts)
    ends = [r["end"] for r in rows]
    sends = [r["send"] for r in rows if "send" in r]
    return {"tool": "nf_task_get", "planned": len(rows), "actually_sent": len(sends),
            "sdk_send_spread_ms": (max(sends)-min(sends))/1e6 if sends else None,
            "elapsed_s": (max(ends)-due)/1e9, "statuses": dict(Counter(r["status"] for r in rows)),
            "rows": rows}


async def observed_idle(db, bindings):
    names = [b.role.application_name for b in bindings]
    deadline = asyncio.get_running_loop().time()+8
    while True:
        with db.uow(write=False) as repo:
            count = repo.conn.scalar(text(
                "SELECT count(*) FROM pg_stat_activity WHERE application_name=ANY(:names)"), {"names": names})
        if not count or asyncio.get_running_loop().time() >= deadline:
            return count
        await asyncio.sleep(.15)


async def trial(kernel, directory, output, report, *, idle_arm=False, maximum=200):
    bindings = []
    host = psutil.virtual_memory()
    report.update(host_memory_bytes=host.total, start_available_bytes=host.available,
                  reserve_bytes=1024**3)
    with kernel.db.uow(write=False) as repo:
        report["postgres"] = repo.conn.scalar(text("SELECT version()"))
        report["pg_limit"] = repo.conn.scalar(text("SHOW max_connections"))
    async with AsyncExitStack() as stack:
        for i in range(maximum):
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
                b = await stock(nested, role, report["server_args"])
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
                row["sweep"] = (await bounded_sweep(kernel.db, bindings, 8) if idle_arm
                                else await phase(kernel.db, bindings, MIX, 4, 1, None))
                print(i+1, row["sweep"]["statuses"], flush=True)
            write_file(output, report)
        if bindings:
            if idle_arm:
                report["final_live_pids_before"] = [b.pid for b in bindings if psutil.pid_exists(b.pid)]
                report["final_sweep"] = await bounded_sweep(kernel.db, bindings, 8)
                report["cold_bound_pg_sessions_before_burst"] = await observed_idle(kernel.db, bindings)
                write_file(output, report)
                report["simultaneous_burst"] = await cold_burst(bindings)
                report["final_live_pids_after"] = [b.pid for b in bindings if psutil.pid_exists(b.pid)]
            else:
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
    parser.add_argument("--idle-arm", action="store_true",
                        help="Stock 2s idle / one slot; bounded waves then one cold simultaneous request each")
    parser.add_argument("--maximum", type=int, default=200, choices=range(1, 201), metavar="1..200")
    args = parser.parse_args()
    db = Database(pool_size=2)
    report = {"source": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
              "server_entrypoint": "python -m novel_factory.interfaces.mcp_stdio",
              "server_args": ["--idle-db-seconds", "2", "--pool-slots", "1"] if args.idle_arm else [],
              "target": args.maximum, "steps": []}
    try:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            kernel = Kernel(db, ObjectStore(directory/"objects"))
            asyncio.run(trial(kernel, directory, args.output, report,
                              idle_arm=args.idle_arm, maximum=args.maximum))
    except BaseException as exc:
        report["error"] = type(exc).__name__
        raise
    finally:
        write_file(args.output, report)
        db.close()


if __name__ == "__main__":
    main()
