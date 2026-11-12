"""Exercise two independently authorized MCP stdio processes on a disposable migrated PG17."""
import argparse
import asyncio
from collections import Counter
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import inspect
import json
import math
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
from time import perf_counter

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from sqlalchemy import text

from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.kernel import Kernel
from novel_factory.application.runtime_tasks import RuntimeTaskService
from novel_factory.fixtures import load_fixture, point
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.model_gateway import OpenAICompatibleAdapter
from novel_factory.infrastructure.objects import ObjectStore

TOOLS = ("nf_task_get", "nf_context_get")


@dataclass(frozen=True)
class Role:
    path: Path
    expected: dict
    application_name: str
    principal_id: str


def provision(kernel, directory, index):
    world = load_fixture(kernel)
    adapter = OpenAICompatibleAdapter(endpoint="https://example.test/v1/chat/completions", model="fixture", api_key=None)
    role = RuntimeTaskService(kernel, AgentGateway(kernel, {"fixture": adapter})).prepare(
        world["principal"], world["world_ref"], role_id="character_actor", task_kind="character_action",
        actor_id=world["ids"]["protagonist"], story_time=point(1), subject_ids=[world["ids"]["sword"]],
        predicates=["owner"], assignment=f"Role {index} proposes an action.", adapter_id="fixture")
    with kernel.db.uow(write=False) as repo:
        task = repo.one("tasks", {"task_id": role.task["task_id"]})
        context = repo.one("contexts", {"context_bundle_id": role.context_bundle_id})["payload"]
    expected_task = {key: task[key] for key in ("task_id", "generation", "state", "context_bundle_id")}
    expected_task["world_ref"] = world["world_ref"]
    assert context["task_id"] == task["task_id"] and context["actor_id"] == world["ids"]["protagonist"]
    app_name = "nf_stdio_load_" + task["task_id"]
    url = kernel.db.engine.url.update_query_dict({"application_name": app_name})
    binding = {key: task[key] for key in ("task_id", "generation")}
    path = directory / f"binding-{index}.json"
    if os.name == "nt":
        from novel_factory.infrastructure.private_files import create_private_file
        fd = create_private_file(path)
    else:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        json.dump({"database_url": url.render_as_string(hide_password=False), "token": role.private_token,
                   "object_root": str(kernel.objects.root), "binding": {**binding, "worker_id": role.worker_id}}, out)
    return Role(path, {"nf_task_get": expected_task, "nf_context_get": context}, app_name, role.principal.principal_id)


async def phase(sessions, oracles, *, samples_per_role, offered_rps=None):
    rows = []
    inflight = [set(), set()]
    start_utc = datetime.now(UTC).isoformat()
    start = perf_counter()

    async def read(role, sequence, due):
        tool = TOOLS[sequence % len(TOOLS)]
        sent = perf_counter()
        try:
            async with asyncio.timeout(15):
                reply = await sessions[role].call_tool(tool, {})
            if reply.isError:
                result = (reply.structuredContent or {}).get("error", {}).get("code", "MCP_ERROR")
            else:
                result = "OK" if reply.structuredContent == oracles[role][tool] else "WRONG_CONTENT"
        except Exception as error:
            result = type(error).__name__
        end = perf_counter()
        rows.append({"role": role, "sequence": sequence, "tool": tool, "status": result,
                     "due_ms": round(1000 * (due - start), 3), "sent_ms": round(1000 * (sent - start), 3),
                     "call_ms": round(1000 * (end - sent), 3), "arrival_to_reply_ms": round(1000 * (end - due), 3)})

    if offered_rps is None:
        async def worker(role, offset):
            for sequence in range(offset, samples_per_role, 2):
                await read(role, sequence, perf_counter())

        await asyncio.gather(*(worker(role, offset) for role in range(2) for offset in range(2)))
    else:
        for index in range(2 * samples_per_role):
            role, sequence = index % 2, index // 2
            due = start + index / offered_rps
            await asyncio.sleep(max(0, due - perf_counter()))
            if len(inflight[role]) == 4:
                rows.append({"role": role, "sequence": sequence, "tool": TOOLS[sequence % 2],
                             "status": "CLIENT_DROP", "due_ms": round(1000 * (due - start), 3)})
                continue
            pending = asyncio.create_task(read(role, sequence, due))
            inflight[role].add(pending)
            pending.add_done_callback(inflight[role].discard)
        await asyncio.gather(*[pending for active in inflight for pending in active])
    elapsed = perf_counter() - start
    counts = Counter(row["status"] for row in rows)
    latencies = sorted(row["arrival_to_reply_ms"] for row in rows if "arrival_to_reply_ms" in row)
    percentiles = {f"p{p}": latencies[math.ceil(len(latencies) * p / 100) - 1]
                   for p in (50, 95, 99)} if latencies else {}
    return {"mode": "closed" if offered_rps is None else "open", "offered_rps": offered_rps,
            "started_utc": start_utc, "elapsed_s": elapsed, "offered": 2 * samples_per_role,
            "statuses": dict(counts), "verified_rps": counts["OK"] / elapsed,
            "all_replies_arrival_to_reply_ms": percentiles,
            "samples": sorted(rows, key=lambda r: (r["role"], r["sequence"]))}


async def exercise(kernel, roles, samples_per_role, offered_rps):
    sessions = []
    oracles = [role.expected for role in roles]
    assert len({role.principal_id for role in roles}) == len({o["nf_task_get"]["task_id"] for o in oracles}) == 2
    assert len({o["nf_task_get"]["world_ref"]["work_id"] for o in oracles}) == 2
    async with AsyncExitStack() as stack:
        for role in roles:
            server = StdioServerParameters(command=sys.executable, args=[
                "-m", "novel_factory.interfaces.mcp_stdio", "--binding", str(role.path)])
            streams = await stack.enter_async_context(stdio_client(server))
            session = await stack.enter_async_context(ClientSession(*streams))
            await session.initialize()
            names = {tool.name for tool in (await session.list_tools()).tools}
            assert set(TOOLS) <= names
            for tool in TOOLS:
                result = await session.call_tool(tool, {})
                assert not result.isError and result.structuredContent == role.expected[tool]
            sessions.append(session)

        results = []
        for rate in (None, offered_rps):
            result = await phase(sessions, oracles, samples_per_role=samples_per_role, offered_rps=rate)
            with kernel.db.uow(write=False) as repo:
                session_count = text("SELECT count(*) FROM pg_stat_activity WHERE application_name=:name")
                result["binding_sessions_at_finish"] = [
                    repo.conn.scalar(session_count, {"name": role.application_name}) for role in roles]
            results.append(result)
        return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--samples-per-role", type=int, default=120)
    parser.add_argument("--offered-rps", type=float, default=12)
    args = parser.parse_args()
    if args.samples_per_role < 2 or not math.isfinite(args.offered_rps) or args.offered_rps <= 0:
        parser.error("Set at least two samples per role and a positive finite offered rate")
    # The fixed driver can be called by a different revision's editable installation.
    runtime_source = Path(inspect.getfile(Kernel)).resolve().parent
    source = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=runtime_source, text=True).strip()
    db = Database(pool_size=1)
    try:
        with tempfile.TemporaryDirectory() as space:
            directory = Path(space)
            kernel = Kernel(db, ObjectStore(directory / "objects"))
            roles = [provision(kernel, directory, i) for i in range(2)]
            results = asyncio.run(exercise(kernel, roles, args.samples_per_role, args.offered_rps))
            with db.uow(write=False) as repo:
                tasks = [role.expected["nf_task_get"] for role in roles]
                attempts = repo.conn.scalar(
                    text("SELECT count(*) FROM nf_call_attempts WHERE task_id IN (:a,:b)"),
                    {"a": tasks[0]["task_id"], "b": tasks[1]["task_id"]})
                refs = [task["world_ref"] for task in tasks]
                heads_unchanged = all(kernel.head(repo, ref["work_id"], ref["branch_id"]) == ref for ref in refs)
                postgres = repo.conn.scalar(text("SELECT version()"))
    finally:
        db.close()
    cgroup = Path("/sys/fs/cgroup")
    limits = {name: (cgroup / name).read_text().strip() for name in ("cpu.max", "memory.max")
              if (cgroup / name).is_file()}
    report = {"source_commit_at_start": source, "driver_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "platform": platform.platform(), "python": platform.python_version(), "logical_cpus_visible": os.cpu_count(),
              "cgroup_limits": limits,
              "postgres": postgres, "server_processes": 2, "client_processes": 1, "client_sessions": 2,
              "server_slots_per_binding": 2, "server_pg_pool_per_binding": 2, "server_pool_overflow": 0,
              "driver_db_pool": 1, "stdio_idle_seconds": 30, "warmup_per_role": "initialize, tools/list, one read per tool",
              "closed_client_concurrency_per_binding": 2, "open_client_inflight_cap_per_binding": 4,
              "distinct_tasks_principals_works": 2, "call_attempt_rows": attempts, "world_heads_unchanged": heads_unchanged,
              "phases": results}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if not heads_unchanged or attempts or any(r["statuses"] != {"OK": r["offered"]} for r in results):
        raise SystemExit("A read failed or did not match its bound role, or a workload invariant changed")


if __name__ == "__main__":
    main()
