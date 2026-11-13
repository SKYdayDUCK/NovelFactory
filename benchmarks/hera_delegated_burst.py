"""Disconnect all submitting clients, reopen the engine, verify durable supervised results."""
import argparse
import asyncio
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta
import gzip
import json
import logging
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep
from uuid import uuid4

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import text

from benchmarks.http_load import loopback_server
from benchmarks.p2_behavioral_load import hardware, isolated_kernel
from benchmarks.p59_workflow_load import FixtureAdapter
from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.delegation import install, provision_slot
from novel_factory.application.kernel import Kernel
from novel_factory.application.runtime_tasks import RuntimeTaskService
from novel_factory.application.supervisor import RuntimeSupervisor
from novel_factory.fixtures import load_fixture, point
from novel_factory.infrastructure.database import Database
from novel_factory.interfaces.http import HttpSettings, create_app


def prepare(kernel, adapter, count):
    gateway = AgentGateway(kernel, {"fixture": adapter})
    runtime, supervisor = RuntimeTaskService(kernel, gateway), RuntimeSupervisor(kernel, gateway)
    books = [load_fixture(kernel) for _ in range(2)]
    budgets = [kernel.tasks.create_budget(b["world_ref"]["work_id"], str(count)) for b in books]
    population = []
    for i in range(count):
        book = books[i % 2]
        owner, wr = book["principal"], book["world_ref"]
        instance = str(uuid4())
        install_ref = install(kernel, owner, nf_instance_id=instance, org_id=str(uuid4()), work_id=wr["work_id"],
                              branch_id=wr["branch_id"], expires_at=datetime.now(UTC) + timedelta(hours=4))
        job = runtime.define(owner, wr, role_id="evidence_maintainer", task_kind="summary", story_time=point(1),
                             subject_ids=[book["ids"]["sword"]], predicates=["owner"], assignment="Summarize visible evidence.",
                             adapter_id="fixture", max_amount="0.10", token_budget=100000, deadline_seconds=14400)
        # This completion scenario has no injected failures. The separate 2000 population
        # experiment exercises UNKNOWN/cancel and uses the same deterministic provider.
        adapter.jobs[job.spec["task_id"]] = 1
        provision_slot(supervisor, owner, install_ref["installation_id"], scope_ref="summary", task_template_id="summary.v1",
                       frozen=job, budget_id=budgets[i % 2], reserve_amount="0.10", token_reservation=100000,
                       accept_summary=True)
        cmd = dict(protocol_version="aw-nf.v1", nf_instance_id=instance, work_id=wr["work_id"], branch_id=wr["branch_id"],
                   operation_id=str(uuid4()), grant_epoch=1, task_template_id="summary.v1", scope_ref="summary",
                   actor_binding_ref=None, role_id="evidence_maintainer", generation=0, budget_reservation_ref=str(uuid4()),
                   max_amount="0.10", currency="USD", token_limit=100000)
        population.append((install_ref, cmd, job.spec["task_id"]))
    return books, population


async def submit_all(base, population):
    gate, ready = asyncio.Event(), asyncio.Queue()
    init_slots = asyncio.Semaphore(4)
    rows = []
    release = 0.0

    async def lane(i, binding, cmd, task_id):
        nonlocal release
        is_mcp = i % 4 in (0, 3)
        arrived = False
        status = "NO_RESPONSE"
        start = None
        try:
            async with AsyncExitStack() as stack:
                async with init_slots:
                    client = await stack.enter_async_context(httpx.AsyncClient(base_url=base, trust_env=False, timeout=240,
                        headers={"Authorization": "Bearer "+binding["token"]},
                        limits=httpx.Limits(max_connections=2, max_keepalive_connections=1, keepalive_expiry=300)))
                    if is_mcp:
                        uri = base + "/mcp/installations/" + binding["installation_id"]
                        stream = await stack.enter_async_context(streamable_http_client(uri, http_client=client))
                        sdk = await stack.enter_async_context(ClientSession(*stream[:2]))
                        await sdk.initialize()
                        assert "nf_delegation_submit" in {t.name for t in (await sdk.list_tools()).tools}
                    else:
                        assert (await client.get("/health")).is_success
                ready.put_nowait(i)
                arrived = True
                await gate.wait()
                start = monotonic()
                if is_mcp:
                    res = await sdk.call_tool("nf_delegation_submit", cmd)
                    body = res.structuredContent or {}
                    status = "OK" if not res.isError else body.get("error", {}).get("code", "ERROR")
                else:
                    path = "/api/v1/hera/installations/" + binding["installation_id"] + "/delegations/submit"
                    res = await client.post(path, json=cmd)
                    body = res.json()
                    status = "OK" if res.status_code == 202 else str(res.status_code)
                if status == "OK" and not (body.get("task_id") == task_id and body.get("operation_id") == cmd["operation_id"]
                                           and body.get("task_state") == "READY" and body.get("attempts") == []):
                    status = "WRONG_RECEIPT"
                elapsed = monotonic() - release
        except Exception as exc:
            if not arrived:
                ready.put_nowait(i)
            await gate.wait()
            status, elapsed = type(exc).__name__, monotonic() - release
        rows.append(dict(task_id=task_id, transport="mcp" if is_mcp else "http", status=status,
                         start_s=None if start is None else start-release, response_s=elapsed))

    async with asyncio.TaskGroup() as group:
        for i, row in enumerate(population):
            group.create_task(lane(i, *row))
        for _ in population:
            await asyncio.wait_for(ready.get(), 120)
        release = monotonic()
        gate.set()
    return rows


def drain(kernel, books, adapter, count):
    supervisor = RuntimeSupervisor(kernel, AgentGateway(kernel, {"fixture": adapter}))
    scopes = [(b["principal"], b["world_ref"]["work_id"], b["world_ref"]["branch_id"]) for b in books]
    started = monotonic()
    last = started
    outcomes, running = [], set()
    turn = 0
    with ThreadPoolExecutor(max_workers=2) as pool:
        while len(outcomes) < count:
            for _ in range(2):
                if len(running) == 2:
                    break
                future = supervisor.admit_model(*scopes[turn % 2], executor=pool)
                turn += 1
                if not isinstance(future, dict):
                    running.add(future)
            if running:
                done, running = wait(running, timeout=30, return_when=FIRST_COMPLETED)
                for future in done:
                    outcomes.append(future.result()["status"])
                    last = monotonic()
            else:
                sleep(.01)
            if monotonic()-last > 60:
                break
    for scope in scopes:
        while True:
            with kernel.db.uow(write=False) as repo:
                remaining = repo.conn.scalar(text("SELECT count(*) FROM nf_tasks WHERE work_id=:w AND state='RESULT_STORED'"),
                                             {"w": scope[1]})
            if not remaining:
                break
            supervisor.advance_world(*scope)
            with kernel.db.uow(write=False) as repo:
                after = repo.conn.scalar(text("SELECT count(*) FROM nf_tasks WHERE work_id=:w AND state='RESULT_STORED'"),
                                         {"w": scope[1]})
            if after >= remaining:
                break
    return dict(drain_s=monotonic()-started, worker_outcomes=dict(Counter(outcomes)), simulated_calls=sum(adapter.calls.values()),
                peak_active_provider_calls=adapter.peak)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--clients", required=True, type=int)
    p.add_argument("--output", required=True, type=Path)
    args = p.parse_args()
    if not 2 <= args.clients <= 1000:
        p.error("choose 2..1000 clients")
    logging.getLogger("mcp").setLevel(logging.WARNING)
    with TemporaryDirectory() as temp, isolated_kernel(os.environ["NF_TEST_DATABASE_URL"], Path(temp)) as (setup, url):
        setup.db.close()
        kernel = Kernel(Database(url, pool_size=4), setup.objects)
        try:
            adapter = FixtureAdapter()
            with kernel.db.uow() as repo:
                repo.conn.execute(text("UPDATE nf_durable_admission_policy SET enabled=true"))
            books, jobs = prepare(kernel, adapter, args.clients)
            settings = HttpSettings(hera_delegations=True, max_queued_ingress=1500, max_inflight_requests=64,
                                    max_queued_operations=1024, queue_timeout_seconds=180)
            with loopback_server(create_app(kernel, settings=settings), lifespan="on", keepalive=300) as base:
                rows = asyncio.run(submit_all(base, jobs))
            kernel.db.close()
            kernel = Kernel(Database(url, pool_size=4), setup.objects)
            accepted = sum(r["status"] == "OK" for r in rows)
            with kernel.db.uow(write=False) as repo:
                durable = repo.conn.scalar(text("SELECT count(*) FROM nf_delegation_receipts"))
            stats = drain(kernel, books, adapter, durable)
            with kernel.db.uow(write=False) as repo:
                effects = repo.conn.execute(text("""
                    SELECT t.task_id::text, t.state, t.generation, r.disposition,
                           (SELECT count(*) FROM nf_call_attempts c WHERE c.task_id=t.task_id) attempts,
                           (SELECT count(*) FROM nf_call_attempts c WHERE c.task_id=t.task_id AND c.state='SUCCEEDED') settled,
                           extract(epoch from (t.updated_at-t.created_at))::float8 lifetime_s
                    FROM nf_tasks t LEFT JOIN nf_task_results r ON r.result_id=t.current_result_id
                """)).mappings().all()
                by_id = {r["task_id"]: dict(r) for r in effects}
                for r in rows:
                    r["effect"] = by_id.get(r["task_id"])
                correct = sum(r["state"] == "ACCEPTED" and r["generation"] == 1 and r["disposition"] == "CURRENT"
                              and r["attempts"] == r["settled"] == adapter.calls[r["task_id"]] == 1 for r in effects)
                reserved = [str(r[0]) for r in repo.conn.execute(text("SELECT reserved_amount FROM nf_budgets"))]
            result = {**stats, "clients": args.clients, "mcp_sessions": sum(r["transport"]=="mcp" for r in rows),
                      "correct_acknowledgements": accepted, "durable_jobs":durable, "correct_terminal_results":correct,
                      "remaining_reservations":reserved, "max_response_s":max(r["response_s"] for r in rows),
                      "environment":hardware(), "rows":rows}
            args.output.parent.mkdir(parents=True,exist_ok=True)
            with gzip.open(args.output,"wt", encoding="utf8") as out:
                json.dump(result,out,separators=(",",":"))
            print({k:v for k,v in result.items() if k not in ("rows","environment")}, flush=True)
        finally:
            kernel.db.close()


if __name__ == "__main__":
    main()
