"""Exact draft delivery and independent review through two real stdio bindings."""
import asyncio
from datetime import UTC, datetime, timedelta
import os
from pathlib import Path

import pytest

from harness_review_helpers import review_assignment
from test_p5_mcp import config, error, exercise
from test_review_independence import enroll_native_case

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def review_ports(tmp_path, env, world, reviewer, scope):
    port = config(tmp_path / "reader", env, reviewer.private_token, {**scope, "worker_id": reviewer.worker_id})
    controller = config(tmp_path / "controller", env, world["token"],
                        {"mode": "creative_controller", **{k: world["world_ref"][k] for k in ("work_id", "branch_id")}})
    service_env = {key: value for key, value in os.environ.items() if key in {
        "NF_CREATIVE_HARNESS_CONFIG", "NF_REVIEW_NATIVE_ATTESTATIONS", "NF_REVIEW_NATIVE_KEY",
    }}
    service_env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
    port.env = dict(service_env)
    controller.env = dict(service_env)
    return port, controller


def test_stdio_reader_has_frozen_coverage_and_cannot_issue_independence(kernel_env, tmp_path, review_assignment):
    world, first, _, reviewer, scope = review_assignment
    port, _ = review_ports(tmp_path, kernel_env, world, reviewer, scope)

    async def read(session):
        names = {t.name for t in (await session.list_tools()).tools}
        assert {"nf_harness_review_read", "nf_harness_review_submit", "nf_harness_review_withdraw"} <= names
        assert "nf_harness_review_decide" not in names and "nf_harness_review_independence" not in names
        await error(session, "nf_harness_review_independence", {}, "VIEW_DENIED")
        args = {"draft_ref": first["draft_ref"], "idempotency_key": "delivered"}
        result = await session.call_tool("nf_harness_review_read", args)
        assert not result.isError, result.content
        assert result.structuredContent["text"] == first["text"]
        again = await session.call_tool("nf_harness_review_read", args)
        assert again.structuredContent["coverage_ref"] == result.structuredContent["coverage_ref"]
    asyncio.run(exercise(port, read))


def test_separate_ports_recheck_independence_on_report_and_editorial_action(
        kernel_env, tmp_path, monkeypatch, review_assignment):
    kernel = kernel_env["kernel"]
    world, first, editorial, reviewer, scope = review_assignment
    case = enroll_native_case(kernel, tmp_path, monkeypatch, first["draft_ref"], scope["task_id"],
                              scope["generation"], controller=world["principal"])
    quota = {"currency": "USD", "max_amount": "0.10", "max_tokens": 10000, "max_attempts": 1,
             "budget_id": kernel.tasks.create_budget(world["world_ref"]["work_id"], "1"),
             "deadline": (datetime.now(UTC) + timedelta(minutes=4)).isoformat()}
    reader_port, controller_port = review_ports(tmp_path, kernel_env, world, reviewer, scope)

    async def issue(session):
        names = {t.name for t in (await session.list_tools()).tools}
        assert "nf_harness_review_independence" in names and "nf_harness_review_submit" not in names
        args = {**scope, "manuscript_ref": first["draft_ref"]}
        proof = await session.call_tool("nf_harness_review_independence", args)
        assert not proof.isError, proof.content
        return proof.structuredContent["independence_ref"]

    proof_ref = asyncio.run(exercise(controller_port, issue))

    async def submit(session):
        reading = await session.call_tool("nf_harness_review_read", {"draft_ref": first["draft_ref"],
                                                                     "idempotency_key": "s3-read"})
        assert not reading.isError, reading.content
        args = {"binding": editorial, "scope_draft_refs": [first["draft_ref"]], "target_ref": first["draft_ref"],
                "coverage_refs": [reading.structuredContent["coverage_ref"]], "independence_ref": proof_ref,
                "quota": quota, "rework_limit": 1, "layer": "PROSE", "observations": [], "idempotency_key": "report"}
        report = await session.call_tool("nf_harness_review_submit", args)
        assert not report.isError, report.content
        body = report.structuredContent
        assert body["report"]["target_ref"] == first["draft_ref"] and body["findings"] == []
        assert body["report"]["evaluation"]["literary_conclusion"] == "NOT_EVALUATED"
        replay = await session.call_tool("nf_harness_review_submit", args)
        assert replay.structuredContent == body
        return body["report_ref"]

    report_ref = asyncio.run(exercise(reader_port, submit))

    async def decide(session):
        args = {**scope, "report_ref": report_ref, "reason": "等待独立读者对章节调度的意见。",
                "action": "DEFER", "idempotency_key": "pause"}
        outcome = await session.call_tool("nf_harness_review_decide", args)
        assert not outcome.isError and outcome.structuredContent["action"] == "DEFER", outcome.content
        case.unlink()
        await error(session, "nf_harness_review_decide", args, "INDEPENDENT_REVIEW_REQUIRED")
        await error(session, "nf_harness_review_decide", {**args, "action": "WITHDRAW", "idempotency_key": "withdraw"},
                    "INDEPENDENT_REVIEW_REQUIRED")

    asyncio.run(exercise(controller_port, decide))
