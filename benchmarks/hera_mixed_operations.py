"""Concurrent public business operations with frozen role and world oracles."""
import argparse
import asyncio
import gzip
import json
import os
import subprocess
from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from uuid import uuid4

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import func, select, text

from benchmarks.hera_mixed import task_spec
from benchmarks.http_load import loopback_server
from benchmarks.p2_behavioral_load import isolated_kernel
from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.mcp_runtime import McpRuntime, ProjectBinding, TaskBinding, TaskOperationsBinding
from novel_factory.application.queries import WorldQueryService
from novel_factory.application.runtime_tasks import RuntimeTaskService
from novel_factory.domain.canonical import fingerprint
from novel_factory.fixtures import create_bound_task, load_fixture, point, seed_sword_history
from novel_factory.infrastructure.database import tables
from novel_factory.infrastructure.model_gateway import OpenAICompatibleAdapter
from novel_factory.interfaces.http import HttpSettings, create_app


def frozen_state(kernel, branch):
    with kernel.db.uow(write=False) as repo:
        return dict(head=repo.one("branches", {"branch_id": branch})["head_revision_id"],
                    revisions=repo.conn.scalar(select(func.count()).select_from(tables["revisions"])),
                    events=repo.conn.scalar(select(func.count()).select_from(tables["events"])),
                    attempts=repo.conn.scalar(select(func.count()).select_from(tables["call_attempts"])),
                    budget=list(map(str, repo.conn.execute(text("SELECT coalesce(sum(spent_amount),0), "
                        "coalesce(sum(reserved_amount),0) FROM nf_budgets")).one())))


def setup(kernel):
    world = load_fixture(kernel)
    history = seed_sword_history(kernel, world)
    wr = history["world_ref"]
    ids = world["ids"]
    owner = world["principal"]
    resume = kernel.issue_grant(owner, wr["work_id"], role_id="resume_operator", capabilities=["PROJECT_RESUME"],
                                branch_ids=[wr["branch_id"]], expires_at=datetime.now(UTC) + timedelta(hours=1))
    project = ProjectBinding(wr["work_id"], wr["branch_id"])
    bindings = {resume["principal"]: project, owner: TaskOperationsBinding(wr["work_id"], wr["branch_id"])}
    gateway = AgentGateway(kernel, {"fixture": OpenAICompatibleAdapter(
        endpoint="https://example.test/v1/chat/completions", model="fixture", api_key=None)})
    actors = []
    for ref, actor in [(history["steps"][0]["world_ref"], "friend"), (wr, "protagonist")]:
        role = RuntimeTaskService(kernel, gateway).prepare(owner, ref, role_id="character_actor",
            task_kind="character_action", actor_id=ids[actor], story_time=point(35),
            subject_ids=[ids["sword"]], predicates=["active_holder"], assignment="Observe the assigned item.",
            adapter_id="fixture", lease_seconds=900)
        b = TaskBinding(role.task["task_id"], role.task["generation"], role.worker_id)
        bindings[role.principal] = b
        runtime = McpRuntime(kernel, role.principal, b)
        ctx = runtime.context_get()
        assert ctx["actor_id"] == ids[actor] and ctx["world_ref"] == ref
        actors.append((role, ctx))
    hist_task = create_bound_task(kernel, owner, wr, point(60), [ids["sword"]], ["owner", "active_holder"])
    second_task = create_bound_task(kernel, owner, wr, point(60), [ids["sword"]], ["owner"])
    hist = {"schema_version": "2.1", "task_id": hist_task["task_id"], "generation": hist_task["generation"],
            "read_contract": {"revision": {"mode": "FIXED", "world_ref": wr}, "process_policy": "ALLOW_PARTIAL",
                              "index_policy": "AUTHORITATIVE", "max_wait_ms": 0},
            "knowledge_cut": {"plane": "WORLD"},
            "subject_time": {"kind": "INTERVAL", "interval": {"start": point(1), "end": point(50), "bounds": "[)"}},
            "query": {"kind": "TIMELINE", "subject_ids": [ids["sword"]], "entry_kinds": ["FACT"],
                      "time_axis": "SOURCE", "include_inactive": True}, "page_size": 100, "cursor": None}
    oracle = WorldQueryService(kernel).query(owner, hist)["payload"]
    assert len(oracle["entries"]) >= 4
    return world, history, bindings, resume, actors, hist, oracle, second_task["task_id"]


async def measure(kernel, rounds, directory):
    world, history, bindings, resume, actors, hist, expected_history, second_task = setup(kernel)
    wr, ids = history["world_ref"], world["ids"]
    app = create_app(kernel, settings=HttpSettings(fenced_task_status=True,
                     rate_burst=1000, per_principal_concurrency=6), mcp_bindings=bindings)
    before = frozen_state(kernel, wr["branch_id"])
    base_task = hist["task_id"]
    rows = []
    accepted = []
    with loopback_server(app, lifespan="on") as base:
        async with AsyncExitStack() as stack:
            async def client(token):
                http = await stack.enter_async_context(httpx.AsyncClient(base_url=base, trust_env=False,
                    headers={"Authorization": "Bearer "+token}, timeout=30))
                streams = await stack.enter_async_context(streamable_http_client(base+"/mcp", http_client=http))
                sdk = await stack.enter_async_context(ClientSession(streams[0],streams[1]))
                await sdk.initialize()
                return http, sdk

            owner_http, owner_sdk = await client(world["token"])
            _, project_sdk = await client(resume["token"])
            actor_clients = [await client(role.private_token) for role,_ in actors]
            world_status = (await owner_http.get("/api/v1/tasks/"+base_task)).json()
            assert world_status["world_ref"] == wr and world_status["task_id"] == base_task

            second_status = (await owner_http.get("/api/v1/tasks/"+second_task)).json()
            assert second_status["world_ref"] == wr and second_status["task_id"] == second_task

            async def wire(n, label, transport, call, oracle, gate=None):
                if gate is not None:
                    await gate.wait()
                start=perf_counter()
                value={}
                code="ERROR"
                try:
                    response=await call
                    if transport=="mcp":
                        value=response.structuredContent or {}
                        code=value.get("error",{}).get("code","MCP_ERROR") if response.isError else "OK"
                    else:
                        value=response.json()
                        code="OK" if response.status_code in (200,202) else value.get("code",str(response.status_code))
                    ok=bool(oracle(code,value))
                except Exception as exc:  # noqa: BLE001 - retain every offered result
                    ok=False
                    code=type(exc).__name__
                rows.append(dict(round=n, operation=label, transport=transport, code=code, ok=ok,
                                 started=start, completed=perf_counter(), result_sha256=fingerprint(value)))
                return value

            for n in range(rounds):
                spec=task_spec(world["principal"], wr)
                spec["role_id"]="control_operator"
                tid=spec["task_id"]
                accepted.append(tid)
                cmd={"spec":spec,"idempotency_key":"mixed:"+tid}
                expect=lambda c,v:c=="OK" and v.get("task_id")==tid and v.get("state")=="READY"
                def command(origin, name, payload):
                    if origin=="mcp":
                        return owner_sdk.call_tool("nf_task_"+name,payload)
                    path="/api/v1/hera/tasks" if name=="submit" else "/api/v1/tasks/cancel"
                    return owner_http.post(path,json=payload)
                origin="mcp" if n%2 else "http"
                other="http" if origin=="mcp" else "mcp"
                gate=asyncio.Event()
                pending=[]
                def add(label,transport,call,oracle):
                    pending.append(asyncio.create_task(wire(n,label,transport,call,oracle,gate)))
                for i, ((_,sdk),(_,ctx)) in enumerate(zip(actor_clients,actors,strict=True)):
                    add("context."+str(i),"mcp",sdk.call_tool("nf_context_get",{}),
                        lambda c,v,ctx=ctx: c=="OK" and v==ctx)
                lookups=[dict(subject_id=ids["sword"],predicate="active_holder")]
                add("actor.point","mcp",actor_clients[1][1].call_tool("nf_query_batch",{"lookups":lookups}),
                    lambda c,v:c=="OK" and v["items"][0]["payload"]["state_items"][0]["answer"]["data"]["entity_id"]==ids["friend"])
                add("actor.denied","mcp",actor_clients[0][1].call_tool("nf_query_batch",{"lookups":[
                    dict(subject_id=ids["sword"],predicate="outside_manifest")]}),lambda c,v:c=="VIEW_DENIED")
                add("history.interval","http",owner_http.post("/api/v1/queries/timeline",json=hist),
                    lambda c,v:c=="OK" and v["read_cut"]["world_ref"]==wr and v["payload"]==expected_history)
                add("foreign.denied","http",actor_clients[0][0].get("/api/v1/tasks/"+actors[1][0].task["task_id"]),
                    lambda c,v:c=="VIEW_DENIED")
                add("project.progress","mcp",project_sdk.call_tool("nf_project_status",{}),
                    lambda c,v:c=="OK" and v["counts"]["RUNNING"]>=3 and v["ready_count"]==v["counts"]["READY"])
                add("task.status","http",owner_http.get("/api/v1/tasks/"+base_task),lambda c,v:c=="OK" and v==world_status)
                batch_status = [world_status, second_status] if n % 2 else [second_status, world_status]
                add("task.batch","mcp",owner_sdk.call_tool("nf_task_status_batch",{"work_id":wr["work_id"],
                    "branch_id":wr["branch_id"],"task_ids":[item["task_id"] for item in batch_status]}),lambda c,v:c=="OK" and v=={"tasks":batch_status})
                add("task.submit",origin,command(origin,"submit",cmd),expect)
                gate.set()
                out=await asyncio.gather(*pending)
                await wire(n,"submit.retry",other,command(other,"submit",cmd),lambda c,v:expect(c,v) and v==out[-1])
                cancel={"task_id":tid,"generation":0,"idempotency_key":"cancel:"+tid}
                receipt=await wire(n,"task.cancel",other,command(other,"cancel",cancel),
                    lambda c,v:c=="OK" and v["task_id"]==tid and v["status"]=="CANCELLED")
                await wire(n,"cancel.retry",origin,command(origin,"cancel",cancel),
                           lambda c,v:c=="OK" and v==receipt)
    after=frozen_state(kernel,wr["branch_id"])
    with kernel.db.uow(write=False) as repo:
        counters=dict(repo.conn.execute(text("""
            SELECT (SELECT count(*) FROM nf_tasks WHERE task_id=ANY(CAST(:ids AS uuid[])) AND state='CANCELLED') as tasks,
                   (SELECT count(*) FROM nf_outbox WHERE aggregate_id=ANY(CAST(:ids AS text[])) AND topic='task.changed') as transitions,
                   (SELECT count(*) FROM nf_task_commands WHERE receipt->>'task_id'=ANY(CAST(:ids AS text[]))) as receipts
        """),{"ids":accepted}).mappings().one())
    ok=(before==after and before["attempts"]==0 and counters==dict(tasks=rounds,transitions=rounds*2,receipts=rounds*2)
         and all(row["ok"] for row in rows) and len(rows)==13*rounds)
    result=dict(source=subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip(),rounds=rounds,ok=ok,
        world_refs=[ctx["world_ref"] for _,ctx in actors],actors=[ctx["actor_id"] for _,ctx in actors],
        status_task_ids=[base_task,second_task],task_ids=accepted,history_entries=len(expected_history["entries"]),before=before,after=after,
        counters=counters,rows=rows)
    directory.mkdir(parents=True,exist_ok=True)
    with gzip.open(directory/f"mixed-{rounds}.json.gz","wt") as f: json.dump(result,f,separators=(",",":"))
    print(rounds,ok,[(x["operation"],x["code"]) for x in rows if not x["ok"]][:10],flush=True)
    return ok


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    for rounds in (2,12):
        with TemporaryDirectory() as tmp, isolated_kernel(os.environ["NF_TEST_DATABASE_URL"],Path(tmp)) as (kernel,_):
            if not asyncio.run(measure(kernel,rounds,args.output)):
                raise SystemExit(1)


if __name__=="__main__":
    main()
