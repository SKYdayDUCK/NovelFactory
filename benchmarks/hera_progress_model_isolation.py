"""Compare authorized project progress with two model jobs held at a simulated provider."""
import argparse
import asyncio
import gzip
import json
import os
import subprocess
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta
from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import text

from benchmarks.hera_mixed import task_spec
from benchmarks.hera_model_isolation import WaitingFixture
from benchmarks.http_load import SqlMeasurements, loopback_server, percentiles
from benchmarks.p2_behavioral_load import hardware, isolated_kernel
from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.kernel import Kernel
from novel_factory.application.mcp_runtime import McpRuntime, ProjectBinding
from novel_factory.application.runtime_tasks import RuntimeTaskService
from novel_factory.application.supervisor import RuntimeSupervisor
from novel_factory.domain.canonical import fingerprint
from novel_factory.fixtures import load_fixture, point
from novel_factory.infrastructure.database import Database
from novel_factory.interfaces.http import HttpSettings, create_app


def setup(kernel, gateway, supervisor, provider):
    scopes, readers, tasks = [], [], []
    runtime = RuntimeTaskService(kernel, gateway)
    for index in range(2):
        book = load_fixture(kernel)
        owner, world = book["principal"], book["world_ref"]
        work, branch = world["work_id"], world["branch_id"]
        scopes.append((owner, work, branch))
        budget = kernel.tasks.create_budget(work, "2")
        if index:
            # One terminal status distinguishes the two branches even if the active counts match.
            marker = task_spec(owner, world)
            marker["role_id"] = "control_operator"
            kernel.create_task(owner, marker, "tag:" + marker["task_id"])
            kernel.cancel_task(owner, marker["task_id"], 0, "cancel-tag:" + marker["task_id"])
        for channel in ("rest", "mcp"):
            grant = kernel.issue_grant(owner, work, role_id="resume_operator",
                capabilities=["PROJECT_RESUME"], branch_ids=[branch],
                expires_at=datetime.now(UTC) + timedelta(hours=1))
            readers.append((grant, ProjectBinding(work, branch), channel))
        for _ in range(2):
            job = runtime.prepare(owner, world, role_id="evidence_maintainer", task_kind="summary",
                story_time=point(1), subject_ids=[book["ids"]["sword"]], predicates=["owner"],
                assignment="Summarize visible evidence.", adapter_id="fixture", queue_state="READY")
            key = job.task["task_id"]
            provider.jobs[key] = len(tasks)
            tasks.append(key)
            supervisor.enroll(owner, key, manifest_ref=job.manifest_ref, knowledge_cut={"plane":"WORLD"},
                context_tokens=20000, input_ref=job.agent_input["input_ref"], budget_id=budget,
                reserve_amount="0.1", token_reservation=100000)
    return scopes, readers, tasks


def ledger(kernel, scopes):
    with kernel.db.uow(write=False) as repo:
        by_branch = {}
        for _, work, branch in scopes:
            groups = repo.conn.execute(text("""
                SELECT state,count(*) FROM nf_tasks WHERE work_id=:w AND branch_id=:b GROUP BY state
            """), {"w":work,"b":branch}).all()
            by_branch[branch] = dict(groups)
        attempts = repo.conn.execute(text("""
            SELECT task_id::text,attempt_id::text,state FROM nf_call_attempts ORDER BY task_id
        """)).all()
        leased = list(repo.conn.scalars(text("SELECT task_id::text FROM nf_dispatch_slots ORDER BY task_id")))
        return {"states":by_branch,"attempts":[tuple(row) for row in attempts],"leased":leased}


async def read_progress(kernel, readers, states):
    app = create_app(kernel, settings=HttpSettings(rate_burst=1000),
                     mcp_bindings={grant["principal"]:binding for grant,binding,_ in readers})
    rows, expected_by_grant = [], {}
    with loopback_server(app, lifespan="on") as base:
        async with AsyncExitStack() as stack:
            lanes = []
            for grant, binding, channel in readers:
                expected = McpRuntime(kernel, grant["principal"], binding).project()
                totals = states[binding.branch_id]
                assert {s:n for s,n in expected["counts"].items() if n} == totals
                assert expected["ready_count"] == totals["READY"]
                expected_by_grant[grant["principal"].grant_id] = expected
                client = await stack.enter_async_context(httpx.AsyncClient(
                    base_url=base, headers={"Authorization":"Bearer "+grant["token"]}, trust_env=False))
                if channel == "mcp":
                    wires = await stack.enter_async_context(streamable_http_client(base+"/mcp", http_client=client))
                    client = await stack.enter_async_context(ClientSession(*wires[:2]))
                    await client.initialize()
                    await client.list_tools()
                else:
                    assert (await client.get(f"/api/v1/works/{binding.work_id}/branches/{binding.branch_id}/workflow")).is_success
                lanes.append((grant["principal"].grant_id, binding, channel, client))

            async def read(grant_id, binding, channel, client):
                expected = expected_by_grant[grant_id]
                for sequence in range(20):
                    start = perf_counter()
                    if channel == "mcp":
                        result = await client.call_tool("nf_project_status", {})
                        value = result.structuredContent or {}
                        success = not result.isError
                    else:
                        result = await client.get(f"/api/v1/works/{binding.work_id}/branches/{binding.branch_id}/workflow")
                        value = result.json()
                        success = result.is_success
                    end = perf_counter()
                    rows.append(dict(grant=grant_id,work=binding.work_id,branch=binding.branch_id,
                                     transport=channel,sequence=sequence,start=start-t0,end=end-t0,
                                     verified=success and value==expected,receipt_sha256=fingerprint(value)))

            with SqlMeasurements(kernel.db.engine) as sql:
                t0 = perf_counter()
                await asyncio.gather(*(read(*lane) for lane in lanes))
                elapsed = perf_counter() - t0
    verified = sum(row["verified"] for row in rows)
    return dict(verified=verified,seconds=elapsed,goodput=verified/elapsed,
                latency=percentiles([row["end"]-row["start"] for row in rows]),
                sql=sql.result(elapsed),expected_by_grant=expected_by_grant,rows=rows)


def arm(kernel, loaded):
    provider = WaitingFixture()
    gateway = AgentGateway(kernel, {"fixture":provider})
    supervisor = RuntimeSupervisor(kernel, gateway)
    scopes, readers, tasks = setup(kernel, gateway, supervisor, provider)
    with kernel.db.uow() as repo:
        assert repo.one("dispatch_limits", {"scope":"GLOBAL","subject":"*"})["slots"] == 2
        repo.add("dispatch_limits", {"scope":"RESOURCE","subject":"MODEL","slots":2})
    with ThreadPoolExecutor(2) as executor:
        futures = []
        try:
            if loaded:
                futures = [supervisor.admit_model(*scope, executor=executor) for scope in scopes]
                assert provider.arrived.wait(20)
            occupied = kernel.db.engine.pool.checkedout()
            assert occupied == 0
            before = ledger(kernel, scopes)
            for i, (_, _, b) in enumerate(scopes):
                counts = dict(READY=1,RUNNING=1) if loaded else dict(READY=2)
                if i:
                    counts["CANCELLED"] = 1
                assert before["states"][b] == counts
            assert len(before["leased"]) == (2 if loaded else 0)
            assert Counter(a[2] for a in before["attempts"]) == ({"SENT":2} if loaded else {})
            results = asyncio.run(read_progress(kernel, readers, before["states"]))
            after = ledger(kernel, scopes)
            assert before == after and results["verified"] == 80
        finally:
            provider.release.set()
        for future in futures:
            assert future.result()["status"] == "RESULT_STORED"
    if loaded:
        for scope in scopes:
            assert supervisor.admit_model(*scope)["status"] == "RESULT_STORED"
    final = ledger(kernel, scopes)
    if loaded:
        assert all(final["states"][branch].get("RESULT_STORED") == 2 for _,_,branch in scopes)
        assert Counter(a[0] for a in final["attempts"]) == Counter(tasks)
        assert provider.calls == Counter(tasks) and not final["leased"]
    return dict(loaded=loaded,model_tasks=tasks,checked_out_at_barrier=occupied,
                before=before,after=after,final=final,fixture_calls=dict(provider.calls),reads=results)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    results = dict(environment=hardware(),mcp_sdk=version("mcp"),pool_size=4,max_overflow=0,
                   model_slots=2,source=subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip(),arms=[])
    os.environ["NF_HERA_PROJECT_PROGRESS"] = "0"
    for loaded in (False,True,True,False):
        with TemporaryDirectory() as tmp, isolated_kernel(os.environ["NF_TEST_DATABASE_URL"],Path(tmp)) as (seed,url):
            seed.db.close()
            kernel = Kernel(Database(url,pool_size=4,max_overflow=0),seed.objects)
            try:
                record = arm(kernel, loaded)
            finally:
                kernel.db.close()
        results["arms"].append(record)
        with gzip.open(args.output,"wt") as output:
            json.dump(results,output,separators=(",",":"))
        print(loaded,record["reads"]["verified"],record["reads"]["latency"],flush=True)


if __name__ == "__main__":
    main()
