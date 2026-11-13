from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text

from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import load_fixture


pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def test_status_and_policy_join_fence_work_grant_and_generation(kernel_env):
    kernel = kernel_env["kernel"]
    first, other = load_fixture(kernel), load_fixture(kernel)
    wr = first["world_ref"]
    role = kernel.issue_grant(first["principal"], wr["work_id"], role_id="writer")["principal"]
    spec = {
        "schema_version": "2.0", "task_id": str(uuid4()), "generation": 0, "role_id": "writer",
        "task_kind": "draft", "state": "READY", "world_ref": wr, "story_time": {"tick": 0, "phase": 0},
        "context_bundle_id": None, "policy_version": "nf-policy-1", "dependency_fingerprint": None,
        "runtime_contract_version": "1" * 64, "capability_grant_id": role.grant_id, "input_artifacts": [],
        "budget": {"currency": "USD", "max_amount": "1", "token_budget": 16},
        "wall_deadline": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    }
    create = kernel.prepare_task_create_request({"spec": spec, "idempotency_key": "private"})
    spec["world_ref"] = other["world_ref"]
    receipt = create(role)
    assert create(role) == receipt
    task_id = receipt["task_id"]
    assert kernel.get_task(role, task_id)["world_ref"] == wr
    assert kernel.claim_task(role, "writer", task_id=task_id)["generation"] == 1
    status = kernel.get_task(role, task_id)
    assert status["generation"] == 1 and status["world_ref"] == wr
    with kernel.db.uow(write=False) as repo:
        for grant in (str(uuid4()), other["principal"].grant_id):
            work, missing = repo.locked_work_grant(wr["work_id"], grant)
            assert work["work_id"] == wr["work_id"] and missing is None
            _, missing, _ = repo.locked_task_status(task_id, grant)
            assert missing is None
    for principal in (other["principal"], first["principal"]):
        with pytest.raises(DomainError) as denied:
            kernel.get_task(principal, task_id)
        assert denied.value.code == "VIEW_DENIED"
    kernel.revoke_grant(first["principal"], role.grant_id)
    for op in (lambda: create(role), lambda: kernel.get_task(role, task_id)):
        with pytest.raises(DomainError) as revoked:
            op()
        assert revoked.value.code == "VIEW_DENIED"


def test_interleaved_create_witness_rechecks_actual_projection(kernel_env):
    kernel = kernel_env["kernel"]
    first, other = load_fixture(kernel), load_fixture(kernel)
    with kernel.db.uow(write=False) as repo:
        for world in (first, other, first):
            assert kernel._read_revision(repo, world["world_ref"], materialize=False) is None
    with kernel.db.uow() as repo:
        repo.conn.execute(text("SET LOCAL session_replication_role = replica"))
        repo.conn.execute(text("UPDATE nf_revisions SET snapshot = '{}'::jsonb WHERE world_revision_id = :id"),
                          {"id": first["world_ref"]["world_revision_id"]})
    with kernel.db.uow(write=False) as repo, pytest.raises(DomainError) as damaged:
        kernel._read_revision(repo, first["world_ref"], materialize=False)
    assert damaged.value.code == "PROJECTION_CORRUPT"
