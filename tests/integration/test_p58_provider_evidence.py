"""A local HTTP fixture exercises a real NF attempt and authorized operator export."""

import pytest
from test_p5_supervisor import _job, backend

from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.provider_evidence import inspect, reconcile_invoice, summarize
from novel_factory.application.supervisor import RuntimeSupervisor
from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import load_fixture


@pytest.mark.postgres
def test_evidence_export_uses_authoritative_scope_and_actual_response(kernel_env):
    k = kernel_env["kernel"]
    w, other = load_fixture(k), load_fixture(k)
    wr = w["world_ref"]
    budget = k.tasks.create_budget(wr["work_id"], "1")
    with backend() as (fixture, requests, _):
        gw = AgentGateway(k, {"test": fixture})
        sup = RuntimeSupervisor(k, gw)
        job = _job(k, gw, sup, w, budget, "READY")
        sup.run_once(w["principal"], wr["work_id"], wr["branch_id"])
    with k.db.uow(write=False) as repo:
        (call,) = repo.many("call_attempts", {"task_id": job.task["task_id"]})
    ids = [str(call["attempt_id"])]
    report = inspect(k, w["principal"], wr["work_id"], wr["branch_id"], ids)
    (row,) = report["attempts"]
    assert row["request_hash"] == call["request_hash"] and row["response_sha256"]
    assert row["budget_id"] == budget and len(requests) == 1
    assert report["dual_provider_overlap"] == "NOT_DEMONSTRATED"
    invoice = dict(
        receipt_id=row["receipt_id"],
        currency=row["currency"],
        amount="0.010000",
        tokens=18,
        document_sha256="1" * 64,
    )
    with pytest.raises(DomainError):
        reconcile_invoice(
            k, w["principal"], wr["work_id"], wr["branch_id"], ids[0], **{**invoice, "tokens": 17}
        )
    settled = reconcile_invoice(
        k, w["principal"], wr["work_id"], wr["branch_id"], ids[0], **invoice
    )
    assert settled["state"] == "SUCCEEDED"
    assert (
        reconcile_invoice(k, w["principal"], wr["work_id"], wr["branch_id"], ids[0], **invoice)
        == settled
    )
    with pytest.raises(DomainError):
        reconcile_invoice(
            k,
            w["principal"],
            wr["work_id"],
            wr["branch_id"],
            ids[0],
            **{**invoice, "amount": "0.020000"},
        )
    assert (
        inspect(k, w["principal"], wr["work_id"], wr["branch_id"], ids)["attempts"][0][
            "settled_amount"
        ]
        == invoice["amount"]
    )
    with pytest.raises(DomainError):
        inspect(
            k,
            other["principal"],
            other["world_ref"]["work_id"],
            other["world_ref"]["branch_id"],
            ids,
        )


def test_overlap_uses_transport_timestamps_and_distinct_roles():
    row = {
        "role_id": "chapter_writer",
        "attempt_id": "a",
        "adapter": "deepseek",
        "provider": "https://api.deepseek.com",
        "execution_kind": "REAL_EXTERNAL",
        "receipt_id": "test-response",
        "response_sha256": "test-hash",
        "transport_window": {
            "started_at": "2026-01-01T12:00:00Z",
            "received_at": "2026-01-01T12:00:05Z",
        },
    }
    second = {
        **row,
        "adapter": "codex-cli",
        "provider": "codex-cli",
        "execution_kind": "REAL_HOST",
        "role_id": "reviewer",
        "attempt_id": "b",
    }
    assert summarize([row, second])["overlapping_real_transports"][0]["overlap_seconds"] == 5
    for change in (
        {"role_id": "chapter_writer"},
        {"receipt_id": None},
        {"execution_kind": "TEST_FIXTURE"},
        {
            "transport_window": {
                "started_at": "2026-01-01T12:00:05Z",
                "received_at": "2026-01-01T12:00:07Z",
            }
        },
    ):
        assert summarize([row, {**second, **change}])["dual_provider_overlap"] == "NOT_DEMONSTRATED"
