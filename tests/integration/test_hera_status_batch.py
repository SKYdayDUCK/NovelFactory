from uuid import uuid4

import pytest

from novel_factory.application.task_operations import TaskOperations, TaskOperationsBinding
from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import load_fixture, point
from test_hera_durable_admission import spec

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def test_batch_returns_only_current_owned_tasks_in_input_order(kernel_env):
    kernel = kernel_env["kernel"]
    book = load_fixture(kernel)
    owner, wr = book["principal"], book["world_ref"]
    actor = kernel.issue_grant(owner, wr["work_id"], role_id="writer", capabilities=["READ", "TASK_CREATE", "TASK_CLAIM"],
                               branch_ids=[wr["branch_id"]], max_story_time=point(2))["principal"]
    binding = TaskOperationsBinding(wr["work_id"], wr["branch_id"])
    op = TaskOperations(kernel, actor, binding=binding)
    ids = []
    for _ in range(4):
        item = spec(book, principal=actor, role="writer")
        op.create(item, item["task_id"])
        ids.append(item["task_id"])
    kernel.claim_task(actor, "batch-worker", task_id=ids[0])
    expected = [op.status(i) for i in reversed(ids)]
    assert op.status_batch(binding.work_id, binding.branch_id, list(reversed(ids))) == {"tasks": expected}
    assert expected[-1]["generation"] == 1
    foreign = spec(book)
    kernel.create_task(owner, foreign, foreign["task_id"])
    for bad, code in (([ids[0],foreign["task_id"]], "VIEW_DENIED"), ([str(uuid4())], "VIEW_DENIED"),
                      (ids + [ids[0].upper()], "INVALID_ARGUMENT"), ([], "INVALID_ARGUMENT"),
                      ([str(uuid4()) for _ in range(33)], "INVALID_ARGUMENT")):
        with pytest.raises(DomainError) as error:
            op.status_batch(binding.work_id, binding.branch_id, bad)
        assert error.value.code == code
    with pytest.raises(DomainError) as wrong_branch:
        op.status_batch(binding.work_id, str(uuid4()), ids)
    assert wrong_branch.value.code == "VIEW_DENIED"
    kernel.revoke_grant(owner, actor.grant_id)
    with pytest.raises(DomainError) as revoked:
        op.status_batch(binding.work_id, binding.branch_id, ids)
    assert revoked.value.code == "VIEW_DENIED"
