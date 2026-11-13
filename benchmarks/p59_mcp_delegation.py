"""Compare the actual cold and batch transports on two authorized Workshop installations."""
import argparse
import asyncio
from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta
import hashlib
from importlib.metadata import version
import json
from math import ceil
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
from time import perf_counter
from uuid import uuid4

from mcp import StdioServerParameters
from sqlalchemy import text

from agent_workshop.auth import Denied
from agent_workshop.reconcile import batch_remote, remote
from benchmarks.p2_behavioral_load import isolated_kernel
from benchmarks.p59_workflow_load import FixtureAdapter
from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.delegation import Delegations, install, provision_slot, revoke
from novel_factory.application.runtime_tasks import RuntimeTaskService
from novel_factory.application.supervisor import RuntimeSupervisor
from novel_factory.fixtures import load_fixture, point
from novel_factory.infrastructure.private_files import create_private_file


def provision(kernel, directory, index):
    world = load_fixture(kernel)
    owner, wr = world["principal"], world["world_ref"]
    instance = str(uuid4())
    ticket = install(kernel, owner, nf_instance_id=instance, org_id=str(uuid4()),
                     work_id=wr["work_id"], branch_id=wr["branch_id"],
                     expires_at=datetime.now(UTC) + timedelta(hours=1))
    installation = ticket["installation_id"]
    gateway = AgentGateway(kernel, {"fixture": FixtureAdapter()})
    frozen = RuntimeTaskService(kernel, gateway).define(
        owner, wr, role_id="evidence_maintainer", task_kind="summary", story_time=point(1),
        subject_ids=[world["ids"]["sword"]], predicates=["owner"], assignment="Summarize the visible evidence.",
        adapter_id="fixture", max_amount="0.10", token_budget=100000,
    )
    budget = kernel.tasks.create_budget(wr["work_id"], "2")
    provision_slot(RuntimeSupervisor(kernel, gateway), owner, installation, scope_ref="summary.1",
                   task_template_id="summary.v1", frozen=frozen, budget_id=budget, reserve_amount="0.10",
                   token_reservation=100000, accept_summary=True)
    get = dict(protocol_version="aw-nf.v1", nf_instance_id=instance, work_id=wr["work_id"],
               branch_id=wr["branch_id"], operation_id=str(uuid4()), grant_epoch=ticket["grant_epoch"])
    submit = dict(get, task_template_id="summary.v1", scope_ref="summary.1", actor_binding_ref=None,
                  role_id="evidence_maintainer", generation=0, budget_reservation_ref=str(uuid4()),
                  max_amount="0.100000", currency="USD", token_limit=100000)
    binding = dict(installation_id=installation, token=ticket["token"], object_root=str(kernel.objects.root),
                   database_url=kernel.db.engine.url.render_as_string(hide_password=False))
    path = directory / f"installation-{index}.json"
    with os.fdopen(create_private_file(path), "w", encoding="utf-8") as stream:
        json.dump(binding, stream)
    params = StdioServerParameters(command=sys.executable, args=[
        "-m", "novel_factory.interfaces.delegation_stdio", "--binding", str(path)])
    receipt = Delegations(kernel, installation, ticket["token"]).call("nf_delegation_submit", submit)
    return params, get, receipt, owner, installation


async def phase(peers, reuse):
    started = perf_counter()
    rows = []

    async def worker(index, peer):
        params, get, receipt, _, _ = peer
        async with AsyncExitStack() as stack:
            invoke = await stack.enter_async_context(batch_remote(params)) if reuse else remote(params)
            for sample in range(4):
                sent = perf_counter()
                result = await invoke("nf_delegation_get", get)
                ended = perf_counter()
                if result != receipt:
                    raise RuntimeError("Incorrect installation receipt")
                rows.append({"book": index, "sample": sample, "sent_s": sent - started,
                             "reply_s": ended - started, "call_ms": (ended - sent) * 1000, "status": "OK"})

    await asyncio.gather(*(worker(index, peer) for index, peer in enumerate(peers)))
    elapsed = perf_counter() - started
    latencies = sorted(row["call_ms"] for row in rows)
    return {"arm": "B" if reuse else "A", "elapsed_s": elapsed, "verified_rps": len(rows) / elapsed,
            "child_launches_by_construction": 2 if reuse else len(rows),
            "call_ms": {f"p{p}": latencies[ceil(len(rows) * p / 100) - 1] for p in (50, 95, 99)},
            "requests": sorted(rows, key=lambda row: row["sent_s"])}


async def exercise(kernel, peers):
    phases = []
    for reuse in (False, True, True, False):
        phases.append(await phase(peers, reuse))
    params, get, expected, owner, installation = peers[0]
    async with batch_remote(params) as invoke:
        if await invoke("nf_delegation_get", get) != expected:
            raise RuntimeError("Warm binding's receipt differs")
        revoke(kernel, owner, installation)
        try:
            await invoke("nf_delegation_get", get)
        except Denied as denied:
            if str(denied) != "VIEW_DENIED":
                raise
        else:
            raise RuntimeError("Revoked grant retained access")
    pairs = []
    for a, b in ((phases[0], phases[1]), (phases[3], phases[2])):
        throughput = b["verified_rps"] / a["verified_rps"]
        tails = {p: b["call_ms"][p] / a["call_ms"][p] for p in ("p95", "p99")}
        pairs.append({"verified_rps_ratio": throughput, "latency_ratios": tails,
                      "passes": throughput >= 1.1 and max(tails.values()) <= 1.1})
    with kernel.db.uow(write=False) as repo:
        receipts = repo.conn.scalar(text("SELECT count(*) FROM nf_delegation_receipts"))
        attempts = repo.conn.scalar(text("SELECT count(*) FROM nf_call_attempts"))
        pg = repo.conn.scalar(text("SELECT version()"))
    if attempts or receipts != 2:
        raise RuntimeError("Read experiment changed durable effect or provider count")
    return {"phases": phases, "pairs": pairs, "revocation": "VIEW_DENIED", "attempts": attempts,
            "delegation_receipts": receipts, "adoption_gate": all(p["passes"] for p in pairs),
            "postgres": pg}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        with isolated_kernel(os.environ["NF_TEST_DATABASE_URL"], directory / "objects") as (kernel, _):
            peers = [provision(kernel, directory, i) for i in range(2)]
            result = asyncio.run(exercise(kernel, peers))
    result.update(source_commit=commit, python=platform.python_version(), mcp_sdk=version("mcp"),
                  affinity=sorted(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None,
                  driver_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                  transport_sha256=hashlib.sha256((root / "workshop/src/agent_workshop/reconcile.py").read_bytes()).hexdigest())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"pairs": result["pairs"], "gate": result["adoption_gate"]}))


if __name__ == "__main__":
    main()
