"""Observe pre-existing charges and UNKNOWN reservations across the real 0017 upgrade."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from novel_factory.application.authorization import Principal
from novel_factory.application.budget_limits import SharedBudgetService
from novel_factory.domain.errors import DomainError
from tests.fixtures.historical_release import run_release

pytestmark = [pytest.mark.integration, pytest.mark.postgres]

# Parent of the migration introducing shared token limits, with 0017 as its head.
RELEASE_0017 = "74350760dfd063f3596e5b6838a56a40d3006ad6"


def test_install_limit_on_populated_0017(kernel_env, tmp_path):
    root = Path(__file__).resolve().parents[2]
    env = {**os.environ, "NF_DATABASE_URL": kernel_env["url"]}

    def migrate(*args):
        subprocess.run([sys.executable, "-m", "alembic", *args], check=True, cwd=root, env=env)

    migrate("downgrade", "0017_nf_workshop_delegation")
    old = run_release(root, tmp_path, RELEASE_0017, """
import json
import os
from sqlalchemy import text
from novel_factory.fixtures import create_bound_task, load_fixture, point
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.objects import ObjectStore

db = Database()
try:
    with db.uow(write=False) as repo:
        assert repo.conn.scalar(text("SELECT version_num FROM alembic_version")) == "0017_nf_workshop_delegation"
    kernel = Kernel(db, ObjectStore(os.environ["NF_OBJECT_ROOT"]))
    world = load_fixture(kernel)
    wr, owner = world["world_ref"], world["principal"]
    budget = kernel.tasks.create_budget(wr["work_id"], "1")
    task = create_bound_task(kernel, owner, wr, point(1))
    def sent(key):
        call = kernel.tasks.reserve_call(
            task["task_id"], task["generation"], task["worker_id"], budget_id=budget,
            amount="0.1", call_key=key, request_hash="a" * 64, token_amount=10,
        )
        assert kernel.tasks.mark_call_sent(call["attempt_id"], worker_id=task["worker_id"])
        return call["attempt_id"]
    kernel.tasks.settle_call(
        sent("finished"), state="SUCCEEDED", actual_amount=".02", actual_tokens=6, provider_receipt="test-invoice",
    )
    kernel.tasks.settle_call(sent("unconfirmed"), state="UNKNOWN")
    print(json.dumps({"work": wr["work_id"], "branch": wr["branch_id"], "budget": budget,
                      "grant": owner.grant_id, "principal": owner.principal_id,
                      "task": task["task_id"], "generation": task["generation"], "worker": task["worker_id"]}))
finally:
    db.close()
""", env)
    migrate("upgrade", "head")
    kernel = kernel_env["kernel"]
    service = SharedBudgetService(kernel)
    scope = (Principal(old["grant"], old["principal"]), old["work"], old["branch"], old["budget"])
    view = service.status(*scope)
    assert (
        view["spent_tokens"] == 6 and view["reserved_tokens"] == 10 and view["total_tokens"] is None
    )
    with pytest.raises(DomainError) as low:
        service.set_limit(*scope, 15)
    assert low.value.code == "BUDGET_EXCEEDED"
    assert service.set_limit(*scope, 16)["available_tokens"] == 0
    with pytest.raises(DomainError) as exhausted:
        kernel.tasks.reserve_call(
            old["task"], old["generation"], old["worker"], budget_id=old["budget"],
            amount="0.1", call_key="after-upgrade", request_hash="a" * 64, token_amount=10,
        )
    assert exhausted.value.code == "BUDGET_EXCEEDED"
    with pytest.raises(subprocess.CalledProcessError):
        migrate("downgrade", "0017_nf_workshop_delegation")
    assert service.status(*scope)["reserved_tokens"] == 10
