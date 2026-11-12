"""Correlate independently authenticated Workshop history and NF audit evidence."""

import json

import pytest

from novel_factory.application.audit_log import LogService
from novel_factory.application.workshop_skills import DynamicSkillAuthority
from novel_factory.domain.errors import DomainError
from test_dynamic_skill_dispatch import call, dynamic_site  # noqa: F401


def test_correlated_delegation_freeze_withdrawal_and_denial(dynamic_site):
    site = dynamic_site
    source = site.delegate("audit-origin")
    method = "BLUE-WREN-SKILL-CANARY must never be serialized to an audit row."
    identity = "SENSITIVE-PROFILE-LABEL-CANARY"
    binding = site.share("v1", method)
    reg = site.activate(source, binding, method, profile=identity)
    job = site.delegate("audit-target", reg["instance_ref"])
    authority = DynamicSkillAuthority(site.kernel)
    authority.refresh(job.principal, job.task_id, job.generation, workshop_binding=binding)
    call(
        site.ports["manager"],
        "aw_skill_revoke",
        operation_id="audit-revoke",
        binding_id=binding["binding_id"],
    )
    with pytest.raises(DomainError):
        authority.refresh(job.principal, job.task_id, job.generation, workshop_binding=binding)

    events = LogService.trace(site.kernel, site.owner, site.wr["work_id"], site.wr["branch_id"], job.task_id)
    with pytest.raises(DomainError):
        LogService.trace(site.kernel, job.principal, site.wr["work_id"], site.wr["branch_id"], job.task_id)
    assert not LogService.trace(
        site.kernel,
        site.owner,
        site.wr["work_id"],
        site.kernel.fork(site.owner, site.wr, "CANDIDATE", "other-audit-branch")["branch_id"],
        job.task_id,
    )
    names = {row["event_type"]: row for row in events}
    bound = names["harness.delegation.bound"]["harness_details"]
    verified = names["harness.skill.verified"]["harness_details"]
    denied = names["harness.skill.denied"]
    assert denied["error_code"] == "DYNAMIC_SKILL_DENIED" and denied["outcome"] == "DENIED"
    assert all(
        verified[k] == denied["harness_details"][k] == bound[k]
        for k in ("operation_id", "installation_id", "receipt_ref")
    )
    aw = []
    cursor = None
    while True:
        response = call(
            site.ports["manager"], "aw_history_get", limit=100, **({"cursor": cursor} if cursor else {})
        )
        aw.extend(response["events"])
        cursor = response["next_cursor"]
        if not cursor:
            break
    remote = [
        r["data"] for r in aw if r["source"] == "aw_remote_receipts" and r["ref_id"] == bound["operation_id"]
    ]
    freeze = [
        r["data"] for r in aw if r["source"] == "harness.skill.frozen" and r["data"]["task_id"] == job.task_id
    ]
    withdrawal = [
        r["data"]
        for r in aw
        if r["source"] == "harness.skill.withdrawn" and r["ref_id"] == binding["binding_id"]
    ]
    assert remote and remote[0]["receipt_ref"] == bound["receipt_ref"] and remote[0]["world_ref"] == site.wr
    assert len(freeze) == 1 and freeze[0]["receipt_ref"] == bound["receipt_ref"]
    assert len(withdrawal) == 1 and withdrawal[0]["binding_id"] == denied["harness_details"]["binding_id"]
    assert withdrawal[0]["skill_epoch"] > freeze[0]["skill_epoch"]
    public = json.dumps([events, aw], default=str)
    assert method not in public and identity not in public and job.frozen.private_token not in public
