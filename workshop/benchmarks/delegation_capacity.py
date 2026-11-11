"""Opt-in two-database capacity acceptance; pytest supplies disposable isolated credentials."""

import asyncio
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import replace
from threading import Lock
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import hashlib
import json
from math import ceil
import os
from pathlib import Path
import sys
from time import perf_counter, sleep
from uuid import uuid4

import pytest
from mcp import ClientSession
from mcp.client.stdio import stdio_client

AW = Path(__file__).resolve().parents[1]
NF = Path(os.environ["AW_NF_SOURCE"]).resolve()
sys.path.insert(0, str(AW / "integration"))
sys.path.insert(0, str(NF / "tests/integration"))
from importlib.util import module_from_spec, spec_from_file_location

spec = spec_from_file_location("aw_dual_fixtures", AW / "integration/conftest.py")
fixtures = module_from_spec(spec)
spec.loader.exec_module(fixtures)
dual_databases = fixtures.dual_databases
from test_dual_stdio import bound_process
from test_p5_supervisor import SyntheticHTTP, backend
from agent_workshop.admin import enroll
from agent_workshop.auth import Denied, connect
from agent_workshop.organization import bootstrap
from agent_workshop.provision import allow_slot
from agent_workshop.reconcile import reconcile
from agent_workshop.server import dispatch
from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.delegation import install, provision_slot
from novel_factory.application.kernel import Kernel
from novel_factory.application.runtime_tasks import RuntimeTaskService
from novel_factory.application.supervisor import RuntimeSupervisor
from novel_factory.fixtures import load_fixture, point
from novel_factory.infrastructure.capacity import AdaptiveController
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.objects import ObjectStore


def drain_paired_run(supervisor, owner, world_ref, size, policy):
    work, branch = world_ref["work_id"], world_ref["branch_id"]
    ceiling = 2
    controller = None
    if policy == "adaptive-2":
        controller = AdaptiveController(
            supervisor.kernel.db, "model", ceiling, "model", work=work, branch=branch, interval=0.5
        )
    admitted = completed = 0
    pending = {}
    targets = []
    deadline = perf_counter() + 600
    with ThreadPoolExecutor(max_workers=ceiling) as pool:
        while completed < size:
            assert perf_counter() < deadline, (policy, admitted, completed)
            target = controller.limit() if controller else ceiling
            if not targets or targets[-1]["target"] != target:
                targets.append({"admitted": admitted, "completed": completed, "target": target})
            while admitted < size and len(pending) < target:
                result = supervisor.run_once(owner, work, branch, executor=pool)
                if isinstance(result, Future):
                    pending[result] = perf_counter()
                    admitted += 1
                else:
                    assert result["status"] == "IDLE", result["status"]
                    break
            if not pending:
                sleep(0.01)
                continue
            done, _ = wait(pending, timeout=0.1, return_when=FIRST_COMPLETED)
            for future in done:
                result = future.result()
                assert result["status"] == "RESULT_STORED", result["status"]
                elapsed = perf_counter() - pending.pop(future)
                if controller:
                    controller.monitor.completed(elapsed)
                completed += 1
    return targets


@pytest.mark.parametrize("size", [10, 50, 200])
@pytest.mark.parametrize("policy", ["fixed-1", "fixed-2", "adaptive-2"])
def test_logical_delegation_backlog(dual_databases, tmp_path, size, policy):
    dsn = dual_databases["aw"]
    db = Database(dual_databases["nf"])
    kernel = Kernel(db, ObjectStore(tmp_path / "objs"))
    try:
        world = load_fixture(kernel)
        wr, owner = world["world_ref"], world["principal"]
        org, instance = str(uuid4()), str(uuid4())
        auth = install(
            kernel,
            owner,
            nf_instance_id=instance,
            org_id=org,
            work_id=wr["work_id"],
            branch_id=wr["branch_id"],
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        inst = auth["installation_id"]

        def binding(c, e):
            return {"database_url": dsn, "token": enroll(c, inst, org, e, 1, 1)}

        with connect(dsn) as c:
            c.execute("INSERT INTO aw_installations VALUES (%s,%s,true)", (inst, instance))
            bootstrap(c, org, installation_id=inst)
            c.execute(
                "INSERT INTO aw_budget_accounts VALUES (%s,%s,'USD')", (org, Decimal(".10") * size)
            )
            manager, chief = binding(c, "manager"), binding(c, "chief")
        members = []
        for i in range(size - 8):
            employee = f"maintainer-{i}"
            dispatch(
                manager,
                "aw_hire",
                dict(
                    operation_id=f"hire-{i}",
                    expected_revision=i + 1,
                    employee_id=employee,
                    role_id="evidence_maintainer",
                    supervisor_id="chief",
                    team_id="production",
                ),
            )
            members.append(employee)
        nf_budget = kernel.tasks.create_budget(wr["work_id"], str(size + 1))
        resultdir = Path(os.environ["AW_BENCH_RESULTS"])
        resultdir.mkdir(parents=True, exist_ok=True)
        with backend(concurrent=policy != "fixed-1") as (http, requests, _):
            # The HTTP fixture reports tokens but no invoice. Supply a separately
            # declared synthetic tariff; never treat missing real pricing as zero.
            class MeteredFixture(SyntheticHTTP):
                lock = Lock()
                active = peak = tokens = 0

                def generate(self, request):
                    with self.lock:
                        self.active += 1
                        self.peak = max(self.peak, self.active)
                    try:
                        if policy != "fixed-1":
                            sleep(0.05)
                        response = super().generate(request)
                        with self.lock:
                            self.tokens += response.input_tokens + response.output_tokens
                        return replace(
                            response,
                            actual_amount=Decimal("0.02"),
                            currency="USD",
                            provider_receipt="fixture-invoice-" + request.attempt_id,
                        )
                    finally:
                        with self.lock:
                            self.active -= 1

            meter = MeteredFixture(
                endpoint=http.endpoint,
                model=http.model,
                api_key="synthetic-fixture-only",
                allow_loopback_http=True,
            )
            gateway = AgentGateway(kernel, {"synthetic": meter})
            sup = RuntimeSupervisor(kernel, gateway)
            task_service = RuntimeTaskService(kernel, gateway)
            for i in range(size + 1):
                ref = f"work-{i}"
                definition = task_service.define(
                    owner,
                    wr,
                    role_id="evidence_maintainer",
                    task_kind="summary",
                    adapter_id="synthetic",
                    assignment="Summarize the authorized scene.",
                    story_time=point(1),
                    subject_ids=[world["ids"]["sword"]],
                    predicates=["owner"],
                    max_amount="0.10",
                    token_budget=100000,
                )
                provision_slot(
                    sup,
                    owner,
                    inst,
                    scope_ref=ref,
                    task_template_id="summary.v1",
                    frozen=definition,
                    budget_id=nf_budget,
                    reserve_amount="0.10",
                    token_reservation=100000,
                    accept_summary=True,
                )
                slot_policy = dict(
                    work_id=wr["work_id"],
                    branch_id=wr["branch_id"],
                    nf_instance_id=instance,
                    actor_binding_ref=None,
                    role_id="evidence_maintainer",
                    task_template_id="summary.v1",
                    scope_ref=ref,
                    generation=0,
                    grant_epoch=1,
                    max_amount="0.10",
                    currency="USD",
                    token_limit=100000,
                )
                with connect(dsn) as c:
                    allow_slot(c, org, inst, ref, members[i % len(members)], slot_policy)
            params = bound_process(
                tmp_path,
                "novel_factory.interfaces.delegation_stdio",
                dict(
                    database_url=dual_databases["nf"],
                    object_root=str(tmp_path / "objs"),
                    installation_id=inst,
                    token=auth["token"],
                ),
                NF / "src",
            )

            async def run():
                async with stdio_client(params) as streams, ClientSession(*streams) as client:
                    await client.initialize()

                    async def invoke(name, request):
                        r = await client.call_tool(name, request)
                        if r.isError:
                            raise Denied(r.structuredContent["error"]["code"])
                        return r.structuredContent

                    starts = {}
                    latencies = []
                    t0 = perf_counter()
                    for i in range(size + 1):
                        args = dict(
                            operation_id=f"d-{i}",
                            slot_id=f"work-{i}",
                            max_amount="0.10",
                            token_limit=100000,
                        )
                        if i == size:
                            with pytest.raises(Denied, match="BUDGET_EXCEEDED"):
                                dispatch(chief, "aw_delegate", args)
                            break
                        begun = perf_counter()
                        op = dispatch(chief, "aw_delegate", args)["operation_id"]
                        assert dispatch(chief, "aw_delegate", args)["operation_id"] == op
                        starts[op] = begun
                    for op in starts:
                        assert await reconcile(dsn, inst, org, op, invoke) == "BOUND"
                    if policy == "fixed-1":
                        targets = [{"admitted": 0, "completed": 0, "target": 1}]
                        for _ in starts:
                            r = sup.run_once(owner, wr["work_id"], wr["branch_id"])
                            assert r["status"] == "RESULT_STORED", r.get("status")
                    else:
                        targets = drain_paired_run(sup, owner, wr, size, policy)
                    assert sup.run_once(owner, wr["work_id"], wr["branch_id"])["status"] == "IDLE"
                    for op in starts:
                        # Respect the worker's eligibility time even at the smallest tier.
                        for _ in range(10):
                            status = await reconcile(dsn, inst, org, op, invoke)
                            if status != "IDLE":
                                break
                            await asyncio.sleep(0.25)
                        assert status == "SETTLED", status
                        latencies.append((perf_counter() - starts[op]) * 1000)
                    return sorted(latencies), perf_counter() - t0, targets

            ms, elapsed, targets = asyncio.run(asyncio.wait_for(run(), timeout=600))
            with connect(dsn) as c:
                counts = c.execute(
                    """SELECT count(*) AS tasks,count(DISTINCT task_id) AS unique_tasks,
                    count(*) FILTER (WHERE task_state='ACCEPTED') AS accepted
                    FROM aw_remote_receipts WHERE org_id=%s""",
                    (org,),
                ).fetchone()
                holds = c.execute(
                    """SELECT count(*) FILTER(WHERE status='HELD') AS held,count(*) FILTER(WHERE status='SETTLED') AS settled
                    FROM aw_budget_holds WHERE org_id=%s""",
                    (org,),
                ).fetchone()
                roster = c.execute(
                    "SELECT count(*) AS n FROM aw_employees WHERE org_id=%s AND status='ACTIVE'",
                    (org,),
                ).fetchone()["n"]
            ceiling = 1 if policy == "fixed-1" else 2
            contract = "delegation-contract.json" if ceiling == 1 else "admission-comparison.json"
            data = dict(
                registered_members=roster,
                delegations=size,
                elapsed_s=elapsed,
                logical_tasks_per_s=size / elapsed,
                **counts,
                **holds,
                provider="local HTTP synthetic",
                provider_requests=len(requests),
                concurrency_policy=ceiling,
                policy=policy,
                admission_targets=targets,
                model_calls_while_idle=len(requests) - size,
                observed_peak_model_calls=meter.peak,
                synthetic_reported_tokens=meter.tokens,
                synthetic_tariff="0.02 USD per fixture call",
                extra_budget_authorizations=0,
                latency_ms={f"p{q}": ms[ceil(len(ms) * q / 100) - 1] for q in (50, 95, 99)},
                per_operation_ms=ms,
                contract_sha256=hashlib.sha256(
                    (AW / "benchmarks" / contract).read_bytes()
                ).hexdigest(),
            )
            suffix = "" if ceiling == 1 else "-" + policy
            (resultdir / f"delegations-{size}{suffix}.json").write_text(json.dumps(data, indent=2) + "\n")
            assert (
                roster
                == counts["tasks"]
                == counts["unique_tasks"]
                == counts["accepted"]
                == holds["settled"]
                == len(requests)
                == size
            )
            assert holds["held"] == 0 and 1 <= meter.peak <= ceiling
            assert elapsed <= 600 and all(1 <= row["target"] <= ceiling for row in targets)
    finally:
        db.close()
