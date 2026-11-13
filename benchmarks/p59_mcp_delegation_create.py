"""Time the production Workshop worker creating four authorized NF tasks per batch."""
import argparse
import asyncio
from datetime import UTC, datetime, timedelta
import hashlib
from importlib.metadata import version
import json
import math
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
from time import perf_counter
from uuid import uuid4

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from sqlalchemy import text

from agent_workshop.admin import enroll
from agent_workshop.auth import connect
from agent_workshop.organization import bootstrap
from agent_workshop.provision import allow_slot
from benchmarks.p59_workflow_load import FixtureAdapter
from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.delegation import install, provision_slot
from novel_factory.application.kernel import Kernel
from novel_factory.application.runtime_tasks import RuntimeTaskService
from novel_factory.application.supervisor import RuntimeSupervisor
from novel_factory.fixtures import load_fixture, point
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.objects import ObjectStore
from novel_factory.infrastructure.private_files import create_private_file
from workshop.integration.conftest import database_pair


def profile(directory, module, binding):
    path = directory / (module + ".json")
    with os.fdopen(create_private_file(path), "w", encoding="utf-8") as stream:
        json.dump(binding, stream)
    return StdioServerParameters(command=sys.executable, args=["-m", module, "--binding", str(path)])


async def enroll_intents(params, slots):
    async with stdio_client(params) as streams, ClientSession(*streams) as client:
        await client.initialize()
        if "aw_delegate" not in {tool.name for tool in (await client.list_tools()).tools}:
            raise RuntimeError("Worker has no AW delegation capability")
        operations = {}
        for slot, task in slots.items():
            reply = await client.call_tool("aw_delegate", {
                "operation_id": slot, "slot_id": slot, "max_amount": "1", "token_limit": 2000,
            })
            if reply.isError or reply.structuredContent["state"] != "REQUESTED":
                raise RuntimeError("AW could not durably accept a new intent")
            operations[reply.structuredContent["operation_id"]] = task
        return operations


def phase(kernel, aw_dsn, directory, reuse):
    world = load_fixture(kernel)
    wr, owner = world["world_ref"], world["principal"]
    actor = world["ids"]["protagonist"]
    instance, org = str(uuid4()), str(uuid4())
    grant = install(kernel, owner, nf_instance_id=instance, org_id=org, work_id=wr["work_id"],
                    branch_id=wr["branch_id"], expires_at=datetime.now(UTC) + timedelta(hours=1))
    installation = grant["installation_id"]
    gateway = AgentGateway(kernel, {"fixture": FixtureAdapter()})
    supervisor = RuntimeSupervisor(kernel, gateway)
    budget = kernel.tasks.create_budget(wr["work_id"], "4")
    with connect(aw_dsn) as conn:
        conn.execute("INSERT INTO aw_installations VALUES (%s,%s,true)", (installation, instance))
        bootstrap(conn, org, installation_id=installation)
        conn.execute("INSERT INTO aw_budget_accounts VALUES (%s,4,'USD')", (org,))
        token = enroll(conn, installation, org, "volume", 1, 1)
    slots = {}
    for index in range(4):
        slot = f"scene-{index}"
        frozen = RuntimeTaskService(kernel, gateway).define(
            owner, wr, role_id="character_actor", task_kind="character_action", actor_id=actor,
            story_time=point(1), subject_ids=[world["ids"]["sword"]], predicates=["owner"],
            assignment="Propose a visible action.", adapter_id="fixture", max_amount="1", token_budget=2000,
        )
        provision_slot(supervisor, owner, installation, scope_ref=slot, task_template_id="actor",
                       frozen=frozen, budget_id=budget, reserve_amount="1", token_reservation=2000,
                       context_tokens=1000)
        policy = dict(nf_instance_id=instance, work_id=wr["work_id"], branch_id=wr["branch_id"],
                      generation=0, grant_epoch=grant["grant_epoch"], scope_ref=slot, task_template_id="actor",
                      actor_binding_ref=actor, role_id="character_actor", max_amount="1", currency="USD",
                      token_limit=2000)
        with connect(aw_dsn) as conn:
            allow_slot(conn, org, installation, slot, "actor-1", policy)
        slots[slot] = frozen.spec["task_id"]
    aw = profile(directory, "agent_workshop.server", {"token": token, "database_url": aw_dsn})
    nf = profile(directory, "novel_factory.interfaces.delegation_stdio", {
        "installation_id": installation, "token": grant["token"], "object_root": str(kernel.objects.root),
        "database_url": kernel.db.engine.url.render_as_string(hide_password=False),
    })
    operations = asyncio.run(enroll_intents(aw, slots))
    args = [sys.executable, "-u", "-m", "agent_workshop.reconcile", "--installation", installation,
            "--org", org, "--limit", "4"]
    if reuse:
        args.append("--reuse-session")
    previous = started = perf_counter()
    rows = []
    with subprocess.Popen([*args, "--", nf.command, *nf.args], env={**os.environ, "AW_DATABASE_URL": aw_dsn},
                          stdout=subprocess.PIPE, text=True) as proc:
        for line in proc.stdout:
            ended = perf_counter()
            reply = json.loads(line)
            rows.append({**reply, "service_ms": (ended - previous) * 1000, "completed_s": ended - started})
            previous = ended
        if proc.wait():
            raise RuntimeError("Worker exited with an error")
    elapsed = perf_counter() - started
    if {r["operation_id"] for r in rows} != set(operations) or len(rows) != len(slots):
        raise RuntimeError("AW failed to reconcile every operation exactly once")
    with connect(aw_dsn) as conn, kernel.db.uow(write=False) as repo:
        for row in rows:
            op = row["operation_id"]
            expected = operations[op]
            hold = conn.execute("SELECT status,amount FROM aw_budget_holds WHERE operation_id=%s AND org_id=%s",
                                (op, org)).fetchone()
            remote = conn.execute("SELECT task_id,task_state FROM aw_remote_receipts WHERE operation_id=%s "
                                  "AND org_id=%s", (op, org)).fetchone()
            receipt = repo.one("delegation_receipts", {"installation_id": installation, "operation_id": op})
            task = repo.one("tasks", {"task_id": expected})
            if (row["state"] != "BOUND" or hold["status"] != "HELD" or hold["amount"] != 1 or
                    str(remote["task_id"]) != expected or remote["task_state"] != task["state"] or
                    task["state"] != "READY" or task["work_id"] != wr["work_id"] or receipt["task_id"] != expected):
                raise RuntimeError("Mismatched delegation, task or hold")
            row.update(task_id=expected, task_state=task["state"], hold=hold["status"],
                       budget_amount=str(hold["amount"]), receipt_ref=receipt["receipt_ref"])
        if (len(repo.many("delegation_receipts", {"installation_id": installation})) != 4
                or repo.conn.scalar(text("SELECT count(*) FROM nf_call_attempts")) != 0):
            raise RuntimeError("Duplicate receipt or unexpected provider spend")
    latency = sorted(r["service_ms"] for r in rows)
    return {"arm": "B" if reuse else "A", "elapsed_s": elapsed, "verified_rps": len(rows) / elapsed,
            "operation_ms": {f"p{i}": latency[math.ceil(i / 100 * len(rows)) - 1] for i in (50, 95, 99)},
            "rows": rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    with tempfile.TemporaryDirectory() as tmp, database_pair(os.environ["AW_DUAL_TEST_ADMIN_URL"]) as urls:
        db = Database(urls["nf"])
        try:
            kernel = Kernel(db, ObjectStore(Path(tmp) / "objects"))
            phases = []
            for i, reuse in enumerate((False, True, True, False)):
                directory = Path(tmp) / str(i)
                directory.mkdir()
                phases.append(phase(kernel, urls["aw"], directory, reuse))
        finally:
            db.close()
    pairs = []
    for a, b in ((phases[0], phases[1]), (phases[3], phases[2])):
        throughput = b["verified_rps"] / a["verified_rps"]
        ratios = {p: b["operation_ms"][p] / a["operation_ms"][p] for p in ("p95", "p99")}
        pairs.append(dict(rps_ratio=throughput, tail_ratios=ratios, passed=throughput >= 1.1 and max(ratios.values()) <= 1.1))
    out = dict(phases=phases, pairs=pairs, source_commit=commit, commercial_attempts=0,
               python=platform.python_version(), platform=platform.platform(), sdk=version("mcp"),
               affinity=sorted(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None,
               source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    args.output.write_text(json.dumps(out, indent=2) + "\n")
    print(json.dumps(pairs))


if __name__ == "__main__":
    main()
