"""Private evidence projections retain failed/unknown work and never cross branch or work grants."""

import io
import json
import os
from dataclasses import replace
from hashlib import sha256

import pytest
from test_agent_gateway import FixtureAdapter, setup_gateway
from test_p6_export_release import binding, run_cli
from test_p6_export_release import volume as frozen_volume  # noqa: F401

from novel_factory.application.audit_log import record
from novel_factory.application.bound_tools import invoke_bound_tool
from novel_factory.application.mcp_runtime import McpRuntime, TaskBinding
from novel_factory.application.runtime_evidence import export_runtime_evidence
from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import load_fixture
from novel_factory.infrastructure.private_files import read_private_bytes

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


class PrivateFixture(FixtureAdapter):
    @property
    def configuration(self):
        return {
            **super().configuration,
            "provider": "https://test-provider.invalid/private-project",
            "endpoint": "https://test-provider.invalid/private-project/model",
        }

    def generate(self, request):
        return replace(
            super().generate(request),
            provider_receipt="private-receipt-sentinel",
            raw_response={"fixture": True, "private": "private-provider-response-sentinel"},
        )


def _rows(data):
    return [json.loads(line) for line in data.splitlines()]


@pytest.mark.parametrize("known", [True, False])
def test_runtime_export_roles_skills_calls_and_ledger_state(kernel_env, tmp_path, known):
    adapter = PrivateFixture()
    if not known:
        adapter.amount = None
    item = setup_gateway(kernel_env, adapter)
    kernel, world, request = item["kernel"], item["world"], item["request"]
    wr = world["world_ref"]
    bound = TaskBinding(request["task_id"], request["generation"], request["worker_id"])
    runtime = McpRuntime(kernel, item["principal"], bound)
    invoke_bound_tool(runtime, "nf_task_get", {}, request_id="private-tool-argument-sentinel")
    result = item["gateway"].execute(item["principal"], request)
    with kernel.db.uow(write=False) as repo:
        audit = kernel.artifact(repo, result["evidence_ref"], wr["work_id"])["payload"]
    sibling = kernel.fork(world["principal"], wr, "CANDIDATE", "evidence-test-branch")
    other = load_fixture(kernel)
    with kernel.db.uow() as repo:
        excluded = [
            record(
                repo.conn,
                "mcp.started",
                work_id=scope["work_id"],
                branch_id=scope["branch_id"],
                tool_name="nf_task_get",
            )
            for scope in (sibling, other["world_ref"])
        ]
        excluded.append(record(repo.conn, "grant.issued", work_id=wr["work_id"]))
        kernel._receipt(
            repo,
            world["principal"],
            wr,
            "P6_IDEA_ENROLL",
            "initial",
            {},
            {
                "task_id": request["task_id"],
                "input_ref": audit["input_ref"],
                "creative_input": "private-idea-sentinel",
            },
        )
    auditor = kernel.issue_grant(
        world["principal"],
        wr["work_id"],
        role_id="control_operator",
        capabilities=["ADMIN"],
        branch_ids=[wr["branch_id"]],
    )
    owner = auditor["principal"]
    out = io.BytesIO()
    receipt = export_runtime_evidence(kernel, owner, wr["work_id"], wr["branch_id"], out)
    raw = out.getvalue()
    rows = _rows(raw)
    assert receipt["file_sha256"] == sha256(raw).hexdigest()
    assert receipt["row_counts"]["call"] == 1
    assert rows[0] == {
        "record": "scope",
        "version": "1.0",
        "work_id": wr["work_id"],
        "branch_id": wr["branch_id"],
    }
    for value in [
        *excluded,
        other["world_ref"]["work_id"],
        "private-idea-sentinel",
        "private-project",
        "private-tool-argument-sentinel",
        "private-receipt-sentinel",
        "private-provider-response-sentinel",
        "https://",
        "完成当前合法",
        "他在桥前",
        world["token"],
    ]:
        assert str(value).encode() not in raw
    changes = [
        r for r in rows if r["record"] == "task_transition" and r["task_id"] == request["task_id"]
    ]
    assert {"READY", "RUNNING"} <= {r["state"] for r in changes} and all(
        r["changed_at"] for r in changes
    )
    idea = next(r for r in rows if r.get("command_type") == "P6_IDEA_ENROLL")
    assert idea["input_ref"] == audit["input_ref"] and idea["task_id"] == request["task_id"]
    actual = next(r for r in rows if r["record"] == "call")
    assert actual["ledger_state"] == ("SUCCEEDED" if known else "UNKNOWN")
    assert actual["actual_amount"] == ("0.010000" if known else None)
    assert actual["call_origin"] == "FIXTURE" and actual["provider"] == "test-provider.invalid"
    assert actual["model"] == "p2-gateway-fixture-v1"
    assert actual["provider_receipt_sha256"] == sha256(b"private-receipt-sentinel").hexdigest()
    assert actual["input_tokens"] == 25 and actual["output_tokens"] == 40
    assert actual["evidence_sha256"] == result["evidence_ref"]["sha256"]
    assert (
        actual["prompt_ref"] == audit["prompt_ref"]
        and actual["response_ref"] == audit["response_ref"]
    )
    tasks = [r for r in rows if r["record"] == "task_generation" and r["context_bundle_id"]]
    assert (
        len(tasks) == 1
        and tasks[0]["task_id"] == request["task_id"]
        and tasks[0]["role_id"] == "writer"
    )
    assert tasks[0]["context_bundle_id"] == audit["context_bundle_id"]
    assert tasks[0]["skill"] and tasks[0]["worker_claimed_at"] and tasks[0]["runtime_sha256"]
    assert any(
        r.get("event_type") == "local_tool.finished" and r["tool_name"] == "nf_task_get"
        for r in rows
    )
    for principal, scope in (
        (owner, sibling),
        (owner, other["world_ref"]),
        (item["principal"], wr),
    ):
        rejected = io.BytesIO()
        with pytest.raises(DomainError) as denied:
            export_runtime_evidence(
                kernel, principal, scope["work_id"], scope["branch_id"], rejected
            )
        assert denied.value.code == "VIEW_DENIED" and not rejected.getvalue()

    config = binding(tmp_path / "private.json", kernel_env, auditor["token"], wr)
    path = tmp_path / "run.jsonl"

    def cli():
        return run_cli("p6_runtime_evidence.py", "--binding", config, "--output", path)

    run = cli()
    assert run.returncode == 0, run.stderr
    assert (
        json.loads(run.stdout) == receipt
        and path.read_bytes() == raw
        and read_private_bytes(path, len(raw)) == raw
        and (os.name == "nt" or path.stat().st_mode & 0o077 == 0)
    )
    assert cli().stderr.strip() == "OUTPUT_EXISTS" and path.read_bytes() == raw
    kernel.revoke_grant(world["principal"], owner.grant_id)
    path.unlink()
    revoked = cli()
    assert revoked.returncode != 0 and not revoked.stdout and not path.exists()


def test_runtime_export_matches_ordered_release_and_manifest(kernel_env, request):
    runner, result, _ = request.getfixturevalue("frozen_volume")
    wr, expected = result["world_ref"], result["release"]
    out = io.BytesIO()
    receipt = export_runtime_evidence(
        kernel_env["kernel"], runner.principal, wr["work_id"], wr["branch_id"], out
    )
    rows = _rows(out.getvalue())
    accepted = [
        r
        for r in rows
        if r["record"] == "business_receipt" and r["command_type"] == "NARRATIVE_ACCEPT"
    ]
    with kernel_env["kernel"].db.uow(write=False) as repo:
        record = kernel_env["kernel"].artifact(
            repo, expected["release_ref"], wr["work_id"], kind="RELEASE_RECORD"
        )["payload"]
    assert (
        len(record["ordered_acceptance_refs"])
        == len(expected["manifest"]["ordered_artifacts"])
        == 6
    )
    assert all(
        ref in [r["artifact_ref"] for r in accepted] for ref in record["ordered_acceptance_refs"]
    )
    assert any(
        r["record"] == "business_receipt"
        and r["command_type"] == "NARRATIVE_RECONCILE"
        and r["reconciliation_ref"]
        for r in rows
    )
    release = next(r for r in rows if r["record"] == "release")
    assert (
        receipt["row_counts"]["release"] == 1 and release["release_ref"] == expected["release_ref"]
    )
    assert release["manifest_ref"] == expected["manifest_ref"]
    assert release["ordered_text_refs"] == [
        item["artifact_ref"] for item in expected["manifest"]["ordered_artifacts"]
    ]
    assert (
        release["external_delivery"] == "NOT_REQUESTED"
        and release["manifest_sha256"] == expected["manifest"]["manifest_sha256"]
    )
