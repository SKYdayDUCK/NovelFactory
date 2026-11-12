"""Weighted admission over persistent organizations and works with live eligible queues."""
from collections import Counter

import pytest
from sqlalchemy import text

from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.model_routing import activate
from novel_factory.application.supervisor import RuntimeSupervisor
from novel_factory.fixtures import load_fixture
from novel_factory.orchestration import dispatch
from test_p5_supervisor import _job, backend
from test_p58_routing import policy

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def test_weighted_organizations_and_books_keep_their_share_after_restart(kernel_env):
    kernel = kernel_env["kernel"]
    with backend() as (adapter, requests, _):
        gateway = AgentGateway(kernel, {"one": adapter})
        supervisor = RuntimeSupervisor(kernel, gateway)
        scopes = []
        weights = [1, 2, 1, 2]
        for index, weight in enumerate(weights):
            book = load_fixture(kernel)
            owner, wr = book["principal"], book["world_ref"]
            route = {**policy(book), "org_id": f"group-{index // 2}"}
            activate(kernel, owner, wr, route, gateway.adapters, expected_revision=0)
            budget = kernel.tasks.create_budget(wr["work_id"], "5")
            for _ in range(9):
                _job(kernel, gateway, supervisor, book, budget, "READY", adapter_id="@policy")
            scopes.append((owner, wr["work_id"], wr["branch_id"]))
            with kernel.db.uow() as repo:
                repo.conn.execute(text("INSERT INTO nf_runtime_work_share(work_id,weight) VALUES (:w,:weight)"),
                                  {"w": wr["work_id"], "weight": weight})
        with kernel.db.uow() as repo:
            repo.conn.execute(text("INSERT INTO nf_runtime_org_share(organization,weight) VALUES "
                                   "('local/group-0',1),('local/group-1',2)"))
        wins = Counter()
        order = []
        for round_index in range(18):
            if round_index == 9:
                supervisor = RuntimeSupervisor(kernel, gateway)
            # Register all four live demand tickets before measuring priority. next_job
            # alone never claims or consumes a slot. The supervisor performs the claim.
            for _, work, branch in scopes:
                with kernel.db.uow() as repo:
                    dispatch.next_job(repo.conn, work, branch)
            for index, scope in enumerate(scopes):
                result = supervisor.admit_model(*scope)
                if result["status"] == "RESULT_STORED":
                    wins[index] += 1
                    order.append(index)
                    break
                assert result["status"] == "IDLE"
            else:
                pytest.fail("No eligible book made progress")
        assert [wins[i] for i in range(4)] == [2, 4, 4, 8], order
        assert len(requests) == 18
