"""Loopback SDK Streamable HTTP with two private role bindings and a fixed total PG budget."""
import argparse
import asyncio
from collections import Counter
from contextlib import AsyncExitStack, asynccontextmanager
import gzip
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import random
import socket
import subprocess
import sys
import tempfile
from time import perf_counter

import anyio
import httpx
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from sqlalchemy import text
from starlette.applications import Starlette
from starlette.responses import Response
from starlette.routing import Route

from benchmarks.bound_stdio_throughput import provision
from benchmarks.p2_behavioral_load import isolated_kernel
from novel_factory.application.kernel import Kernel
from novel_factory.application.mcp_runtime import McpRuntime, TaskBinding
from novel_factory.domain.errors import DomainError
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.objects import ObjectStore
from novel_factory.interfaces.bindings import read_private_config
from novel_factory.interfaces.mcp_stdio import BoundToolAdmission, make_server


async def serve(paths, slots):
    # Only server-provisioned files enter this experiment. Never accept a task
    # binding, database URL or object-store path from a request.
    configs = [read_private_config(path) for path in paths]
    db = Database(configs[0]["database_url"], pool_size=slots, max_overflow=0)
    kernel = Kernel(db, ObjectStore(configs[0]["object_root"]))
    admission = BoundToolAdmission(slots)
    auth_inflight = anyio.CapacityLimiter(4)
    auth_workers = anyio.CapacityLimiter(slots)
    security = TransportSecuritySettings(allowed_hosts=["127.0.0.1:*"])
    managers = {}
    for config in configs:
        principal = kernel.authenticate(config["token"])
        runtime = McpRuntime(kernel, principal, TaskBinding(**config["binding"]))
        managers[principal] = StreamableHTTPSessionManager(
            make_server(runtime, admission=admission), stateless=True,
            json_response=True, security_settings=security,
        )

    class Endpoint:
        async def __call__(self, scope, receive, send):
            from starlette.datastructures import Headers
            prefix, _, token = Headers(scope=scope).get("authorization", "").partition(" ")
            manager = None
            if prefix == "Bearer" and token:
                try:
                    auth_inflight.acquire_nowait()
                except anyio.WouldBlock:
                    return await Response(status_code=429)(scope, receive, send)
                try:
                    principal = await anyio.to_thread.run_sync(kernel.authenticate, token, limiter=auth_workers)
                    manager = managers.get(principal)
                except DomainError:
                    pass
                finally:
                    auth_inflight.release()
            if manager is None:
                return await Response(status_code=401)(scope, receive, send)
            await manager.handle_request(scope, receive, send)

    @asynccontextmanager
    async def lifespan(_app):
        async with AsyncExitStack() as stack:
            for manager in managers.values():
                await stack.enter_async_context(manager.run())
            yield

    try:
        app = Starlette(routes=[Route("/mcp", Endpoint(), methods=["GET", "POST", "DELETE"])], lifespan=lifespan)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            address = listener.getsockname()
            server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
            running = asyncio.create_task(server.serve(sockets=[listener]))
            while not server.started and not running.done():
                await asyncio.sleep(.005)
            if not server.started:
                await running
                raise RuntimeError("Server startup failed")
            print(address[1], flush=True)
            await running
    finally:
        admission.close()
        db.close()


@asynccontextmanager
async def replicas(roles, count):
    processes = []
    ports = []
    cold = []
    try:
        for _ in range(count):
            start = perf_counter()
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "benchmarks.p59_mcp_remote", "--worker", "--slots", str(2 // count),
                *(str(role.path) for role in roles), stdout=asyncio.subprocess.PIPE,
            )
            processes.append(proc)
            line = await asyncio.wait_for(proc.stdout.readline(), 45)
            ports.append(int(line.strip()))
            cold.append(perf_counter() - start)
        yield ports, cold
    finally:
        for proc in processes:
            if proc.returncode is None:
                proc.terminate()
        for proc in processes:
            try:
                await asyncio.wait_for(proc.wait(), 10)
            except TimeoutError:
                proc.kill()
                await proc.wait()


async def measure(roles, policy, count, calls):
    async with replicas(roles, count) as (ports, cold), AsyncExitStack() as stack:
        sessions = {}
        for index, role in enumerate(roles):
            cfg = read_private_config(role.path)
            for replica, port in enumerate(ports):
                client = await stack.enter_async_context(httpx.AsyncClient(
                    headers={"Authorization": "Bearer " + cfg["token"]}, trust_env=False, timeout=15))
                url = f"http://127.0.0.1:{port}/mcp"
                streams = await stack.enter_async_context(streamable_http_client(url, http_client=client))
                session = await stack.enter_async_context(ClientSession(streams[0], streams[1]))
                init = await session.initialize()
                assert init.protocolVersion == "2025-11-25" and streams[2]() is None
                assert {t.name for t in (await session.list_tools()).tools} == {
                    "nf_task_get", "nf_context_get", "nf_query_batch", "nf_artifact_put"}
                warm = await session.call_tool("nf_task_get", {})
                assert warm.structuredContent == role.expected["nf_task_get"]
                wrong = await session.call_tool("nf_task_get", {"task_id": roles[1-index].expected["nf_task_get"]["task_id"]})
                assert wrong.isError and wrong.structuredContent["error"]["code"] == "INVALID_ARGUMENT"
                sessions[index, replica] = session
        async with httpx.AsyncClient(trust_env=False) as unauthorized:
            for port in ports:
                assert (await unauthorized.post(f"http://127.0.0.1:{port}/mcp", json={})).status_code == 401
        pending = [0] * count
        next_replica = 0
        rng = random.Random(9071)
        rows = []
        start = perf_counter()

        async def stream(lane):
            nonlocal next_replica
            for seq in range(lane, calls, 4):
                index = seq % 2
                tool = ("nf_task_get", "nf_context_get")[(seq // 2) % 2]
                candidates = list(range(count))
                if policy == "rr":
                    dest = next_replica % count
                elif policy == "lor":
                    dest = min(candidates, key=lambda k: (pending[k], (k-next_replica) % count))
                else:
                    pair = rng.sample(candidates, min(2, count))
                    dest = min(pair, key=pending.__getitem__)
                next_replica += 1
                backlog = pending.copy()
                pending[dest] += 1
                before = perf_counter()
                try:
                    async with asyncio.timeout(15):
                        result = await sessions[index, dest].call_tool(tool, {})
                    status = (result.structuredContent or {}).get("error", {}).get("code", "MCP_ERROR") if result.isError else (
                        "OK" if result.structuredContent == roles[index].expected[tool] else "WRONG_CONTENT")
                except Exception as exc:
                    status = type(exc).__name__
                    # SDK may convert an HTTP admission rejection into a JSON-RPC error.
                    if "429" in str(exc):
                        status = "HTTP_429"
                finally:
                    pending[dest] -= 1
                rows.append(dict(seq=seq, role=index, tool=tool, replica=dest, outstanding=backlog,
                                 start_ms=1000*(before-start), elapsed_ms=1000*(perf_counter()-before), status=status))
        await asyncio.gather(*(stream(i) for i in range(4)))
        elapsed = perf_counter()-start
        statuses = Counter(r["status"] for r in rows)
        times = sorted(r["elapsed_ms"] for r in rows)
        return dict(policy=policy, replicas=count, process_cold_s=cold, statuses=statuses,
                    verified_rps=statuses["OK"]/elapsed, elapsed_s=elapsed, rows=rows,
                    tails_ms={str(p): times[math.ceil(p * len(times)/100)-1] for p in (50,95,99)})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--slots", type=int, choices=(1, 2), default=1)
    parser.add_argument("paths", nargs="*")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--calls", type=int, default=80)
    args = parser.parse_args()
    logging.disable(logging.WARNING)
    if args.worker:
        return asyncio.run(serve(args.paths, args.slots))
    if not args.output or args.calls < 4:
        parser.error("Pass --output and at least four calls")
    with tempfile.TemporaryDirectory() as tmp, isolated_kernel(os.environ["NF_TEST_DATABASE_URL"], Path(tmp)/"objects") as (kernel, _):
        roles = [provision(kernel, Path(tmp), i) for i in range(2)]
        assert roles[0].principal_id != roles[1].principal_id
        order = [(1,"rr"), (2,"rr"), (2,"rr"), (1,"rr"), (2,"rr"), (2,"lor"),
                 (2,"lor"), (2,"rr"), (2,"rr"), (2,"p2c"), (2,"p2c"), (2,"rr")]
        blocks = []
        for count, policy in order:
            result = asyncio.run(measure(roles, policy, count, args.calls))
            blocks.append(result)
            print(count, policy, json.dumps(result["statuses"]), flush=True)
        with kernel.db.uow(write=False) as repo:
            heads = [r.expected["nf_task_get"]["world_ref"] for r in roles]
            unchanged = all(kernel.head(repo, h["work_id"], h["branch_id"]) == h for h in heads)
            attempts = repo.conn.scalar(text("SELECT count(*) FROM nf_call_attempts"))
        data = dict(blocks=blocks, sha=subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip(),
                    driver_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                    correct_heads=unchanged, attempts=attempts, checked_authorization=True)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(args.output, "wt") as out:
            json.dump(data, out, separators=(",", ":"))
        if not unchanged or attempts or any(set(b["statuses"])-{"OK","HTTP_429","BACKPRESSURE"} for b in blocks):
            raise SystemExit("FAILED: semantic or unexpected transport outcome (see raw receipts)")


if __name__ == "__main__":
    main()
