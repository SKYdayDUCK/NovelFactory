"""Observe NF and AW across the original W5 physical test's manager downtime."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def trace_offline_advance(request, monkeypatch):
    if request.node.name != "test_physical_pitr_both_directions":
        yield
        return
    module = request.module
    original_tick = module.tick
    original_run = module.RuntimeSupervisor.run_once
    state = {}
    ticks = []

    def aw_snapshot(dsn, operation):
        with module.connect(dsn) as connection:
            row = connection.execute(
                """SELECT d.state AS delegation, r.task_id::text, r.receipt_ref::text,
                          r.task_state, r.accounting_state, b.status AS budget_hold,
                          (SELECT count(*) FROM aw_cost_observations o
                           WHERE o.operation_id = d.operation_id) AS attempts_observed
                   FROM aw_delegations d JOIN aw_remote_receipts r USING(org_id,operation_id)
                   JOIN aw_budget_holds b USING(org_id,operation_id) WHERE d.operation_id=%s""",
                (operation,),
            ).fetchone()
            return row

    def nf_snapshot(supervisor, task):
        with supervisor.kernel.db.uow(write=False) as repo:
            return {"state": repo.one("tasks", {"task_id": task})["state"],
                    "attempts": [str(a["attempt_id"]) for a in repo.many("call_attempts", {"task_id": task})]}

    def tick(dsn, installation, org, operation, transport):
        status = original_tick(dsn, installation, org, operation, transport)
        if not state and status == "BOUND":
            state.update(aw=dsn, operation=operation, initial=aw_snapshot(dsn, operation))
        elif dsn == state.get("aw") and ticks and "reconnected" not in state:
            state["reconnected"] = aw_snapshot(dsn, operation)
        return status

    def run_once(supervisor, *args, **kwargs):
        if not state or "reconnected" in state:
            return original_run(supervisor, *args, **kwargs)
        task = state["initial"]["task_id"]
        before = nf_snapshot(supervisor, task)
        aw_before = aw_snapshot(state["aw"], state["operation"])
        result = original_run(supervisor, *args, **kwargs)
        aw_after = aw_snapshot(state["aw"], state["operation"])
        assert aw_before == aw_after == state["initial"]
        ticks.append({"before": before, "after": nf_snapshot(supervisor, task), "status": result["status"]})
        return result

    monkeypatch.setattr(module, "tick", tick)
    monkeypatch.setattr(module.RuntimeSupervisor, "run_once", run_once)
    yield
    assert ticks and state.get("reconnected")
    start, nf_end = ticks[0]["before"], ticks[-1]["after"]
    initial, reconnected = state["initial"], state["reconnected"]
    assert start["state"] != "ACCEPTED" and not start["attempts"]
    assert nf_end["state"] == "ACCEPTED" and len(nf_end["attempts"]) == 1
    assert initial["task_state"] != "ACCEPTED" and reconnected["task_state"] == "ACCEPTED"
    assert initial["receipt_ref"] == reconnected["receipt_ref"]
    assert initial["task_id"] == reconnected["task_id"]
    assert reconnected["attempts_observed"] == 1 and reconnected["budget_hold"] == "HELD"
    receipt = {"status": "PASS", "recorded_utc": datetime.now(timezone.utc).isoformat(),
               "scope": "No Workshop worker runs between initial NF binding and original-key reconciliation. NF runs two actual supervisor scans.",
               "operation_id": state["operation"], "aw_unchanged_while_offline": initial,
               "nf_supervisor_scans": ticks, "aw_after_reconciliation": reconnected,
               "production_provider_calls": 0}
    Path(os.environ["AW_OFFLINE_WITNESS"]).write_text(json.dumps(receipt, indent=2)+"\n")
