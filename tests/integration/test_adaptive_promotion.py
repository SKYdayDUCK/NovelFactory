"""Release an actual held-out experiment through a signed, subject-independent opinion."""

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from uuid import uuid4

import pytest
from adaptive_helpers import setup_adaptive_case
from test_agent_gateway import FixtureAdapter

from novel_factory.application.adaptive_evaluation import AdaptiveEvaluationService
from novel_factory.application.adaptive_instructions import AdaptiveInstructionService
from novel_factory.application.adaptive_promotion import AdaptivePromotionService
from novel_factory.application.adaptive_runtime import read_selection
from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.authorization import ADMIN_CAPABILITIES
from novel_factory.application.runtime_tasks import RuntimeTaskService
from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import point, run_command, load_fixture
from novel_factory.interfaces.review_independence import enroll


pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def release_case(kernel, tmp_path, monkeypatch):
    source = setup_adaptive_case(kernel, tmp_path, monkeypatch)
    admin, wr = source["admin"], source["wr"]
    proposal = AdaptiveInstructionService(kernel).propose(admin, wr, **source["args"])
    candidate = proposal["candidate_ref"]
    run = AdaptiveEvaluationService(kernel).run(
        admin, wr, candidate_ref=candidate, idempotency_key="measure-adaptive-arms"
    )
    return release_inputs(source, run)


def release_inputs(source, run):
    """Bind an independent reviewer and publisher to a completed source evaluation."""
    kernel, admin, wr = source["kernel"], source["admin"], source["wr"]
    candidate = run["candidate_ref"]
    refs = {
        "candidate_ref": candidate,
        "engineering_report_ref": run["engineering_report_ref"],
        "run_manifest_ref": run["run_manifest_ref"],
    }
    runtime = RuntimeTaskService(kernel, AgentGateway(kernel, {"fixture": FixtureAdapter()}))
    reviewing = runtime.prepare(
        admin,
        wr,
        role_id="reviewer",
        task_kind="literary",
        subject_ids=[source["world"]["ids"]["sword"]],
        predicates=["owner"],
        assignment="核对改动后两组试验的差异与不足。",
        story_time=point(1, 1),
        adapter_id="fixture",
        input_artifacts=list(refs.values()),
    )
    publisher = kernel.issue_grant(
        admin,
        wr["work_id"],
        role_id="control_operator",
        capabilities=ADMIN_CAPABILITIES,
        planes=["WORLD", "PLAN"],
        branch_ids=[wr["branch_id"]],
        max_story_time=point(100),
    )["principal"]
    service = AdaptivePromotionService(kernel)
    job = {"reviewer_task_id": reviewing.task["task_id"], "reviewer_generation": reviewing.task["generation"]}
    case = service.describe_case(admin, wr, **refs, **job, publisher_grant_id=publisher.grant_id)
    return {
        **source,
        "refs": refs,
        "job": job,
        "case": case,
        "reviewer": reviewing,
        "publisher": publisher,
        "service": service,
        "runtime": runtime,
        "target": case_target(kernel, wr, candidate),
    }


def case_target(kernel, wr, ref):
    with kernel.db.uow() as repo:
        svc = AdaptivePromotionService(kernel)
        _, design = svc._candidate(repo, wr, ref)
        return {
            **{k: design["target"][k] for k in ("role_id", "task_kind", "skill_name")},
            "dynamic_instance_ref": design["target"].get("instance_ref"),
        }


def enroll_case(prepared, tmp_path, *, same_group=False):
    case = prepared["case"]
    groups = {who["principal_id"]: "original instruction and evaluation group" for who in case["producers"]}
    groups[case["reviewer"]["principal_id"]] = "external comparative reviewer"
    groups[case["publisher"]["principal_id"]] = (
        "external comparative reviewer" if same_group else "release authority"
    )
    records = []
    for pid, group in groups.items():
        path = Path(tmp_path) / (pid + "-adaptive-identity.json")
        path.write_text(
            json.dumps(
                {
                    "kind": "VERIFIED_SUBJECT_GROUP",
                    "work_id": case["world_ref"]["work_id"],
                    "branch_id": case["world_ref"]["branch_id"],
                    "principal_id": pid,
                    "subject_group": group,
                    "source_record_id": "case-" + pid,
                    "verified_by": "offline independent test custodian",
                }
            )
        )
        path.chmod(0o600)
        records.append(path)
    return enroll(case, records, (datetime.now(UTC) + timedelta(minutes=10)).isoformat())


def test_independent_release_monitor_and_rollback_preserve_world_and_historical_task(
    kernel_env, tmp_path, monkeypatch
):
    k = kernel_env["kernel"]
    s = release_case(k, tmp_path, monkeypatch)
    svc, wr, refs, r, pub = s["service"], s["wr"], s["refs"], s["reviewer"], s["publisher"]

    def world_state():
        with k.db.uow() as repo:
            return (
                k.head(repo, wr["work_id"], wr["branch_id"]),
                repo.one("works", {"work_id": wr["work_id"]})["interpretation_epoch"],
                repo.many("events", {"work_id": wr["work_id"]}),
            )

    before = world_state()
    deadline = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    args = {
        **refs,
        **s["job"],
        "publisher_grant_id": pub.grant_id,
        "verdict": "APPROVE",
        "reason": "Held-out objective checks improve and all adverse rows remain recorded.",
        "valid_until": deadline,
        "idempotency_key": "opinion-1",
    }
    data = svc.evidence(r.principal, wr, **refs, **s["job"])
    assert data["eligible"] and len(data["cases"]) >= 4
    with pytest.raises(DomainError):
        svc.review(r.principal, wr, **args)
    with pytest.raises(DomainError):
        enroll_case(s, tmp_path, same_group=True)
    path = enroll_case(s, tmp_path)
    with pytest.raises(DomainError):
        svc.review(s["admin"], wr, **args)
    other = load_fixture(k)
    with pytest.raises(DomainError):
        svc.evidence(r.principal, other["world_ref"], **refs, **s["job"])
    opinion = svc.review(r.principal, wr, **args)
    dec = {
        **refs,
        "review_ref": opinion["review_ref"],
        "action": "APPROVE",
        "expected_epoch": 0,
        "reason": "Publish the frozen, checked method.",
        "valid_until": deadline,
        "idempotency_key": "publish-1",
    }
    with pytest.raises(DomainError):
        svc.decide(s["admin"], wr, **dec)
    with pytest.raises(DomainError):
        svc.decide(pub, wr, **dec, route="POLICY_AUTOMATIC")
    sibling = k.fork(s["admin"], wr, "CANDIDATE", "foreign-authority")
    with pytest.raises(DomainError):
        svc.decide(pub, sibling, **dec)
    # The signed authorizing reports can be withdrawn independently of this new promotion case.
    from novel_factory.application.review_independence import native_case_path

    with k.db.uow() as repo:
        source_ref = s["records"][0][2]["independence_ref"]
        source_case = k.artifact(repo, source_ref, wr["work_id"])["payload"]["binding"]
    source_path = native_case_path(source_case)
    original = source_path.read_bytes()
    source_path.unlink()
    try:
        with pytest.raises(DomainError):
            svc.decide(pub, wr, **dec)
        with pytest.raises(DomainError):
            svc.evidence(r.principal, wr, **refs, **s["job"])
    finally:
        source_path.write_bytes(original)
        source_path.chmod(0o600)
    with k.db.uow() as repo:
        expired_task = {"task_id": s["job"]["reviewer_task_id"]}
        lease = repo.one("tasks", expired_task)["lease_expiry"]
        repo.change("tasks", expired_task, {"lease_expiry": repo.now() - timedelta(seconds=1)})
    with pytest.raises(DomainError, match="lease"):
        svc.decide(pub, wr, **dec)
    with k.db.uow() as repo:
        repo.change("tasks", expired_task, {"lease_expiry": lease})
    released = svc.decide(pub, wr, **dec)
    assert released == svc.decide(pub, wr, **dec) and released["epoch"] == 1
    with pytest.raises(DomainError):
        svc.decide(pub, wr, **{**dec, "idempotency_key": "stale-cas"})
    with k.db.uow() as repo:
        active = svc.selected(repo, wr, **s["target"])
        assert active["selection_ref"] == released["selection_ref"]
        assert len(read_selection(k, repo, wr, active["selection_ref"])) == 1
    job = s["runtime"].prepare(
        s["admin"],
        wr,
        role_id=s["target"]["role_id"],
        task_kind=s["target"]["task_kind"],
        story_time=point(1, 1),
        subject_ids=[s["world"]["ids"]["sword"]],
        predicates=["owner"],
        assignment="检查修订后的叙事结构。",
        adapter_id="fixture",
    )
    monitored = svc.monitor(
        pub,
        wr,
        decision_ref=released["decision_ref"],
        task_id=job.task["task_id"],
        generation=job.task["generation"],
        idempotency_key="monitor-current-task",
    )
    assert monitored["selection_ref"] == active["selection_ref"] and monitored["attempts"] == []
    path.unlink()
    with k.db.uow() as repo, pytest.raises(DomainError):
        svc.selected(repo, wr, **s["target"])
    rolled = svc.rollback(
        pub,
        wr,
        decision_ref=released["decision_ref"],
        expected_epoch=1,
        reason="Return future assignments to the baseline.",
        idempotency_key="rollback-1",
    )
    assert rolled["selection_ref"] is None and rolled["epoch"] == 2
    with k.db.uow() as repo:
        assert svc.selected(repo, wr, **s["target"]) is None
        frozen = k.artifact(repo, job.agent_input["input_ref"], wr["work_id"])["payload"]
        assert frozen["adaptive_instruction_ref"] == released["selection_ref"]
        assert (
            read_selection(k, repo, wr, frozen["adaptive_instruction_ref"])[0]["body"]
            == active["instructions"]
        )
    assert world_state() == before


def test_automatic_release_requires_live_scoped_policy_and_current_independent_opinion(
    kernel_env, tmp_path, monkeypatch
):
    from novel_factory.infrastructure.creative_config import CreativeHarnessConfig

    k = kernel_env["kernel"]
    s = release_case(k, tmp_path, monkeypatch)
    svc, wr, refs, pub = s["service"], s["wr"], s["refs"], s["publisher"]
    enroll_case(s, tmp_path)
    until = (datetime.now(UTC) + timedelta(minutes=4)).isoformat()
    request = {**refs, **s["job"], "publisher_grant_id": pub.grant_id, "valid_until": until}
    reviewer = s["reviewer"].principal
    reject = svc.review(
        reviewer,
        wr,
        **request,
        verdict="REJECT",
        reason="Retain the original version for now.",
        idempotency_key="reject-opinion",
    )
    args = {**refs, "expected_epoch": 0, "reason": "Evaluate the signed decision.", "valid_until": until}
    with pytest.raises(DomainError):
        svc.decide(
            pub, wr, **args, action="APPROVE", review_ref=reject["review_ref"], idempotency_key="override"
        )
    decision = svc.decide(
        pub, wr, **args, action="REJECT", review_ref=reject["review_ref"], idempotency_key="reject"
    )
    assert decision["selection_ref"] is None and decision["epoch"] == 0
    accept = svc.review(
        reviewer,
        wr,
        **request,
        verdict="APPROVE",
        reason="I accept these held-out results.",
        idempotency_key="approve-opinion",
    )
    with pytest.raises(DomainError):
        svc.decide(
            pub,
            wr,
            **args,
            action="REJECT",
            review_ref=reject["review_ref"],
            idempotency_key="superseded-opinion",
        )
    args.update(action="APPROVE", review_ref=accept["review_ref"], route="POLICY_AUTOMATIC")
    policy = svc.policy(
        s["admin"],
        wr,
        **s["target"],
        publisher_grant_id=pub.grant_id,
        valid_until=until,
        idempotency_key="scope-automatic",
    )
    with pytest.raises(DomainError):
        svc.decide(pub, wr, **args, policy_ref=policy["policy_ref"], idempotency_key="off")
    cfg = CreativeHarnessConfig(enabled=True, adaptive_instructions=True, auto_promote=True)
    monkeypatch.setenv("NF_CREATIVE_HARNESS_CONFIG", json.dumps(cfg.as_dict()))
    with pytest.raises(DomainError):
        svc.decide(pub, wr, **args, idempotency_key="missing-policy")
    wrong = svc.policy(
        s["admin"],
        wr,
        **{**s["target"], "role_id": "writer"},
        publisher_grant_id=pub.grant_id,
        valid_until=until,
        idempotency_key="wrong-policy",
    )
    with pytest.raises(DomainError):
        svc.decide(pub, wr, **args, policy_ref=wrong["policy_ref"], idempotency_key="mis-scoped")
    promoted = svc.decide(pub, wr, **args, policy_ref=policy["policy_ref"], idempotency_key="eligible-auto")
    assert promoted["epoch"] == 1
    advanced = run_command(
        k, s["admin"], wr, "ADVANCE_SCOPE", {"scope_id": s["world"]["ids"]["sword2"], "target": point(3)}
    )["world_ref"]
    with k.db.uow() as repo:
        assert (
            advanced != wr
            and svc.selected(repo, advanced, **s["target"])["selection_ref"] == promoted["selection_ref"]
        )

    svc.revoke_policy(
        s["admin"],
        wr,
        policy_ref=policy["policy_ref"],
        reason="End this release policy.",
        idempotency_key="retire-policy",
    )
    with k.db.uow() as repo, pytest.raises(DomainError):
        svc.selected(repo, wr, **s["target"])
    rolled = svc.rollback(
        pub,
        wr,
        decision_ref=promoted["decision_ref"],
        expected_epoch=1,
        reason="Restore static.",
        idempotency_key="manual-withdrawal",
    )
    assert rolled["epoch"] == 2 and rolled["selection_ref"] is None
