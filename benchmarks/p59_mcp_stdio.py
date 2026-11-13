"""Real, independently bound SDK stdio sessions on a disposable migrated PostgreSQL DB.

Run `python benchmarks/p59_mcp_stdio.py --help`. Requires psutil for the load
driver. Server timing hooks are benchmark-only and installed equally in all arms.
"""
import argparse
import asyncio
from collections import Counter
from contextlib import AsyncExitStack
from contextvars import ContextVar
from dataclasses import dataclass
import gzip
import hashlib
from importlib.metadata import version
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
from time import perf_counter_ns, process_time
from uuid import uuid4

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.lowlevel import Server
import psutil
from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from sqlalchemy.pool import QueuePool

from bound_stdio_throughput import provision
from novel_factory.application.kernel import Kernel
from novel_factory.fixtures import fixture_definition
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.objects import ObjectStore
from novel_factory.interfaces import mcp_stdio

HERE = Path(__file__).resolve()
ROOT = HERE.parents[1]
NAMES = {"nf_task_get", "nf_context_get", "nf_query_batch", "nf_artifact_put"}
READS = ["nf_task_get", "nf_context_get"]
MIX = ["tools/list", *READS, "nf_query_batch"]
VARIANTS = {"A": [], "B": ["--compact-role-reads"], "C": ["--batch-discovery"],
            "S": ["--pool-slots", "1"], "G": ["--compact-role-reads", "--batch-discovery"],
            "H": ["--compact-role-reads", "--bench-task-batch"]}


def write_file(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as handle:
        json.dump(data, handle, separators=(",", ":"), allow_nan=False)
    tmp.replace(path)


def serve_with_metrics(target):
    """Timestamp SDK dispatch, admission, sync service, pool checkout and cursor IO."""
    current = ContextVar("current_request", default=None)
    collectors = []
    if "--bench-task-batch" in sys.argv:
        from p59_role_read_group import install

        sys.argv.remove("--bench-task-batch")
        collectors = install()
    original_handle = Server._handle_request
    original_run = mcp_stdio.BoundToolAdmission.run
    original_connect = QueuePool.connect
    rows = []
    Path(str(target) + ".pid").write_text(str(os.getpid()))

    async def handle(server, responder, request, *args, **kwargs):
        row = {"id": responder.request_id, "method": request.method,
               "receive": perf_counter_ns(), "sql": 0, "sql_ns": 0,
               "checkouts": 0, "checkout_ns": 0, "workers": []}
        token = current.set(row)
        try:
            return await original_handle(server, responder, request, *args, **kwargs)
        finally:
            row["reply"] = perf_counter_ns()
            rows.append(row)
            current.reset(token)

    async def run(admission, method, *args, **kwargs):
        row = current.get()
        if row is None:
            return await original_run(admission, method, *args, **kwargs)

        def work(*args, **kwargs):
            start = perf_counter_ns()
            try:
                return method(*args, **kwargs)
            finally:
                row["workers"].append([start, perf_counter_ns()])

        return await original_run(admission, work, *args, **kwargs)

    def connect(pool):
        row = current.get()
        start = perf_counter_ns()
        try:
            return original_connect(pool)
        finally:
            if row is not None:
                row["checkouts"] += 1
                row["checkout_ns"] += perf_counter_ns() - start

    @event.listens_for(Engine, "before_cursor_execute")
    def before(conn, cursor, sql, params, context, many):
        row = current.get()
        if row is not None:
            row["sql"] += 1
            context._nf_started_ns = perf_counter_ns()

    @event.listens_for(Engine, "after_cursor_execute")
    def after(conn, cursor, sql, params, context, many):
        row = current.get()
        if row is not None:
            row["sql_ns"] += perf_counter_ns() - context._nf_started_ns

    Server._handle_request = handle
    mcp_stdio.BoundToolAdmission.run = run
    QueuePool.connect = connect
    try:
        mcp_stdio.main()
    finally:
        write_file(target, rows)
        if collectors:
            Path(str(target)+".groups").write_text(json.dumps(dict(collectors[0].shapes)))


class WriteProbe:
    """Capture JSON-RPC ids and SDK stream handoff without altering the payload."""
    def __init__(self, stream):
        self.stream = stream
        self.active = ContextVar("current_client_request", default=None)

    async def send(self, msg):
        row = self.active.get()
        if row is not None:
            row["wire_id"] = msg.message.root.id
            row["send"] = perf_counter_ns()
        return await self.stream.send(msg)

    async def __aenter__(self):
        await self.stream.__aenter__()
        return self

    async def __aexit__(self, *args):
        return await self.stream.__aexit__(*args)

    def __getattr__(self, name):
        return getattr(self.stream, name)


@dataclass
class Bound:
    role: object
    session: ClientSession
    writer: WriteProbe
    pid: int
    trace: Path
    cold_ns: int
    arm_open_time: object = None


async def invoke(bound, tool):
    if tool == "tools/list":
        result = await bound.session.list_tools()
        names = [item.name for item in result.tools]
        if not names:
            return "EMPTY_DISCOVERY"
        return "OK" if len(names) == len(NAMES) and set(names) == NAMES else "WRONG_CONTENT"
    subject = fixture_definition("r1_sword")["ids"]["sword"] if tool == "nf_query_batch" else None
    args = {"lookups": [{"subject_id": subject, "predicate": "owner"}]} if subject else {}
    result = await bound.session.call_tool(tool, args)
    if result.isError:
        return (result.structuredContent or {}).get("error", {}).get("code", "MCP_ERROR")
    value = result.structuredContent
    if tool != "nf_query_batch":
        return "OK" if value == bound.role.expected[tool] else "WRONG_CONTENT"
    items = value.get("items", [])
    if not value.get("all_items_succeeded") or len(items) != 1:
        return "WRONG_CONTENT"
    state = items[0]["payload"]["state_items"]
    if len(state) != 1 or state[0]["subject_id"] != subject or state[0]["predicate"] != "owner":
        return "WRONG_CONTENT"
    answer = state[0]["answer"]
    valid = (answer["world_ref"] == bound.role.expected["nf_task_get"]["world_ref"]
             and answer["plane"] == "BELIEF" and answer["knowledge_status"] == "UNKNOWN"
             and answer["data"] is None and answer["evidence_refs"] == [])
    return "OK" if valid else "WRONG_CONTENT"


async def open_binding(stack, role, directory, index, arm):
    trace = directory / f"server-{index}-{uuid4().hex}.json.gz"
    # The SDK deliberately sanitizes the subprocess environment. Pass only the
    # import/loader paths needed by an isolated, non-installed checkout.
    env = {key: os.environ[key] for key in ("PYTHONPATH", "LD_LIBRARY_PATH") if key in os.environ}
    parameters = StdioServerParameters(command=sys.executable, env=env, args=[
        str(HERE), "server", str(trace), "--binding", str(role.path), *VARIANTS[arm]])
    start = perf_counter_ns()
    reader, writer = await stack.enter_async_context(stdio_client(parameters))
    probe = WriteProbe(writer)
    session = await stack.enter_async_context(ClientSession(reader, probe))
    async with asyncio.timeout(60):
        await session.initialize()
    pid = int(Path(str(trace) + ".pid").read_text())
    bound = Bound(role, session, probe, pid, trace, perf_counter_ns() - start)
    for tool in ["tools/list", *READS]:
        status = await invoke(bound, tool)
        if status != "OK":
            raise RuntimeError(f"warmup {tool}: {status}")
    return bound


def quantiles(values):
    ordered = sorted(values)
    return {f"p{p}": round(ordered[math.ceil(len(ordered)*p/100)-1], 4) for p in (50, 95, 99)} if values else {}


class Observer:
    def __init__(self, db, bindings):
        self.db = db
        self.processes = [psutil.Process(b.pid) for b in bindings]
        self.names = [b.role.application_name for b in bindings]
        self.samples = []

    def sample(self):
        timestamp = perf_counter_ns()
        rss = threads = 0
        cpu = 0
        for process in self.processes:
            with process.oneshot():
                rss += process.memory_info().rss
                threads += process.num_threads()
                times = process.cpu_times()
                cpu += times.user + times.system
        with self.db.uow(write=False) as repo:
            pg, busy, locks = repo.conn.execute(text("""
                SELECT count(*), count(*) FILTER (WHERE state='active'),
                       count(*) FILTER (WHERE wait_event_type='Lock') FROM pg_stat_activity
                WHERE application_name = ANY(:names)
            """), {"names": self.names}).one()
        self.samples.append({"t": timestamp, "rss": rss, "threads": threads, "server_cpu_s": cpu,
                             "pg": pg, "pg_active": busy, "pg_waiting_lock": locks})

    async def observe(self, end):
        while not end.is_set():
            await asyncio.to_thread(self.sample)
            try:
                await asyncio.wait_for(end.wait(), .25)
            except TimeoutError:
                pass


async def phase(db, bindings, names, count, concurrency, rate):
    rows = []
    pending = [set() for _ in bindings]
    start = perf_counter_ns()
    client_cpu = process_time()
    observer = Observer(db, bindings)
    end_sampling = asyncio.Event()
    sampling = asyncio.create_task(observer.observe(end_sampling))

    async def call(i, j, due):
        b = bindings[i]
        row = {"binding": i, "seq": j, "tool": names[j % len(names)], "due": due,
               "call": perf_counter_ns()}
        token = b.writer.active.set(row)
        try:
            async with asyncio.timeout(15):
                row["status"] = await invoke(b, row["tool"])
        except Exception as exc:
            row["status"] = type(exc).__name__
        finally:
            row["end"] = perf_counter_ns()
            b.writer.active.reset(token)
            rows.append(row)

    async def worker(i, lane):
        for j in range(lane, count, concurrency):
            await call(i, j, perf_counter_ns())

    try:
        if rate is None:
            await asyncio.gather(*(worker(i, lane) for i in range(len(bindings))
                                   for lane in range(concurrency)))
        else:
            for k in range(count*len(bindings)):
                i, j = k % len(bindings), k // len(bindings)
                due = start + round(k * 1e9 / rate)
                await asyncio.sleep(max(0, (due-perf_counter_ns())/1e9))
                if len(pending[i]) >= 4:
                    rows.append({"binding": i, "seq": j, "tool": names[j % len(names)],
                                 "due": due, "status": "CLIENT_DROP"})
                    continue
                future = asyncio.create_task(call(i, j, due))
                pending[i].add(future)
                future.add_done_callback(pending[i].discard)
            await asyncio.gather(*(f for active in pending for f in active))
        finish = perf_counter_ns()
    finally:
        end_sampling.set()
        await sampling
    counts = Counter(r["status"] for r in rows)
    elapsed = (finish-start)/1e9
    rep = [r for r in rows if "end" in r]
    return {"start": start, "finish": finish, "rate": rate, "count": count, "lanes": concurrency,
            "planned": len(rows), "elapsed_s": elapsed, "client_cpu_s": process_time()-client_cpu,
            "statuses": dict(counts), "ok_rps": counts["OK"]/elapsed,
            "sent_rps": sum("send" in r for r in rows)/elapsed,
            "all_reply_ms": quantiles([(r["end"]-r["due"])/1e6 for r in rep]),
            "ok_reply_ms": quantiles([(r["end"]-r["due"])/1e6 for r in rep if r["status"] == "OK"]),
            "send_to_reply_ms": quantiles([(r["end"]-r["send"])/1e6 for r in rep if "send" in r]),
            "per_binding_ok": [sum(r["binding"] == i and r["status"] == "OK" for r in rows)
                               for i in range(len(bindings))],
            "resources": observer.samples, "rows": rows}


def attach_traces(bindings, phases):
    records = []
    for b in bindings:
        with gzip.open(b.trace, "rt") as f:
            records.append({row["id"]: row for row in json.load(f)})
    for phase_data in phases:
        matched = []
        for row in phase_data["rows"]:
            trace = records[row["binding"]].get(row.get("wire_id"))
            if trace is not None:
                row["server"] = trace
                matched.append(trace)
        elapsed = phase_data["elapsed_s"]
        phase_data.update(server_dispatched=len(matched), server_dispatch_rps=len(matched)/elapsed,
                          service_work_items=sum(len(r["workers"]) for r in matched),
                          sql=sum(r["sql"] for r in matched),
                          sql_wall_ms=sum(r["sql_ns"] for r in matched)/1e6,
                          checkout_wall_ms=sum(r["checkout_ns"] for r in matched)/1e6)
        # An admitted work item may serve multiple request ids in the discovery candidate.
        if phase_data["rate"] is None:
            phase_data["closed_client_outstanding_limit"] = phase_data["lanes"] * len(bindings)


def check_audits(db, bindings, phases):
    # SDK restarts recycle wire ids. Scope by the database high-water time
    # (arm_open_time) to exclude all earlier server processes' audit records.
    for phase_data in phases:
        checked = 0
        for index, b in enumerate(bindings):
            expected = {sha256(str(row["wire_id"]).encode()).hexdigest() for row in phase_data["rows"]
                        if row["binding"] == index and row["status"] == "OK"}
            with db.uow(write=False) as repo:
                found = repo.conn.execute(text("""
                    SELECT a.request_id, count(*) FROM nf_audit_events a
                    JOIN nf_audit_events f ON f.parent_span_id=a.event_id AND f.causation_id=a.event_id
                    WHERE a.event_type='mcp.started' AND f.event_type='mcp.finished'
                      AND a.task_id=:task AND a.tool_name='nf_task_get' AND f.outcome='OK'
                      AND a.request_id=ANY(:expected) AND a.event_time >= :opened
                    GROUP BY a.request_id
                """), {"task": b.role.expected["nf_task_get"]["task_id"], "expected": list(expected),
                       "opened": b.arm_open_time}).all()
            assert {k: n for k, n in found} == {k: 1 for k in expected}
            checked += len(found)
        phase_data["correct_audit_pairs"] = checked


async def matrix(db, kernel, directory, args, report):
    roles = [provision(kernel, directory, i) for i in range(args.bindings)]
    assert len({r.principal_id for r in roles}) == args.bindings
    assert len({r.expected["nf_task_get"]["world_ref"]["work_id"] for r in roles}) == args.bindings
    for arm in args.arms:
        bindings = []
        with db.uow(write=False) as repo:
            arm_open = repo.conn.scalar(text("SELECT clock_timestamp()"))
        async with AsyncExitStack() as stack:
            for i, role in enumerate(roles):
                b = await open_binding(stack, role, directory, i, arm)
                b.arm_open_time = arm_open
                bindings.append(b)
            phases = [await phase(db, bindings, args.names, args.count, args.lanes, rate)
                      for rate in (None, args.rps)]
            if args.suite == "task-batch":
                check_audits(db, bindings, phases)
        attach_traces(bindings, phases)
        with db.uow(write=False) as repo:
            ref = [r.expected["nf_task_get"]["world_ref"] for r in roles]
            unchanged = all(kernel.head(repo, w["work_id"], w["branch_id"]) == w for w in ref)
            attempts = repo.conn.scalar(text(
                "SELECT count(*) FROM nf_call_attempts WHERE task_id = ANY(CAST(:ids AS uuid[]))"),
                {"ids": [r.expected["nf_task_get"]["task_id"] for r in roles]})
        if not unchanged or attempts:
            raise RuntimeError("unexpected durable effect in read-only workload")
        report["trials"].append({"arm": arm, "pids": [b.pid for b in bindings],
            "group_sizes_including_warmup": [json.loads(Path(str(b.trace)+".groups").read_text())
                                               for b in bindings] if arm == "H" else None,
            "cold_ms": [round(b.cold_ns/1e6, 2) for b in bindings], "sessions": len(bindings),
            "expected_heads_unchanged": unchanged, "provider_attempts": attempts, "phases": phases})
        write_file(args.output, report)
        print(arm, [(round(p["ok_rps"], 2), p["statuses"]) for p in phases], flush=True)


async def staircase(db, kernel, directory, args, report):
    bindings = []
    out = report["steps"] = []
    with db.uow(write=False) as repo:
        slots = int(repo.conn.scalar(text("SHOW max_connections")))
    async with AsyncExitStack() as stack:
        for i in range(200):
            memory = [psutil.Process(b.pid).memory_info().rss for b in bindings]
            estimate = max(memory, default=0)
            free = psutil.virtual_memory().available
            if sum(memory) + estimate > args.rss_budget * 1024**2 or free-estimate < 2**30 or i+1 > slots-4:
                report["stop"] = {"live": i, "rss_bytes": sum(memory), "next_estimated_bytes": estimate,
                                  "available_bytes": free, "pg_max_connections": slots}
                break
            role = provision(kernel, directory, i)
            b = await open_binding(stack, role, directory, i, "A")
            bindings.append(b)
            row = {"live": i+1, "pid": b.pid, "cold_ms": b.cold_ns/1e6,
                   "rss_bytes": psutil.Process(b.pid).memory_info().rss}
            out.append(row)
            if i+1 in (2, 10, 50, 200):
                row["sweep"] = await phase(db, bindings, MIX, 4, 1, None)
                print("stage", i+1, row["sweep"]["statuses"], flush=True)
            write_file(args.output, report)
        if bindings:
            report["final_live_sweep"] = await phase(db, bindings, MIX, 4, 1, None)
            report["final_live_pids"] = [b.pid for b in bindings if psutil.pid_exists(b.pid)]
    attach_traces(bindings, [r["sweep"] for r in out if "sweep" in r] +
                  ([report["final_live_sweep"]] if bindings else []))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--suite", choices=["reads", "discovery", "mixed", "connections", "task-batch"], default="reads")
    parser.add_argument("--arms", default="ABBA")
    parser.add_argument("--bindings", type=int, default=2)
    parser.add_argument("--rss-budget", type=int, default=4096, help="Maximum server RSS, MiB, for connection sweep")
    args = parser.parse_args()
    if not args.arms or any(a not in VARIANTS for a in args.arms) or not 1 <= args.bindings <= 200 or args.rss_budget < 1:
        parser.error("invalid arm sequence, binding count, or memory budget")
    args.names, args.count, args.lanes, args.rps = {
        "reads": (READS, 24, 2, 12), "discovery": (["tools/list"], 40, 4, 60),
        "task-batch": (["nf_task_get"], 40, 2, 12), "mixed": (MIX, 16, 2, 12), "connections": (MIX, 4, 1, 0)}[args.suite]
    db = Database(pool_size=2)
    with db.uow(write=False) as repo:
        postgres = repo.conn.scalar(text("SELECT version()"))
    report = {"suite": args.suite, "arms": args.arms, "trials": [], "python": sys.version,
              "sdk": version("mcp"), "postgres": postgres, "platform": platform.platform(),
              "affinity": psutil.Process().cpu_affinity() if hasattr(psutil.Process, "cpu_affinity") else None,
              "source": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
              "tracked_product_diff_sha256": hashlib.sha256(subprocess.check_output(
                  ["git", "diff", "HEAD", "--", "src"], cwd=ROOT)).hexdigest(),
              "product_tree_sha256": hashlib.sha256(b"".join(
                  str(p.relative_to(ROOT)).encode()+b"\0"+hashlib.sha256(p.read_bytes()).digest()
                  for p in sorted((ROOT/"src").rglob("*.py")))).hexdigest(),
              "driver_sha256": hashlib.sha256(HERE.read_bytes()).hexdigest(),
              "protocol_blob": "cb40bf58239efc4286c1f16fa10612199543b0ab",
              "limits": {n: (Path("/sys/fs/cgroup")/n).read_text().strip()
                         if (Path("/sys/fs/cgroup")/n).exists() else None
                         for n in ("cpu.max", "memory.max", "pids.max")}}
    try:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            kernel = Kernel(db, ObjectStore(directory / "objects"))
            runner = staircase if args.suite == "connections" else matrix
            asyncio.run(runner(db, kernel, directory, args, report))
    except BaseException as exc:
        report["interrupted_by"] = type(exc).__name__
        raise
    finally:
        write_file(args.output, report)
        db.close()


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "server":
        target = Path(sys.argv[2])
        del sys.argv[1:3]
        serve_with_metrics(target)
    else:
        main()
