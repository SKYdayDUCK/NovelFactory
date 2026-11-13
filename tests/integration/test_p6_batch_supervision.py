"""Three real PostgreSQL supervisors reach a loopback HTTP fixture concurrently."""
import importlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event, Lock

import pytest
from sqlalchemy import text
from test_p5_supervisor import backend

from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.domain.errors import DomainError
from novel_factory.infrastructure.private_files import prepare_private_directory

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def test_three_isolated_workers_dispatch_and_recover_original_enrollment(kernel_env, tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    batch = importlib.import_module("p6_batch")
    root = tmp_path / "batch"
    prepare_private_directory(root)
    books = {key: json.loads((batch.ROOT / "examples/p6" / name / "inspiration.json").read_bytes())
             for key, name in batch.BOOKS.items()}
    limits = batch.limits({"total_tokens": 100000, "token_reservation": 50000,
                           "total_amount": "1", "reserve_amount": ".2"})
    budgets = {k: {"preparation": limits, "writing": limits} for k in books}
    monkeypatch.setattr(batch, "source_release", lambda: "f" * 40)
    kernel = kernel_env["kernel"]
    with backend() as (writer, calls, _), backend(concurrent=True) as (reader, review_calls, review_server):
        reader.model = "fixture-internal-review"
        routes = {batch.ROUTES["creative"]: writer, batch.ROUTES["review"]: reader}
        def gateway():
            return AgentGateway(kernel, routes, creative_harness=batch.HARNESS)
        with kernel.db.uow() as repo:
            # Only this module's disposable cluster. The installation ships with two slots.
            repo.conn.execute(text("UPDATE nf_dispatch_limits SET slots=3 WHERE scope IN ('GLOBAL','ROLE')"))
        ids = batch.freeze(kernel, gateway(), root, books, budgets)
        assert len({p["task_id"] for p in ids.values()}) == 3 and not calls and not review_calls
        assert batch.freeze(kernel, gateway(), root, books, budgets) == ids
        state = batch.private(root / "batch.json")
        barrier, stopped, lock = Barrier(3, timeout=15), Event(), Lock()
        entered = []
        def concurrent_request(context):
            with lock:
                entered.append(context["world_ref"]["work_id"])
            try:
                barrier.wait()
            finally:
                stopped.set()
            return {}  # Deliberately malformed: no manuscript or simulated literary success.
        # Initial world design is complex planning and uses the review route.
        review_server.context_candidate = concurrent_request
        with ThreadPoolExecutor(max_workers=3) as pool:
            workers = [pool.submit(batch.serve, kernel, gateway(), root, key, stopped) for key in books]
            assert [future.result(timeout=40) for future in workers] == [0, 0, 0]
        assert not calls and len(review_calls) == 3 and set(entered) == {p["work_id"] for p in ids.values()}
        assert all(batch.private(root / key / "preparation.json")["world_ref"]["work_id"] == ids[key]["work_id"]
                   for key in books)
        ledger_ids, contexts = set(), set()
        with kernel.db.uow(write=False) as repo:
            for key in books:
                job = repo.one("tasks", {"task_id": ids[key]["task_id"]})
                assert job["work_id"] == ids[key]["work_id"] and job["context_bundle_id"]
                attempts = repo.conn.execute(text("SELECT budget_id FROM nf_call_attempts WHERE task_id=:task"),
                                             {"task": job["task_id"]}).scalars().all()
                assert len(attempts) == 1
                ledger_ids.add(attempts[0])
                contexts.add(job["context_bundle_id"])
        assert len(ledger_ids) == len(contexts) == 3
        assert batch.freeze(kernel, gateway(), root, books, budgets) == ids
        assert not calls and len(review_calls) == 3
        changed = {**state["allowances"], "A": {**budgets["A"], "writing": {**limits, "total_tokens": 120000}}}
        with pytest.raises(DomainError, match="changed"):
            batch.freeze(kernel, gateway(), root, books, changed)
