"""Real NF draft assignments against independent, operator-enrolled native evidence."""
from datetime import UTC, datetime, timedelta
import json
import os
from uuid import uuid4

import pytest

from novel_factory.application.review_independence import ReviewIndependenceService, native_case_path
from novel_factory.domain.errors import DomainError
from novel_factory.interfaces.review_independence import enroll
from harness_review_helpers import review_assignment

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def enroll_native_case(kernel, tmp_path, monkeypatch, manuscript_ref, reviewer_task_id, reviewer_generation, *,
                       controller, producer_group="workshop-authors", reviewer_group="external-editors"):
    service = ReviewIndependenceService(kernel)
    request = {"manuscript_ref": manuscript_ref, "reviewer_task_id": reviewer_task_id, "reviewer_generation": reviewer_generation}
    binding = service.describe(controller, **request)
    directory = tmp_path / "independence"
    directory.mkdir(mode=0o700, exist_ok=True)
    key = directory / "private-key"
    if not key.exists():
        key.write_bytes(os.urandom(32))
        key.chmod(0o600)
    monkeypatch.setenv("NF_REVIEW_NATIVE_KEY", str(key))
    monkeypatch.setenv("NF_REVIEW_NATIVE_ATTESTATIONS", str(directory))
    paths = []
    groups = {p["principal_id"]: producer_group for p in binding["producers"]}
    groups[binding["reviewer"]["principal_id"]] = reviewer_group
    for principal, group in groups.items():
        path = directory / ("record-" + principal)
        path.write_text(json.dumps({"kind": "VERIFIED_SUBJECT_GROUP", "principal_id": principal, "subject_group": group,
                                    "source_record_id": "identity-case-" + principal, "verified_by": "test registrar",
                                    **{k: binding["world_ref"][k] for k in ("work_id", "branch_id")}}))
        path.chmod(0o600)
        paths.append(path)
    return enroll(binding, paths, (datetime.now(UTC) + timedelta(hours=1)).isoformat())


def test_signed_case_is_live_scoped_and_revocable(kernel_env, tmp_path, monkeypatch, review_assignment):
    world, draft, _, reviewer, scope = review_assignment
    kernel, service = kernel_env["kernel"], ReviewIndependenceService(kernel_env["kernel"])
    args = dict(manuscript_ref=draft["draft_ref"], reviewer_task_id=scope["task_id"], reviewer_generation=scope["generation"])
    with pytest.raises(DomainError, match="qualified"):
        service.issue(world["principal"], **args)
    with pytest.raises(DomainError, match="qualified"):
        enroll_native_case(kernel, tmp_path, monkeypatch, **args, controller=world["principal"], reviewer_group="workshop-authors")
    case = enroll_native_case(kernel, tmp_path, monkeypatch, **args, controller=world["principal"])
    ref = service.issue(world["principal"], **args)["independence_ref"]
    assert service.issue(world["principal"], **args)["independence_ref"] == ref
    branch = kernel.fork(world["principal"], world["world_ref"], "CANDIDATE", str(uuid4()))
    other_admin = kernel.issue_grant(world["principal"], branch["work_id"], role_id="other-admin", branch_ids=[branch["branch_id"]],
                                    capabilities=["READ", "ADMIN"])["principal"]
    for unauthorized in [other_admin, reviewer.principal, draft["writer"]]:
        with pytest.raises(DomainError):
            kernel.get_artifact(unauthorized, branch["work_id"], ref)
    assert kernel.get_artifact(world["principal"], branch["work_id"], ref)
    binding = service.describe(world["principal"], **args)
    prod = binding["producers"][0]
    verify = dict(proof_ref=ref, **args, creator_task_id=prod["task_id"], creator_generation=prod["generation"])
    with pytest.raises(DomainError):
        service.refresh_many([ref, {**ref, "sha256": "0" * 64}])
    fresh = service.refresh_many([ref, ref])[ref["artifact_revision_id"]]
    with kernel.db.uow() as repo:
        result = service.verify(repo, **verify, live_proof=fresh)
        assert result["reviewer"]["principal_id"] != result["producer"]["principal_id"]
        assert result["independence_receipt"] == ref
        with pytest.raises(DomainError):
            service.verify(repo, **{**verify, "creator_task_id": reviewer.task["task_id"]}, live_proof=fresh)
    envelope = json.loads(case.read_text())
    envelope["case"]["subjects"][prod["principal_id"]]["group"] = "forged"
    case.write_text(json.dumps(envelope))
    with pytest.raises(DomainError):
        service.refresh(ref)
    case.unlink()
    with pytest.raises(DomainError):
        service.refresh(ref)
    assert not native_case_path(binding).exists()


def test_offline_registrar_enrolls_and_withdraws_exact_case(kernel_env, tmp_path, monkeypatch, review_assignment):
    import subprocess
    import sys
    from test_p5_mcp import config

    kernel = kernel_env["kernel"]
    world, draft, _, _, scope = review_assignment
    case = enroll_native_case(kernel, tmp_path, monkeypatch, draft["draft_ref"], scope["task_id"], scope["generation"],
                              controller=world["principal"])
    case.unlink()
    private = tmp_path / "controller"
    wr = world["world_ref"]
    config(private, kernel_env, world["token"], {"mode": "creative_controller", "work_id": wr["work_id"], "branch_id": wr["branch_id"]})
    args = ["--controller-binding", str(private), "--manuscript-ref", json.dumps(draft["draft_ref"]),
            "--reviewer-task", scope["task_id"], "--generation", str(scope["generation"])]

    def run(action, extra=()):
        return subprocess.run([sys.executable, "-m", "novel_factory.interfaces.review_independence", action, *args, *extra],
                              capture_output=True, text=True, check=True, timeout=20).stdout.strip()

    assert json.loads(run("describe"))["authority"] == "NATIVE"
    sources = [arg for file in sorted(case.parent.glob("record-*")) for arg in ["--subject-evidence", str(file)]]
    assert run("enroll", [*sources, "--expires-at", (datetime.now(UTC) + timedelta(minutes=5)).isoformat()]) == case.name
    ref = json.loads(run("issue"))["independence_ref"]
    assert ReviewIndependenceService(kernel).refresh(ref)
    run("withdraw")
    with pytest.raises(DomainError):
        ReviewIndependenceService(kernel).refresh(ref)
