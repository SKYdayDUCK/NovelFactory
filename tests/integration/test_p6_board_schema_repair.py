"""A current writer's rejected result is evidence for a new, separately accounted technical task."""
import json
from dataclasses import replace
from decimal import Decimal

import pytest

from novel_factory.application.production_pipeline import pipeline_status
from novel_factory.application.rolling_production import register_rolling
from novel_factory.application.supervisor import RuntimeSupervisor
from test_p5_supervisor import backend
from test_p6_rolling_production import answer, connect, request
from test_release import setup_batch

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


@pytest.mark.parametrize("outcome", ["recover", "repeat", "unknown"])
def test_quality_board_recovers_invalid_payload_or_fences_unknown(kernel_env, outcome):
    kernel = kernel_env["kernel"]
    fixture = setup_batch(kernel, count=1)
    wr, owner = fixture["world_ref"], fixture["principal"]
    work, branch = wr["work_id"], wr["branch_id"]
    args = request(kernel, fixture, 1)
    args["profile"]["overrides"]["production_mode"] = "quality"
    with backend() as (adapter, calls, server):
        server.packet_candidate = lambda packet: {} if len(calls) == 1 or outcome != "recover" else answer(packet)
        generate = adapter.generate
        if outcome != "unknown":
            adapter.generate = lambda packet: replace(generate(packet), actual_amount=Decimal(0), currency="USD")
        gateway = connect(kernel, adapter)
        register_rolling(kernel, gateway, owner, **args)
        for _ in range(22):
            RuntimeSupervisor(kernel, gateway).run_once(owner, work, branch)
            board = pipeline_status(kernel, owner, work, branch)
            node = next(n for n in board["nodes"].values() if n["stage"] == "draft")
            if node["state"] in {"DRAFT_STORED", "STOPPED"}:
                break
        if outcome == "unknown":
            assert node["state"] == "STOPPED" and node["reason"] == "CALL_RECONCILIATION_REQUIRED"
            assert len(calls) == 1 and not node.get("schema_repairs")
            return
        history = node["schema_repairs"]
        assert len({r["attempt_id"] for r in history}) == len(history)
        assert all(r["diagnostic"]["keyword"] == "required" and r["reflection_ref"] for r in history)
        assert history[0]["method"] in json.loads(calls[1]["messages"][1]["content"])["assignment"]
        if outcome == "recover":
            assert node["state"] == "DRAFT_STORED" and len(calls) == 2 and len(history) == 1
            assert history[-1]["actual_fix"] == node["output_ref"]
        else:
            assert node["state"] == "STOPPED" and node["reason"] == "STRATEGY_REASSESSMENT_REQUIRED"
            assert len(calls) == 3 and len(history) == 3
            assert history[0]["method"] != history[1]["method"]


@pytest.mark.parametrize("wire", [True, False])
def test_postpaid_board_separates_returned_bytes_from_unknown_charge(kernel_env, monkeypatch, wire):
    from agent_runtime import providers
    from agent_runtime._http_worker import _persist
    from test_p6_postpaid_delivery import fake_transport

    kernel = kernel_env["kernel"]
    fixture = setup_batch(kernel, count=1)
    wr, owner = fixture["world_ref"], fixture["principal"]
    work, branch = wr["work_id"], wr["branch_id"]
    args = request(kernel, fixture, 1)
    args["profile"]["overrides"]["production_mode"] = "quality"
    adapter, calls = fake_transport(kernel, monkeypatch, "defer", lambda packet: {} if len(calls) == 1 else answer(packet))
    post = providers.post

    def returned(**kwargs):
        response, receipt, window = post(**kwargs)
        if wire:
            _persist(kwargs, 200, response, True, receipt, window)
        return response, receipt, window

    monkeypatch.setattr(providers, "post", returned)
    gateway = connect(kernel, adapter)
    register_rolling(kernel, gateway, owner, **args)
    for _ in range(18):
        RuntimeSupervisor(kernel, gateway).run_once(owner, work, branch)
        board = pipeline_status(kernel, owner, work, branch)
        node = next(n for n in board["nodes"].values() if n["stage"] == "draft")
        if node["state"] in {"STOPPED", "DRAFT_STORED"}:
            break
    if not wire:
        assert node["reason"] == "CALL_RECONCILIATION_REQUIRED" and len(calls) == 1
        return
    assert len(calls) == 2 and len(set(calls)) == 2
    repair = node["schema_repairs"][0]
    with kernel.db.uow(write=False) as repo:
        source = repo.one("call_attempts", {"attempt_id": repair["attempt_id"]})
        assert source["state"] == "UNKNOWN" and source["actual_amount"] is None
    assert node["state"] == "DRAFT_STORED" and repair["actual_fix"] == node["output_ref"]
